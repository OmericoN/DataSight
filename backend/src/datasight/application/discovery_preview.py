"""Application services for adaptive OpenAlex discovery previews."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from datasight.config import CODE_VERSION
from datasight.domain.discovery import CatalogPublications
from datasight.domain.discovery_strategy import (
    DiscoveryPhase,
    EvidenceReason,
    StrategyQuery,
    build_citing_queries,
    build_direct_queries,
    build_exact_queries,
    build_focused_queries,
    build_manual_query,
    build_random_query,
    profile_um_catalog,
    rank_strategy_candidates,
    structured_direct_dataset_ids,
    verified_exact_dataset_ids,
)
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.infrastructure.pubfetcher.openalex import OpenAlexApiError, OpenAlexClient, openalex_work_id

PREVIEW_TTL = timedelta(minutes=30)
STRATEGY_VERSION = 3


class DiscoveryPreviewError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "preview_error") -> None:
        super().__init__(message)
        self.kind = kind


def get_um_profile() -> dict[str, Any]:
    with PipelineRepository() as repo:
        records = repo.list_um_dataset_records()
        unresolved = profile_um_catalog(records)
        cache = repo.list_topic_resolution_cache(unresolved["catalog_fingerprint"])
    return profile_um_catalog(records, {name: topic_id for name, topic_id in cache.items() if topic_id})


def get_openalex_status() -> dict[str, Any]:
    return OpenAlexClient().rate_limit_status()


def validate_discovery_preview(
    preview_id: str,
    processing_limit: int | None = None,
) -> dict[str, Any]:
    """Validate an expiring preview before a run is enqueued."""
    with PipelineRepository() as repo:
        preview = repo.get_discovery_preview(preview_id)
        if not preview:
            raise DiscoveryPreviewError(
                "The discovery preview has expired. Create a new preview before launching.",
                kind="stale_preview",
            )
        request = dict(preview.get("request") or {})
        if int(request.get("strategy_version") or 0) != STRATEGY_VERSION:
            raise DiscoveryPreviewError(
                "This discovery preview uses an obsolete strategy version. Create a new preview.",
                kind="strategy_changed",
            )
        current_profile = profile_um_catalog(repo.list_um_dataset_records())
        if current_profile["catalog_fingerprint"] != preview["catalog_fingerprint"]:
            raise DiscoveryPreviewError(
                "The UM catalog changed after this preview. Create a new preview.",
                kind="catalog_changed",
            )
        preview_limit = int(request.get("processing_limit") or 0)
        if processing_limit is not None and processing_limit > preview_limit:
            raise DiscoveryPreviewError(
                f"processing_limit cannot exceed the preview target of {preview_limit}.",
                kind="processing_limit_increase",
            )
    return preview


def create_discovery_preview(request: dict[str, Any]) -> dict[str, Any]:
    request = dict(request)
    client = OpenAlexClient()
    if not client.api_key:
        raise DiscoveryPreviewError(
            "Set OPENALEX_API_KEY before testing the UM discovery strategy.",
            kind="missing_api_key",
        )
    if int(request.get("strategy_version") or 0) != STRATEGY_VERSION:
        raise DiscoveryPreviewError(
            f"strategy_version must be {STRATEGY_VERSION}.",
            kind="strategy_changed",
        )

    mode = str(request.get("mode") or "catalog_funnel")
    if mode not in {"catalog_funnel", "random", "manual"}:
        raise DiscoveryPreviewError("Unsupported discovery mode.", kind="invalid_mode")
    if mode == "random" and request.get("random_seed") is None:
        request["random_seed"] = secrets.randbelow(2_147_483_647) + 1
    with PipelineRepository() as repo:
        records = repo.list_um_dataset_records()
    if mode == "catalog_funnel" and not records:
        raise DiscoveryPreviewError(
            "Import and verify the UM dataset catalog before creating a catalog preview.",
            kind="empty_catalog",
        )

    discovery_limit = int(request.get("discovery_limit") or 500)
    processing_limit = int(request.get("processing_limit") or 50)
    max_cost_usd = float(request.get("max_cost_usd") or 0.25)
    from_year = request.get("from_year")
    to_year = request.get("to_year")
    publication_types = request.get("publication_types") or []
    language = str(request.get("language") or "").casefold() or None
    request["language"] = language

    unresolved_profile = profile_um_catalog(records)
    with PipelineRepository() as repo:
        topic_cache = repo.list_topic_resolution_cache(unresolved_profile["catalog_fingerprint"])
    resolved_topics = {name: topic_id for name, topic_id in topic_cache.items() if topic_id}
    profile = profile_um_catalog(records, resolved_topics)

    fetched: list[dict[str, Any]] = []
    catalog = CatalogPublications.from_records(records) if mode == "catalog_funnel" else None
    source_papers: set[str] = set()
    excluded_catalog_ids: set[str] = set()
    completed_phases: list[str] = []
    phase_results: dict[str, dict[str, Any]] = {}
    actual_cost = 0.0
    actual_calls = 0
    rate_limit: dict[str, str] = {}
    warnings = list(profile.get("warnings") or []) if mode == "catalog_funnel" else []
    partial = False
    stop_reason = "phases_exhausted"
    provider_failed = False
    executed_queries: list[dict[str, Any]] = []

    def execute_phase(
        phase: DiscoveryPhase,
        queries: list[StrategyQuery],
        *,
        stop_after_each_query: bool = False,
    ) -> bool:
        nonlocal actual_cost, actual_calls, rate_limit, partial, stop_reason, provider_failed
        before_ids = {_paper_id(item) for item in fetched}
        result = {
            "status": "completed",
            "fetched": 0,
            "reported_total": 0,
            "unique_added": 0,
            "catalog_hits_skipped": 0,
            "ready_after_phase": _ready_count(fetched, records),
            "calls": 0,
            "cost_usd": 0.0,
        }
        for strategy_query in queries:
            minimum_cost = 0.001 if strategy_query.query else 0.0001
            remaining_cost = max_cost_usd - actual_cost
            if remaining_cost + 1e-12 < minimum_cost:
                result["status"] = "partial"
                partial = True
                stop_reason = "cost_ceiling"
                warnings.append("The OpenAlex cost ceiling was reached; usable partial results were retained.")
                break
            query_audit = {
                "phase": phase.value,
                "evidence_reason": strategy_query.evidence_reason.value,
                "query": strategy_query.query,
                "search_mode": strategy_query.search_mode,
                "filters": strategy_query.filters,
                "sort": strategy_query.sort,
                "sample_size": strategy_query.sample_size,
                "sample_seed": strategy_query.sample_seed,
            }
            executed_queries.append(query_audit)
            try:
                response = client.search_works_with_meta(
                    query=strategy_query.query,
                    limit=min(strategy_query.max_results, max(100, discovery_limit)),
                    filters=strategy_query.filters,
                    sort=strategy_query.sort,
                    search_mode=strategy_query.search_mode,
                    sample_size=strategy_query.sample_size,
                    sample_seed=strategy_query.sample_seed,
                    max_cost_usd=remaining_cost,
                )
            except OpenAlexApiError as exc:
                if exc.status_code in {401, 403}:
                    raise DiscoveryPreviewError(
                        "The configured OpenAlex API key was rejected.",
                        kind="invalid_api_key",
                    ) from exc
                if exc.kind == "budget_exhausted":
                    result["status"] = "partial"
                    partial = True
                    stop_reason = "cost_ceiling"
                    warnings.append("The OpenAlex cost ceiling was reached; usable partial results were retained.")
                    break
                provider_failed = True
                partial = True
                result["status"] = "partial"
                warnings.append(f"One {phase.value} OpenAlex query failed after retries.")
                continue

            actual_cost += response.cost_usd
            actual_calls += response.calls
            rate_limit = response.rate_limit or rate_limit
            partial = partial or response.truncated
            if response.truncated:
                result["status"] = "partial"
            result["fetched"] += len(response.works)
            result["reported_total"] += response.total_count
            result["calls"] += response.calls
            result["cost_usd"] = round(float(result["cost_usd"]) + response.cost_usd, 6)
            for work in response.works:
                paper_id = _paper_id(work)
                if catalog and (reason := catalog.exclusion_reason(work)):
                    result["catalog_hits_skipped"] += 1
                    excluded_catalog_ids.add(paper_id)
                    if reason == "source_title" and paper_id.startswith("W") and paper_id not in catalog.work_ids:
                        source_papers.add(paper_id)
                    continue
                if phase == DiscoveryPhase.RANDOM:
                    work["random_sample_rank"] = len(fetched) + 1
                reasons = set(work.get("discovery_reasons") or []) | {strategy_query.evidence_reason.value}
                if phase == DiscoveryPhase.DIRECT and strategy_query.query:
                    reasons.add(EvidenceReason.REUSE_LANGUAGE.value)
                work["discovery_reasons"] = sorted(reasons)
                verified_ids = verified_exact_dataset_ids(work, strategy_query)
                direct_ids = structured_direct_dataset_ids(work, strategy_query)
                work["matched_um_dataset_ids"] = sorted(
                    set(work.get("matched_um_dataset_ids") or [])
                    | set(verified_ids)
                    | set(direct_ids)
                )
                fetched.append(work)
            if stop_after_each_query and _ready_count(fetched, records) >= processing_limit:
                stop_reason = "ready_target_met"
                break

        after_ids = {_paper_id(item) for item in fetched}
        result["unique_added"] = len(after_ids - before_ids)
        result["ready_after_phase"] = _ready_count(fetched, records)
        result["duplicate_count"] = int(result["fetched"]) - int(result["catalog_hits_skipped"]) - int(result["unique_added"])
        result["duplicate_rate"] = round(
            float(result["duplicate_count"]) / int(result["fetched"]), 4
        ) if result["fetched"] else 0.0
        result["cost_per_unique_added"] = round(
            float(result["cost_usd"]) / int(result["unique_added"]), 6
        ) if result["unique_added"] else None
        phase_results[phase.value] = result
        if result["status"] == "completed":
            completed_phases.append(phase.value)
        return stop_reason in {"ready_target_met", "cost_ceiling"}

    if mode == "manual":
        execute_phase(
            DiscoveryPhase.MANUAL,
            [
                build_manual_query(
                    str(request.get("manual_query") or ""),
                    from_year=from_year,
                    to_year=to_year,
                    publication_types=publication_types,
                    language=language,
                )
            ],
        )
        if _ready_count(fetched, records) >= processing_limit:
            stop_reason = "ready_target_met"
    elif mode == "random":
        execute_phase(
            DiscoveryPhase.RANDOM,
            [
                build_random_query(
                    discovery_limit,
                    int(request["random_seed"]),
                    from_year=from_year,
                    to_year=to_year,
                    publication_types=publication_types,
                    language=language,
                )
            ],
        )
        if _ready_count(fetched, records) >= processing_limit:
            stop_reason = "ready_target_met"
    else:
        common = {
            "from_year": from_year,
            "to_year": to_year,
            "publication_types": publication_types,
            "language": language,
        }
        halted = execute_phase(DiscoveryPhase.DIRECT, build_direct_queries(records, **common))
        if not halted and _ready_count(fetched, records) >= processing_limit:
            stop_reason = "ready_target_met"
            halted = True
        direct_sources = source_papers.copy()
        if not halted and direct_sources:
            phase = DiscoveryPhase.SOURCE_CITATIONS
            halted = execute_phase(phase, build_citing_queries(direct_sources, phase, **common), stop_after_each_query=True)
        if not halted:
            halted = execute_phase(DiscoveryPhase.EXACT, build_exact_queries(records, **common))
            if not halted and _ready_count(fetched, records) >= processing_limit:
                stop_reason = "ready_target_met"
                halted = True
        if not halted and catalog:
            seeds = (catalog.related_work_ids | source_papers) - direct_sources - catalog.work_ids
            phase = DiscoveryPhase.RELATED
            halted = execute_phase(phase, build_citing_queries(seeds, phase, **common), stop_after_each_query=True)
        if not halted:
            unresolved_names = [
                name for name in unresolved_profile.get("top_topics") or [] if name not in topic_cache
            ]
            (
                new_topics,
                topic_cost,
                topic_calls,
                topic_warnings,
                topic_budget_hit,
                topic_provider_failed,
            ) = _resolve_topics(
                client,
                unresolved_names,
                max_cost_usd - actual_cost,
            )
            actual_cost += topic_cost
            actual_calls += topic_calls
            warnings.extend(topic_warnings)
            provider_failed = provider_failed or topic_provider_failed
            partial = partial or topic_provider_failed
            with PipelineRepository() as repo:
                repo.save_topic_resolutions(
                    unresolved_profile["catalog_fingerprint"],
                    unresolved_names[:12],
                    new_topics,
                )
            resolved_topics.update(new_topics)
            profile = profile_um_catalog(records, resolved_topics)
            if topic_budget_hit:
                partial = True
                stop_reason = "cost_ceiling"
                phase_results[DiscoveryPhase.FOCUSED.value] = {
                    "status": "partial",
                    "fetched": 0,
                    "reported_total": 0,
                    "unique_added": 0,
                    "ready_after_phase": _ready_count(fetched, records),
                    "calls": topic_calls,
                    "cost_usd": topic_cost,
                    "duplicate_count": 0,
                    "duplicate_rate": 0.0,
                    "cost_per_unique_added": None,
                }
            else:
                execute_phase(
                    DiscoveryPhase.FOCUSED,
                    build_focused_queries(
                        records,
                        resolved_topics,
                        focus_query=str(request.get("focus_query") or ""),
                        **common,
                    ),
                    stop_after_each_query=True,
                )
                focused = phase_results.get(DiscoveryPhase.FOCUSED.value)
                if focused:
                    focused["calls"] += topic_calls
                    focused["cost_usd"] = round(float(focused["cost_usd"]) + topic_cost, 6)
                    if topic_provider_failed:
                        focused["status"] = "partial"
                        if DiscoveryPhase.FOCUSED.value in completed_phases:
                            completed_phases.remove(DiscoveryPhase.FOCUSED.value)

    candidates = rank_strategy_candidates(
        fetched,
        [record.um_dataset_id for record in records],
        processing_limit=processing_limit,
        discovery_limit=discovery_limit,
    )
    ready_count = sum(bool(candidate["pipeline_ready"]) for candidate in candidates)
    watchlist_count = len(candidates) - ready_count
    included_count = sum(bool(candidate["included"]) for candidate in candidates)
    if stop_reason == "phases_exhausted" and provider_failed:
        stop_reason = "provider_failure"

    preview_id = str(uuid4())
    expires_at = datetime.now(UTC) + PREVIEW_TTL
    unique_fetched = len({_paper_id(item) for item in fetched} | excluded_catalog_ids)
    if mode == "random":
        estimated_cost = round(math.ceil(discovery_limit / 100) * 0.0001, 4)
    elif mode == "manual":
        estimated_cost = round(math.ceil(discovery_limit / 100) * 0.001, 4)
    else:
        estimated_cost = round(
            sum(float(phase["estimated_cost_usd"]) for phase in profile["phases"]), 4
        )
    payload = {
        "preview_id": preview_id,
        "strategy_version": STRATEGY_VERSION,
        "strategy_fingerprint": _strategy_fingerprint(profile["catalog_fingerprint"], request),
        "catalog_fingerprint": profile["catalog_fingerprint"],
        "language": language,
        "code_version": CODE_VERSION,
        "provider": "openalex",
        "provider_snapshot_at": datetime.now(UTC).isoformat(),
        "request": request,
        "executed_queries": executed_queries,
        "expires_at": expires_at.isoformat(),
        "candidate_count": len(candidates),
        "included_count": included_count,
        "ready_count": ready_count,
        "watchlist_count": watchlist_count,
        "estimated_cost_usd": estimated_cost,
        "actual_cost_usd": round(actual_cost, 6),
        "actual_calls": actual_calls,
        "max_cost_usd": max_cost_usd,
        "random_seed": int(request["random_seed"]) if mode == "random" else None,
        "partial": partial,
        "stop_reason": stop_reason,
        "completed_phases": completed_phases,
        "phase_results": phase_results,
        "rate_limit": rate_limit,
        "warnings": list(dict.fromkeys(warnings)),
        "metrics": {
            "pipeline_ready_definition": "open_access_pdf_url_present_unverified",
            "target_met": ready_count >= processing_limit,
            "unique_catalog_works_skipped": len(excluded_catalog_ids),
            "unique_fetched": unique_fetched,
            "duplicates_seen": sum(result["fetched"] for result in phase_results.values()) - unique_fetched,
            "cost_per_retained_candidate": round(actual_cost / len(candidates), 6) if candidates else None,
            "cost_per_pdf_ready_candidate": round(actual_cost / ready_count, 6) if ready_count else None,
        },
        "profile": profile,
        "candidates": candidates,
    }
    with PipelineRepository() as repo:
        repo.save_discovery_preview(
            preview_id=preview_id,
            catalog_fingerprint=profile["catalog_fingerprint"],
            request=request,
            payload=payload,
            expires_at=expires_at,
        )
    return payload


def materialize_discovery_preview(
    preview_id: str,
    pipeline_run_id: int,
    processing_limit: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    preview = validate_discovery_preview(preview_id, processing_limit)
    request = dict(preview.get("request") or {})
    effective_limit = processing_limit or int(request["processing_limit"])
    payload = dict(preview.get("payload") or {})
    candidates = [dict(candidate) for candidate in payload.get("candidates") or []]
    excluded = {openalex_work_id(value) for value in excluded_candidate_ids or []}
    included = 0
    for candidate in candidates:
        paper_id = openalex_work_id(str(candidate.get("paper_id") or candidate.get("paperId") or ""))
        candidate["included"] = False
        if paper_id in excluded:
            candidate["exclusion_reason"] = "Excluded in preview"
        elif not candidate.get("pipeline_ready"):
            candidate["exclusion_reason"] = "No usable PDF link"
        elif included >= effective_limit:
            candidate["exclusion_reason"] = "Processing limit reached"
        else:
            candidate["included"] = True
            candidate["exclusion_reason"] = None
            included += 1

    with PipelineRepository() as repo:
        repo.upsert_publications(candidates, source="openalex")
        persisted = repo.persist_discovery_candidates(pipeline_run_id, candidates)
        paper_ids = repo.included_discovery_paper_ids(pipeline_run_id)
    return {
        "preview_id": preview_id,
        "persisted": persisted,
        "included": len(paper_ids),
        "paper_ids": paper_ids,
        "candidate_count": len(candidates),
        "actual_cost_usd": payload.get("actual_cost_usd", 0),
        "phase_results": payload.get("phase_results", {}),
        "stop_reason": payload.get("stop_reason"),
    }


def _resolve_topics(
    client: OpenAlexClient,
    topic_names: list[str],
    remaining_budget: float,
) -> tuple[dict[str, str], float, int, list[str], bool, bool]:
    resolved: dict[str, str] = {}
    cost = 0.0
    calls = 0
    warnings: list[str] = []
    budget_hit = False
    provider_failed = False
    for name in topic_names[:12]:
        if remaining_budget - cost < 0.001:
            warnings.append("Topic resolution stopped at the preview cost ceiling.")
            budget_hit = True
            break
        try:
            result = client.list_entities_with_meta(
                "topics",
                query=name,
                limit=5,
                select=["id", "display_name"],
                max_cost_usd=remaining_budget - cost,
            )
        except OpenAlexApiError as exc:
            if exc.status_code in {401, 403}:
                raise DiscoveryPreviewError(
                    "The configured OpenAlex API key was rejected.", kind="invalid_api_key"
                ) from exc
            if exc.kind == "budget_exhausted":
                budget_hit = True
                warnings.append("Topic resolution stopped at the preview cost ceiling.")
                break
            warnings.append("Some topic names could not be resolved through OpenAlex.")
            provider_failed = True
            continue
        cost += float(result["cost_usd"])
        calls += int(result["calls"])
        normalized_name = _normalize(name)
        exact = next(
            (
                row
                for row in result["results"]
                if _normalize(row.get("display_name")) == normalized_name
            ),
            None,
        )
        if exact and exact.get("id"):
            resolved[name] = str(exact["id"]).rstrip("/").rsplit("/", 1)[-1]
    if topic_names and len(resolved) < min(12, len(topic_names)):
        warnings.append(f"Resolved {len(resolved)} of {min(12, len(topic_names))} priority topic names.")
    return resolved, round(cost, 6), calls, warnings, budget_hit, provider_failed


def _ready_count(fetched: list[dict[str, Any]], records: list[Any]) -> int:
    candidates = rank_strategy_candidates(
        fetched,
        [record.um_dataset_id for record in records],
        processing_limit=max(1, len(fetched)),
    )
    return sum(bool(candidate["pipeline_ready"]) for candidate in candidates)


def _paper_id(item: dict[str, Any]) -> str:
    return openalex_work_id(str(item.get("paperId") or item.get("id") or ""))


def _strategy_fingerprint(catalog_fingerprint: str, request: dict[str, Any]) -> str:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{catalog_fingerprint}|{encoded}".encode("utf-8")).hexdigest()[:20]


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())
