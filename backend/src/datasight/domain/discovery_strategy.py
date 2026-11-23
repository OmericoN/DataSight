"""Adaptive OpenAlex discovery funnel planning and candidate ranking."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from datasight.domain.discovery import REUSE_SEARCH, mentions_reuse
from datasight.domain.schemas import UMDatasetRecord


class DiscoveryPhase(StrEnum):
    DIRECT = "direct"
    SOURCE_CITATIONS = "source_citations"
    EXACT = "exact"
    RELATED = "related"
    FOCUSED = "focused"
    RANDOM = "random"
    MANUAL = "manual"


class EvidenceTier(StrEnum):
    DIRECT = "direct"
    EXACT = "exact"
    EXPANDED = "expanded"


class EvidenceReason(StrEnum):
    DATASET_LINK = "dataset_link"
    DATASET_CITATION = "dataset_citation"
    IDENTIFIER_MENTION = "identifier_mention"
    TITLE_MENTION = "title_mention"
    SOURCE_CITATION = "source_paper_citation"
    RELATED_WORK_CITATION = "related_work_citation"
    REUSE_LANGUAGE = "reuse_language"
    FOCUSED_TOPIC = "focused_topic"
    FOCUSED_KEYWORD = "focused_keyword"
    RANDOM_SAMPLE = "random_sample"
    MANUAL_QUERY = "manual_query"


EVIDENCE: dict[EvidenceReason, tuple[EvidenceTier, int]] = {
    EvidenceReason.DATASET_LINK: (EvidenceTier.DIRECT, 90),
    EvidenceReason.DATASET_CITATION: (EvidenceTier.DIRECT, 95),
    EvidenceReason.IDENTIFIER_MENTION: (EvidenceTier.EXACT, 85),
    EvidenceReason.TITLE_MENTION: (EvidenceTier.EXACT, 75),
    EvidenceReason.SOURCE_CITATION: (EvidenceTier.EXPANDED, 65),
    EvidenceReason.RELATED_WORK_CITATION: (EvidenceTier.EXPANDED, 60),
    EvidenceReason.REUSE_LANGUAGE: (EvidenceTier.EXPANDED, 45),
    EvidenceReason.FOCUSED_TOPIC: (EvidenceTier.EXPANDED, 45),
    EvidenceReason.FOCUSED_KEYWORD: (EvidenceTier.EXPANDED, 45),
    EvidenceReason.RANDOM_SAMPLE: (EvidenceTier.EXPANDED, 40),
    EvidenceReason.MANUAL_QUERY: (EvidenceTier.EXPANDED, 45),
}

TIER_BANDS: dict[EvidenceTier, tuple[int, int]] = {
    EvidenceTier.DIRECT: (90, 100),
    EvidenceTier.EXACT: (70, 89),
    EvidenceTier.EXPANDED: (40, 69),
}

TIER_ORDER = {
    EvidenceTier.DIRECT: 3,
    EvidenceTier.EXACT: 2,
    EvidenceTier.EXPANDED: 1,
}

GENERIC_TERMS = {
    "biology",
    "business",
    "chemistry",
    "computer science",
    "data",
    "database",
    "engineering",
    "medicine",
    "physics",
    "psychology",
    "research",
    "science",
    "social sciences",
    "work",
}


@dataclass(frozen=True)
class StrategyQuery:
    phase: DiscoveryPhase
    evidence_reason: EvidenceReason
    query: str | None = None
    search_mode: str = "search"
    filters: dict[str, str | int | bool | Sequence[str | int]] = field(default_factory=dict)
    sort: str | None = "relevance_score:desc"
    sample_size: int | None = None
    sample_seed: int | None = None
    catalog_work_to_dataset_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    exact_terms_to_dataset_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    max_results: int = 1000


def profile_um_catalog(
    records: Iterable[UMDatasetRecord],
    resolved_topic_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    record_list = list(records)
    total = len(record_list)
    dois = [record.doi for record in record_list if record.doi]
    keyword_records = 0
    topic_records = 0
    related_records = 0
    work_ids: list[str] = []
    related_ids: set[str] = set()
    keywords: Counter[str] = Counter()
    topics: Counter[str] = Counter()

    for record in record_list:
        raw = _openalex(record)
        work_id = _id_tail(raw.get("id") or record.um_dataset_id)
        if work_id.startswith("W"):
            work_ids.append(work_id)
        record_keywords = _dedupe(
            [*record.keywords, *_row_terms(raw.get("keywords"), "keyword", "display_name")]
        )
        if record_keywords:
            keyword_records += 1
        keywords.update(term for term in record_keywords if _informative(term))
        record_topics = _row_terms(raw.get("topics"), "topic", "display_name")
        if record_topics:
            topic_records += 1
        topics.update(record_topics)
        record_related = {_id_tail(value) for value in raw.get("related_works") or [] if value}
        record_related.discard("")
        if record_related:
            related_records += 1
            related_ids.update(record_related)

    fingerprint_source = json.dumps(
        sorted(
            (record.model_dump(mode="json") for record in record_list),
            key=lambda record: str(record.get("um_dataset_id") or ""),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    resolved = resolved_topic_ids or {}
    top_topics = [term for term, _ in topics.most_common(24)]
    title_count = len(distinctive_titles(record_list, _title_candidate_count(record_list)))
    keyword_count = len(informative_keywords(record_list, 30))
    work_count = len(set(work_ids))
    warnings: list[str] = []
    if top_topics and len(resolved) < len(top_topics):
        warnings.append(
            f"{len(top_topics) - len(resolved)} priority topic names will be resolved only if focused expansion is needed."
        )
    if not total:
        warnings.append("The UM dataset catalog is empty.")

    phases = [
        _phase_profile(
            DiscoveryPhase.DIRECT,
            "Direct evidence",
            "Dataset links and citations, including a search for reuse language in citing papers.",
            work_count,
            3 * math.ceil(work_count / 100),
            (0.001 + 2 * 0.0001) / 3,
            total,
        ),
        _phase_profile(
            DiscoveryPhase.SOURCE_CITATIONS,
            "Citations of source papers",
            "Search papers citing likely original studies found during direct discovery for signs of data reuse. Call count depends on the results.",
            0, 0, 0.001, total,
        ),
        _phase_profile(
            DiscoveryPhase.EXACT,
            "Exact mentions",
            "Exact dataset DOI, title, and alias searches.",
            len(dois) + title_count,
            math.ceil(len(dois) / 25) + math.ceil(title_count / 5),
            0.001,
            total,
        ),
        _phase_profile(
            DiscoveryPhase.RELATED,
            "Citations of related works",
            "Search for reuse language in papers citing works already listed in the UM catalog.",
            len(related_ids),
            math.ceil(len(related_ids) / 100),
            0.001,
            total,
        ),
        _phase_profile(
            DiscoveryPhase.FOCUSED,
            "Focused expansion",
            "Resolved topics and informative catalog keywords.",
            len(top_topics) + keyword_count,
            min(12, max(0, len(top_topics) - len(resolved)))
            + (1 if top_topics or resolved else 0)
            + math.ceil(keyword_count / 5),
            0.001,
            total,
        ),
    ]
    return {
        "dataset_count": total,
        "catalog_fingerprint": fingerprint,
        "coverage": {
            "openalex_ids": _percent(work_count, total),
            "dois": _percent(len(dois), total),
            "keywords": _percent(keyword_records, total),
            "topics": _percent(topic_records, total),
            "related_works": _percent(related_records, total),
        },
        "counts": {
            "openalex_ids": work_count,
            "dois": len(dois),
            "distinctive_titles": title_count,
            "unique_related_works": len(related_ids),
            "informative_keywords": len(keywords),
            "topic_names": len(topics),
            "resolved_topics": len(resolved),
        },
        "topic_resolution": {
            "status": "resolved" if top_topics and len(resolved) >= len(top_topics) else "partial" if resolved else "pending",
            "resolved": len(resolved),
            "requested": len(top_topics),
            "unresolved": max(0, len(top_topics) - len(resolved)),
        },
        "phases": phases,
        "top_topics": top_topics,
        "top_keywords": [term for term, _ in keywords.most_common(30)],
        "warnings": warnings,
    }


def build_direct_queries(
    records: Sequence[UMDatasetRecord],
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> list[StrategyQuery]:
    filters = _base_filters(from_year, to_year, publication_types, language)
    work_to_datasets: dict[str, set[str]] = {}
    for record in records:
        work_id = _id_tail(_openalex(record).get("id") or record.um_dataset_id)
        if work_id.startswith("W"):
            work_to_datasets.setdefault(work_id, set()).add(record.um_dataset_id)
    work_ids = sorted(work_to_datasets)
    queries: list[StrategyQuery] = []
    reuse_queries: list[StrategyQuery] = []
    for batch in _chunks(work_ids, 100):
        citation = StrategyQuery(
            phase=DiscoveryPhase.DIRECT,
            evidence_reason=EvidenceReason.DATASET_CITATION,
            filters={**filters, "cites": batch},
            sort="cited_by_count:desc",
            catalog_work_to_dataset_ids={work_id: tuple(sorted(work_to_datasets[work_id])) for work_id in batch},
        )
        queries.extend([
            citation,
            replace(citation, evidence_reason=EvidenceReason.DATASET_LINK, filters={**filters, "datasets": batch}),
        ])
        reuse_queries.append(replace(citation, query=REUSE_SEARCH, sort="relevance_score:desc"))
    return queries + reuse_queries


def build_exact_queries(
    records: Sequence[UMDatasetRecord],
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> list[StrategyQuery]:
    filters = _base_filters(from_year, to_year, publication_types, language)
    queries: list[StrategyQuery] = []
    doi_pairs = [(record.um_dataset_id, record.doi) for record in records if record.doi]
    for batch in _chunks(doi_pairs, 25):
        terms = {str(doi): tuple([dataset_id]) for dataset_id, doi in batch}
        queries.append(
            StrategyQuery(
                phase=DiscoveryPhase.EXACT,
                evidence_reason=EvidenceReason.IDENTIFIER_MENTION,
                query=" OR ".join(f'"{_escape_phrase(term)}"' for term in terms),
                search_mode="search.exact",
                filters=dict(filters),
                exact_terms_to_dataset_ids=terms,
            )
        )
    for batch in _chunks(distinctive_titles(records, _title_candidate_count(records)), 5):
        term_map: dict[str, tuple[str, ...]] = {}
        for dataset_id, title in batch:
            term_map[title] = tuple(sorted(set(term_map.get(title, ())) | {dataset_id}))
        queries.append(
            StrategyQuery(
                phase=DiscoveryPhase.EXACT,
                evidence_reason=EvidenceReason.TITLE_MENTION,
                query=" OR ".join(f'"{_escape_phrase(term)}"' for term in term_map),
                search_mode="search.exact",
                filters=dict(filters),
                exact_terms_to_dataset_ids=term_map,
            )
        )
    return queries


def build_citing_queries(
    work_ids: Iterable[str],
    phase: DiscoveryPhase,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> list[StrategyQuery]:
    filters = _base_filters(from_year, to_year, publication_types, language)
    reason = EvidenceReason.SOURCE_CITATION if phase == DiscoveryPhase.SOURCE_CITATIONS else EvidenceReason.RELATED_WORK_CITATION
    return [
        StrategyQuery(
            phase=phase,
            evidence_reason=reason,
            query=REUSE_SEARCH,
            filters={**filters, "cites": batch},
        )
        for batch in _chunks(sorted(set(work_ids)), 100)
    ]


def build_focused_queries(
    records: Sequence[UMDatasetRecord],
    resolved_topic_ids: Mapping[str, str],
    *,
    focus_query: str = "",
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> list[StrategyQuery]:
    filters = _base_filters(from_year, to_year, publication_types, language)
    queries: list[StrategyQuery] = []
    topic_ids = list(dict.fromkeys(resolved_topic_ids.values()))[:100]
    if topic_ids:
        queries.append(
            StrategyQuery(
                phase=DiscoveryPhase.FOCUSED,
                evidence_reason=EvidenceReason.FOCUSED_TOPIC,
                query=focus_query.strip() or None,
                filters={**filters, "topics.id": topic_ids},
            )
        )
    for batch in _chunks(informative_keywords(records, 30), 5):
        terms = " OR ".join(f'"{_escape_phrase(term)}"' for term in batch)
        query = f"({terms})"
        if focus_query.strip():
            query = f"{query} AND ({focus_query.strip()})"
        queries.append(
            StrategyQuery(
                phase=DiscoveryPhase.FOCUSED,
                evidence_reason=EvidenceReason.FOCUSED_KEYWORD,
                query=query,
                filters=dict(filters),
                max_results=100,
            )
        )
    return queries


def build_manual_query(
    query: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> StrategyQuery:
    if not query.strip():
        raise ValueError("A manual preview query is required.")
    return StrategyQuery(
        phase=DiscoveryPhase.MANUAL,
        evidence_reason=EvidenceReason.MANUAL_QUERY,
        query=query.strip(),
        filters=_base_filters(from_year, to_year, publication_types, language),
    )


def build_random_query(
    sample_size: int,
    sample_seed: int,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    publication_types: Sequence[str] = (),
    language: str | None = None,
) -> StrategyQuery:
    if sample_size <= 0:
        raise ValueError("A random sample must contain at least one work.")
    return StrategyQuery(
        phase=DiscoveryPhase.RANDOM,
        evidence_reason=EvidenceReason.RANDOM_SAMPLE,
        filters=_base_filters(from_year, to_year, publication_types, language),
        sort=None,
        sample_size=sample_size,
        sample_seed=sample_seed,
        max_results=sample_size,
    )


def verified_exact_dataset_ids(work: Mapping[str, Any], query: StrategyQuery) -> tuple[str, ...]:
    """Return only dataset IDs whose exact term is visible in returned title/abstract text."""
    haystack = _normalize(f"{work.get('title') or ''} {work.get('abstract') or ''}")
    matched: set[str] = set()
    for term, dataset_ids in query.exact_terms_to_dataset_ids.items():
        if _normalize(term) in haystack:
            matched.update(dataset_ids)
    return tuple(sorted(matched))


def structured_direct_dataset_ids(work: Mapping[str, Any], query: StrategyQuery) -> tuple[str, ...]:
    raw = work.get("raw") if isinstance(work.get("raw"), Mapping) else work
    raw_mapping = raw if isinstance(raw, Mapping) else {}
    field_name = "datasets" if query.evidence_reason == EvidenceReason.DATASET_LINK else "referenced_works"
    matched: set[str] = set()
    for work_id in _work_ids(raw_mapping.get(field_name)):
        matched.update(query.catalog_work_to_dataset_ids.get(work_id, ()))
    return tuple(sorted(matched))


def rank_strategy_candidates(
    publications: Iterable[Mapping[str, Any]],
    known_dataset_ids: Sequence[str],
    processing_limit: int,
    discovery_limit: int | None = None,
    excluded_candidate_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    known_ids = {_id_tail(value) for value in known_dataset_ids}
    excluded_ids = {_id_tail(value) for value in excluded_candidate_ids}
    best: dict[str, dict[str, Any]] = {}

    for item in publications:
        paper_id = _id_tail(item.get("paperId") or item.get("id"))
        if not paper_id or paper_id in known_ids:
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), Mapping) else item
        raw_mapping = raw if isinstance(raw, Mapping) else {}
        publication_type = str(item.get("publication_type") or raw_mapping.get("type") or "")
        if item.get("is_retracted") or raw_mapping.get("is_retracted") or publication_type in {"dataset", "paratext"}:
            continue
        reasons = {
            EvidenceReason(reason)
            for reason in item.get("discovery_reasons") or []
            if reason in {evidence.value for evidence in EvidenceReason}
        }
        if not reasons:
            continue
        matched_ids = set(_work_ids(item.get("matched_um_dataset_ids"))) & known_ids
        if EvidenceReason.DATASET_LINK in reasons:
            matched_ids |= _work_ids(raw_mapping.get("datasets")) & known_ids
        if EvidenceReason.DATASET_CITATION in reasons:
            matched_ids |= _work_ids(raw_mapping.get("referenced_works")) & known_ids
        if matched_ids and mentions_reuse(item):
            reasons.add(EvidenceReason.REUSE_LANGUAGE)

        candidate = dict(item)
        candidate.update(
            {
                "paper_id": paper_id,
                "evidence_reasons": sorted(reason.value for reason in reasons),
                "matched_um_dataset_ids": sorted(matched_ids),
                "pipeline_ready": bool(item.get("open_access_url")),
                "included": False,
                "exclusion_reason": None,
            }
        )
        existing = best.get(paper_id)
        if existing:
            merged_reasons = sorted(set(existing["evidence_reasons"]) | set(candidate["evidence_reasons"]))
            merged_ids = sorted(set(existing["matched_um_dataset_ids"]) | set(candidate["matched_um_dataset_ids"]))
            sample_ranks = [
                int(rank)
                for rank in (existing.get("random_sample_rank"), candidate.get("random_sample_rank"))
                if rank is not None
            ]
            merged_sample_rank = min(sample_ranks) if sample_ranks else None
            existing_quality = _metadata_quality(existing)
            candidate_quality = _metadata_quality(candidate)
            if candidate_quality > existing_quality:
                preserved = {
                    "evidence_reasons": merged_reasons,
                    "matched_um_dataset_ids": merged_ids,
                    "pipeline_ready": bool(existing["pipeline_ready"] or candidate["pipeline_ready"]),
                    "random_sample_rank": merged_sample_rank,
                }
                existing.update(candidate)
                existing.update(preserved)
            else:
                existing["evidence_reasons"] = merged_reasons
                existing["matched_um_dataset_ids"] = merged_ids
                existing["pipeline_ready"] = bool(existing["pipeline_ready"] or candidate["pipeline_ready"])
                if merged_sample_rank is not None:
                    existing["random_sample_rank"] = merged_sample_rank
        else:
            best[paper_id] = candidate

    for candidate in best.values():
        tier, strength = _candidate_evidence(candidate["evidence_reasons"])
        candidate["evidence_tier"] = tier.value
        candidate["candidate_strength"] = strength

    ranked = sorted(best.values(), key=_candidate_sort_key)
    ready = [candidate for candidate in ranked if candidate["pipeline_ready"]]
    reserved_ids = {candidate["paper_id"] for candidate in ready[:processing_limit]}
    cap = discovery_limit if discovery_limit is not None else len(ranked)
    retained_ids = set(reserved_ids)
    for candidate in ranked:
        if len(retained_ids) >= cap:
            break
        retained_ids.add(candidate["paper_id"])
    retained = [candidate for candidate in ranked if candidate["paper_id"] in retained_ids]

    included = 0
    for candidate in retained:
        if candidate["paper_id"] in excluded_ids:
            candidate["exclusion_reason"] = "Excluded in preview"
        elif not candidate["pipeline_ready"]:
            candidate["exclusion_reason"] = "No usable PDF link"
        elif included >= processing_limit:
            candidate["exclusion_reason"] = "Processing limit reached"
        else:
            candidate["included"] = True
            included += 1
    return retained


def distinctive_titles(records: Sequence[UMDatasetRecord], limit: int) -> list[tuple[str, str]]:
    scored: list[tuple[float, str, str]] = []
    all_names = [name for record in records for name in [record.title, *record.aliases] if name]
    title_counts = Counter(_normalize(name) for name in all_names)
    for record in records:
        for raw_name in [record.title, *record.aliases]:
            title = raw_name.strip()
            normalized = _normalize(title)
            words = re.findall(r"[a-z0-9]+", normalized)
            if len(words) < 3 or len(title) < 18 or title_counts[normalized] > 1:
                continue
            informative = sum(word not in GENERIC_TERMS and len(word) > 3 for word in words)
            score = informative + min(len(title), 120) / 120
            scored.append((score, record.um_dataset_id, title))
    return [(dataset_id, title) for _, dataset_id, title in sorted(scored, reverse=True)[:limit]]


def informative_keywords(records: Sequence[UMDatasetRecord], limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    for record in records:
        raw = _openalex(record)
        values = _dedupe([*record.keywords, *_row_terms(raw.get("keywords"), "keyword", "display_name")])
        counts.update(term for term in values if _informative(term))
    max_support = max(5, math.ceil(len(records) * 0.08))
    candidates = [(count, term) for term, count in counts.items() if 2 <= count <= max_support]
    candidates.sort(key=lambda pair: (pair[0], len(pair[1])), reverse=True)
    return [term for _, term in candidates[:limit]]


def _candidate_evidence(reason_values: Sequence[str]) -> tuple[EvidenceTier, int]:
    reasons = [EvidenceReason(value) for value in reason_values]
    strongest = max(reasons, key=lambda reason: (TIER_ORDER[EVIDENCE[reason][0]], EVIDENCE[reason][1]))
    tier, base = EVIDENCE[strongest]
    _, upper = TIER_BANDS[tier]
    return tier, min(upper, base + 3 * max(0, len(set(reasons)) - 1))


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[int, int, int, str]:
    tier = EvidenceTier(str(candidate["evidence_tier"]))
    if EvidenceReason.RANDOM_SAMPLE.value in candidate.get("evidence_reasons", ()):
        return (
            -TIER_ORDER[tier],
            -int(candidate["candidate_strength"]),
            int(candidate.get("random_sample_rank") or 1_000_000_000),
            str(candidate["paper_id"]),
        )
    return (
        -TIER_ORDER[tier],
        -int(candidate["candidate_strength"]),
        -int(candidate.get("cited_by_count") or 0),
        str(candidate["paper_id"]),
    )


def _metadata_quality(candidate: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(bool(candidate.get("open_access_url"))),
        sum(bool(candidate.get(field)) for field in ("title", "doi", "abstract", "source_url")),
        int(candidate.get("cited_by_count") or 0),
    )


def _phase_profile(
    phase: DiscoveryPhase,
    label: str,
    description: str,
    coverage_count: int,
    calls: int,
    unit_cost: float,
    total: int,
) -> dict[str, Any]:
    return {
        "id": phase.value,
        "label": label,
        "description": description,
        "coverage_count": coverage_count,
        "coverage_percent": _percent(coverage_count, total),
        "estimated_calls": calls,
        "estimated_cost_usd": round(calls * unit_cost, 4),
    }


def _title_candidate_count(records: Sequence[UMDatasetRecord]) -> int:
    return sum(1 + len(record.aliases) for record in records)


def _base_filters(
    from_year: int | None,
    to_year: int | None,
    publication_types: Sequence[str],
    language: str | None = None,
) -> dict[str, str | int | bool | Sequence[str | int]]:
    filters: dict[str, str | int | bool | Sequence[str | int]] = {}
    if from_year and to_year:
        filters["publication_year"] = f"{from_year}-{to_year}"
    elif from_year:
        filters["publication_year"] = f">{from_year - 1}"
    elif to_year:
        filters["publication_year"] = f"<{to_year + 1}"
    if publication_types:
        filters["type"] = list(dict.fromkeys(value for value in publication_types if value))[:100]
    if language:
        filters["language"] = language.casefold()
    return filters


def _openalex(record: UMDatasetRecord) -> Mapping[str, Any]:
    raw = record.raw or {}
    openalex = raw.get("openalex")
    return openalex if isinstance(openalex, Mapping) else raw


def _row_terms(value: Any, primary: str, fallback: str) -> list[str]:
    terms: list[str] = []
    for row in value or []:
        if isinstance(row, Mapping) and (term := row.get(primary) or row.get(fallback)):
            terms.append(str(term).strip())
    return terms


def _work_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    values = [value] if isinstance(value, (str, Mapping)) else value or []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("id") or item.get("openalex_id") or item.get("dataset_id")
        if item and (work_id := _id_tail(item)):
            ids.add(work_id)
    return ids


def _informative(term: str) -> bool:
    normalized = _normalize(term)
    return len(normalized) > 3 and normalized not in GENERIC_TERMS and not normalized.isdigit()


def _normalize(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _id_tail(value: Any) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _escape_phrase(value: str) -> str:
    return value.replace('"', " ").strip()


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _percent(count: int, total: int) -> float:
    return min(100.0, round(100 * count / total, 1)) if total else 0.0


def _chunks(values: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])
