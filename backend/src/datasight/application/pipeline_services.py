"""Stage service functions used by Celery tasks and local CLI runs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Literal

from datasight.config import PDF_DIR, PROJECT_ROOT, XML_DIR
from datasight.domain.discovery import (
    REUSE_SEARCH,
    DiscoveryOptions,
    DiscoveryQuery,
    build_discovery_profile,
    build_openalex_discovery_queries,
    dedupe_and_score_publications,
    parse_terms,
)
from datasight.application.discovery_preview import materialize_discovery_preview
from datasight.application.insights import INSIGHT_COLUMNS, _csv_cell, serialize_insights_csv
from datasight.application.mention_screening import screen_publication
from datasight.infrastructure.ingestion.converter import GrobidConverter
from datasight.infrastructure.ingestion.downloader import PDFDownloader
from datasight.infrastructure.ingestion.openalex_exports import (
    load_um_openalex_export_bundle,
    load_um_openalex_exports,
    looks_like_openalex_export,
)
from datasight.infrastructure.ingestion.renderer import (
    DEFAULT_RENDER_PROFILE,
    RENDERER_VERSION,
    RenderProfile,
    render_papers,
    render_to_markdown,
)
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.infrastructure.pubfetcher.openalex import OpenAlexClient, openalex_work_id
from datasight.matching.um_matcher import UMCatalogMatcher
from datasight.domain.candidate_detection import DETECTOR_VERSION, SCREENING_VERSION
from datasight.infrastructure.llm.client import GatewayClient
from datasight.domain.schemas import (
    DatasetMention,
    StageResult,
    UMDatasetRecord,
    UMMatchDecision,
)
from datasight.domain.stages import PipelineStage


def discover_publications(
    query: str,
    limit: int = 100,
    open_access_only: bool = True,
    topic_ids: str | list[str] | None = None,
    keyword_terms: str | list[str] | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    use_um_profile: bool = True,
    pipeline_run_id: int | None = None,
    preview_id: str | None = None,
    processing_limit: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    if preview_id:
        if pipeline_run_id is None:
            raise ValueError("pipeline_run_id is required when materializing a discovery preview.")
        with PipelineRepository() as repo:
            # Keep a redelivered task from rematerializing a pool already in use.
            repo.cursor.execute("SELECT pg_advisory_lock(73017,%s)", (pipeline_run_id,))
            for prior in repo.list_stage_runs(pipeline_run_id):
                if prior["stage"] == PipelineStage.DISCOVER and prior["status"] in {"successful", "skipped"}:
                    return prior["metrics"]
            repo.record_stage_start(PipelineStage.DISCOVER, pipeline_run_id)
            materialized = materialize_discovery_preview(
                preview_id=preview_id,
                pipeline_run_id=pipeline_run_id,
                processing_limit=processing_limit or limit,
                excluded_candidate_ids=excluded_candidate_ids,
            )
            processed_skipped = materialized["processed_skipped"]
            result = _stage_result(
                PipelineStage.DISCOVER,
                "successful" if materialized["paper_ids"] else "skipped",
                int(materialized["persisted"]),
                (
                    f"Filled {materialized['candidate_count']} of {materialized['discovery_limit']} candidate pool slots "
                    f"and queued {materialized['included']} of {materialized['processing_limit']} requested papers."
                    + (f" Skipped {processed_skipped} previously processed publications." if processed_skipped else "")
                ),
                materialized,
            )
            repo.record_stage_result(
                PipelineStage.DISCOVER,
                result["status"],
                result,
                pipeline_run_id,
            )
        return result

    options = DiscoveryOptions(
        topic_ids=parse_terms(topic_ids),
        keyword_terms=parse_terms(keyword_terms),
        from_year=from_year,
        to_year=to_year,
        use_um_profile=use_um_profile,
    )
    client = OpenAlexClient()
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.DISCOVER, pipeline_run_id)
        profile = build_discovery_profile(repo.list_um_dataset_records()) if use_um_profile else None
        discovery_queries = build_openalex_discovery_queries(
            query=query,
            options=options,
            profile=profile,
            open_access_only=open_access_only,
        )
        fetched: list[dict[str, Any]] = []
        query_payloads = []

        def fetch_query(discovery_query: DiscoveryQuery) -> None:
            per_query_limit = (
                100
                if discovery_query.reason in {"um_dataset_citation", "um_dataset_link", "um_dataset_reuse_search"}
                else min(100, max(10, limit))
            )
            papers = client.search_works(
                query=discovery_query.query,
                limit=per_query_limit,
                filters=discovery_query.filters,
                sort=discovery_query.sort,
            )
            for paper in papers:
                reasons = list(paper.get("discovery_reasons") or [])
                if discovery_query.reason not in reasons:
                    reasons.append(discovery_query.reason)
                paper["discovery_reasons"] = reasons
            fetched.extend(papers)
            query_payloads.append(
                {
                    "reason": discovery_query.reason,
                    "query": discovery_query.query,
                    "filters": discovery_query.filters,
                    "seed_count": len(discovery_query.seed_work_ids),
                    "fetched": len(papers),
                }
            )

        for discovery_query in discovery_queries:
            fetch_query(discovery_query)
        if profile:
            source_ids = {
                str(paper["paperId"]) for paper in fetched
                if paper.get("paperId") and profile.catalog.exclusion_reason(paper) == "source_title"
            }
            seeds = sorted(source_ids - profile.catalog.work_ids - set(profile.related_work_ids))
            for offset in range(0, len(seeds), 100):
                batch = tuple(seeds[offset:offset + 100])
                fetch_query(DiscoveryQuery(
                    query=REUSE_SEARCH,
                    filters={**discovery_queries[0].filters, "cites": batch},
                    reason="source_paper_citation",
                    seed_work_ids=batch,
                ))
        ranked = dedupe_and_score_publications(fetched, query, options, profile)
        ranked_ids = [openalex_work_id(paper.get("paperId") or paper.get("id")) for paper in ranked]
        processed_ids = repo.processed_discovery_paper_ids(ranked_ids, pipeline_run_id)
        papers = [
            paper for paper, paper_id in zip(ranked, ranked_ids, strict=True)
            if paper_id not in processed_ids
        ][:limit]
        inserted = repo.upsert_publications(papers, source="openalex")
        result = _stage_result(
            PipelineStage.DISCOVER,
            "successful" if papers else "skipped",
            inserted,
            f"Discovered {len(papers)} publications."
            + (f" Skipped {len(processed_ids)} previously processed publications." if processed_ids else ""),
            {
                "query": query,
                "provider": "openalex",
                "fetched": len(papers),
                "processed_skipped": len(processed_ids),
                "openalex_queries": query_payloads,
                "use_um_profile": use_um_profile,
                "paper_ids": [
                    paper.get("paperId") or paper.get("id")
                    for paper in papers
                    if paper.get("paperId") or paper.get("id")
                ],
            },
        )
        repo.record_stage_result(PipelineStage.DISCOVER, result["status"], result, pipeline_run_id)
        return result


def download_pdf_batch(
    limit: int | None = None,
    overwrite: bool = False,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        run = repo.get_pipeline_run(pipeline_run_id) if pipeline_run_id is not None else None
        config = (run or {}).get("config") or {}
        if pipeline_run_id is not None and config.get("validated_pdf_target"):
            # Serialize redelivery of the same run across the downloader's per-paper commits.
            repo.cursor.execute("SELECT pg_advisory_lock(73016,%s)", (pipeline_run_id,))
            repo.record_stage_start(PipelineStage.DOWNLOAD_PDF, pipeline_run_id)
            target = int(config["processing_limit"])
            downloader = PDFDownloader(output_dir=PDF_DIR)
            while paper := repo.next_pool_download(pipeline_run_id, target):
                results = downloader.download_papers([paper], overwrite=overwrite)
                repo.persist_download_results(results, pipeline_run_id)
            counts = repo.pool_download_counts(pipeline_run_id)
            shortfall = target - counts["validated"]
            result = _stage_result(
                PipelineStage.DOWNLOAD_PDF, "completed_with_errors" if shortfall else "successful",
                counts["validated"],
                f"Validated {counts['validated']} of {target} requested PDFs. "
                f"Attempted {counts['attempted']}, including {counts['replacements']} replacements. "
                f"{counts['failed']} downloads failed. Unfilled slots: {shortfall}.",
                {**counts, "requested": target, "unfilled": shortfall, "successful": counts["validated"]},
            )
            repo.record_stage_result(PipelineStage.DOWNLOAD_PDF, result["status"], result, pipeline_run_id)
            return result
        repo.record_stage_start(PipelineStage.DOWNLOAD_PDF, pipeline_run_id)
        downloader = PDFDownloader(output_dir=PDF_DIR)
        papers = repo.get_papers_needing_download(limit=limit, pipeline_run_id=pipeline_run_id)
        results = downloader.download_papers(papers, overwrite=overwrite)
        repo.persist_download_results(results, pipeline_run_id)
        result = _result_from_operation(PipelineStage.DOWNLOAD_PDF, results)
        repo.record_stage_result(PipelineStage.DOWNLOAD_PDF, result["status"], result, pipeline_run_id)
        return result


def grobid_convert_batch(
    limit: int | None = None,
    overwrite: bool = False,
    delete_pdf: bool = False,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.GROBID_CONVERT, pipeline_run_id)
        converter = GrobidConverter(output_dir=XML_DIR)
        papers = repo.get_papers_needing_conversion(limit=limit, pipeline_run_id=pipeline_run_id)
        results = converter.convert_papers(papers, overwrite=overwrite, delete_pdf=delete_pdf)
        repo.persist_conversion_results(results, pipeline_run_id)
        result = _result_from_operation(PipelineStage.GROBID_CONVERT, results)
        repo.record_stage_result(PipelineStage.GROBID_CONVERT, result["status"], result, pipeline_run_id)
        return result


def render_document_batch(
    limit: int | None = None,
    overwrite: bool = False,
    pipeline_run_id: int | None = None,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.RENDER_DOCUMENT, pipeline_run_id)
        papers = repo.get_papers_needing_rendering(limit=limit, pipeline_run_id=pipeline_run_id)
        run = repo.get_pipeline_run(pipeline_run_id) if pipeline_run_id is not None else None
        version = ((run or {}).get("config") or {}).get("renderer_version") or RENDERER_VERSION
        results = render_papers(
            papers, overwrite=overwrite, profile=profile, version=version,
        )
        repo.persist_render_results(results, pipeline_run_id)
        result = _result_from_operation(PipelineStage.RENDER_DOCUMENT, results)
        repo.record_stage_result(PipelineStage.RENDER_DOCUMENT, result["status"], result, pipeline_run_id)
        return result


def detect_mentions_batch(
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.DETECT_MENTIONS, pipeline_run_id)
        run = repo.get_pipeline_run(pipeline_run_id) if pipeline_run_id is not None else None
        run_config = (run or {}).get("config") or {}
        artifacts = repo.get_markdown_artifacts_needing_detection(
            detector_version=SCREENING_VERSION,
            limit=limit,
            pipeline_run_id=pipeline_run_id,
            render_profile=run_config.get("render_profile"),
            renderer_version=run_config.get("renderer_version"),
            include_cached=pipeline_run_id is not None and "stage" not in run_config,
        )
        counts = dict.fromkeys(("likely_use", "uncertain", "no_use", "failed"), 0)
        reused = 0
        client = GatewayClient()
        try:
            for artifact in artifacts:
                try:
                    metrics, cached = _screen_artifact(repo, client, artifact)
                except Exception:
                    repo.conn.rollback()
                    counts["failed"] += 1
                else:
                    counts[metrics["decision"]] += 1
                    reused += int(cached)
        finally:
            client.close()
        forwarded = counts["likely_use"] + counts["uncertain"]
        completed = forwarded + counts["no_use"]
        new = completed - reused
        noun = "publication" if completed == 1 else "publications"
        status = "successful"
        if counts["failed"]:
            status = "completed_with_errors" if completed else "failed"
        result = _stage_result(
            PipelineStage.DETECT_MENTIONS,
            status,
            completed,
            f"Screening complete for {completed} {noun} ({new} new, {reused} reused). "
            f"Forwarded {forwarded} for extraction.",
            {
                "candidates": 0,
                "documents": len(artifacts),
                "failed_documents": counts["failed"],
                "new_decisions": new,
                "reused_decisions": reused,
                "decisions": counts,
                "forwarded": forwarded,
                "detector_version": SCREENING_VERSION,
            },
        )
        repo.record_stage_result(PipelineStage.DETECT_MENTIONS, result["status"], result, pipeline_run_id)
        return result


def _screen_artifact(
    repo: PipelineRepository, client: GatewayClient, artifact: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    digest = artifact["render_sha256"]
    key = (artifact["publication_row_id"], artifact["markdown_artifact_id"], digest, SCREENING_VERSION)
    existing = repo.get_mention_screening(*key)
    if existing:
        return existing, True
    repo.conn.commit()
    metrics = {"paper_id": artifact["paper_id"], "detector_version": SCREENING_VERSION,
               "render_sha256": digest, "candidates": 0}
    try:
        metrics.update(screen_publication(
            client, Path(artifact["path"]), digest, artifact.get("markdown_metadata") or {},
            Path(artifact["xml_path"]) if artifact.get("xml_path") else None,
        ))
    except Exception as exc:
        concurrent = repo.record_mention_screening(*key, metrics, error=str(exc))
        if "decision" in concurrent:
            return concurrent, True
        raise
    return repo.record_mention_screening(*key, metrics), False


def extract_features_from_candidates(
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    from datasight.application.llm_extraction import extract_run

    return extract_run(pipeline_run_id, limit)


def import_um_datasets(path: str) -> dict[str, Any]:
    source = _project_path(path)
    authoritative = looks_like_openalex_export(source)
    if authoritative:
        bundle = load_um_openalex_export_bundle(source)
        records = bundle.records
        warnings = bundle.warnings
        metrics = bundle.metrics
    else:
        records = load_um_dataset_records(path)
        warnings = []
        metrics = {"source_rows": len(records)}
    if not records:
        raise ValueError(f"No UM dataset records were found in {source}.")
    with PipelineRepository() as repo:
        if authoritative:
            count, deleted = repo.sync_um_datasets(records)
        else:
            count = repo.upsert_um_datasets(records)
            deleted = 0
    return {
        "status": "successful" if count else "skipped",
        "count": count,
        "deleted": deleted,
        "path": path,
        "warnings": warnings,
        "metrics": metrics,
    }


def _match_mentions(records: list[UMDatasetRecord], rows: list[dict[str, Any]]) -> list[UMMatchDecision]:
    if not rows:
        return []
    matcher = UMCatalogMatcher(records)
    decisions = []
    for row in rows:
        mention = DatasetMention.model_validate({**row, "aliases": row["aliases"] or []})
        decision = matcher.match(mention)
        decision.mention_id = str(row["mention_id"])
        decisions.append(decision)
    return decisions


def match_um_dataset_batch(
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.MATCH_UM_DATASET, pipeline_run_id)
        records = repo.list_um_dataset_records(for_matching=True)
        if not records:
            raise ValueError("No UM datasets are imported. Run import-um-datasets first.")

        mention_rows = repo.get_unmatched_mentions(limit=limit, pipeline_run_id=pipeline_run_id)
        decisions = _match_mentions(records, mention_rows)

        inserted = repo.persist_match_decisions(decisions)
        result = _stage_result(
            PipelineStage.MATCH_UM_DATASET,
            "successful" if inserted else "skipped",
            inserted,
            f"Matched {inserted} mentions against UM metadata candidates.",
            {"decisions": inserted},
        )
        repo.record_stage_result(PipelineStage.MATCH_UM_DATASET, result["status"], result, pipeline_run_id)
        return result


def download_pipeline_item(
    item_id: int,
    overwrite: bool = False,
    task_id: str | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not claimed and not repo.start_item_stage(item_id, PipelineStage.DOWNLOAD_PDF, task_id):
            return _item_result(PipelineStage.DOWNLOAD_PDF, "skipped", item_id, "Item stage was already terminal.")
        context = repo.get_pipeline_item_context(item_id)
        if not context:
            return _missing_item_result(repo, item_id, PipelineStage.DOWNLOAD_PDF)
        downloader = PDFDownloader(output_dir=PDF_DIR, delay=0)
        from datasight.infrastructure.ingestion.downloader import pdf_locations
        repo.cursor.execute('SELECT raw FROM publications WHERE paper_id=%s', (context['paper_id'],))
        raw = (repo.cursor.fetchone() or {}).get('raw') or {}
        result = downloader.download_paper(
            paper_id=context["paper_id"],
            url=context.get("open_access_url"),
            title=context.get("title"),
            overwrite=overwrite,
            alternative_urls=pdf_locations(context.get('open_access_url'), raw),
        )
        repo.persist_download_results([result], context["pipeline_run_id"])
        status = "successful" if result.success else "failed"
        metrics = _json_safe(vars(result))
        repo.finish_item_stage(item_id, PipelineStage.DOWNLOAD_PDF, status, metrics, result.error)
        return _item_result(PipelineStage.DOWNLOAD_PDF, status, item_id, result.message, metrics)


def grobid_convert_pipeline_item(
    item_id: int,
    overwrite: bool = False,
    delete_pdf: bool = False,
    task_id: str | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not claimed and not repo.start_item_stage(item_id, PipelineStage.GROBID_CONVERT, task_id):
            return _item_result(PipelineStage.GROBID_CONVERT, "skipped", item_id, "Item stage was already terminal.")
        context = repo.get_pipeline_item_context(item_id)
        if not context:
            return _missing_item_result(repo, item_id, PipelineStage.GROBID_CONVERT)
        if not context.get("pdf_path"):
            message = "PDF artifact is missing."
            repo.finish_item_stage(item_id, PipelineStage.GROBID_CONVERT, "failed", {"message": message}, message)
            return _item_result(PipelineStage.GROBID_CONVERT, "failed", item_id, message)
        try:
            converter = GrobidConverter(output_dir=XML_DIR, delay=0)
            converter.ensure_available()
            result = converter.convert_pdf(
                pdf_path=Path(context["pdf_path"]),
                paper_id=context["paper_id"],
                overwrite=overwrite,
                delete_pdf=delete_pdf,
            )
        except Exception as exc:
            message = f"GROBID conversion error: {exc}"
            repo.finish_item_stage(item_id, PipelineStage.GROBID_CONVERT, "failed", {"message": message}, str(exc))
            return _item_result(PipelineStage.GROBID_CONVERT, "failed", item_id, message)
        repo.persist_conversion_results([result], context["pipeline_run_id"])
        status = "successful" if result.success else "failed"
        metrics = _json_safe(vars(result))
        repo.finish_item_stage(item_id, PipelineStage.GROBID_CONVERT, status, metrics, result.error)
        return _item_result(PipelineStage.GROBID_CONVERT, status, item_id, result.message, metrics)


def render_pipeline_item(
    item_id: int,
    overwrite: bool = False,
    task_id: str | None = None,
    claimed: bool = False,
    profile: RenderProfile | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not claimed and not repo.start_item_stage(item_id, PipelineStage.RENDER_DOCUMENT, task_id):
            return _item_result(PipelineStage.RENDER_DOCUMENT, "skipped", item_id, "Item stage was already terminal.")
        context = repo.get_pipeline_item_context(item_id)
        if not context:
            return _missing_item_result(repo, item_id, PipelineStage.RENDER_DOCUMENT)
        if not context.get("xml_path"):
            message = "TEI XML artifact is missing."
            repo.finish_item_stage(item_id, PipelineStage.RENDER_DOCUMENT, "failed", {"message": message}, message)
            return _item_result(PipelineStage.RENDER_DOCUMENT, "failed", item_id, message)
        configured_profile = context.get("render_profile")
        effective_profile: RenderProfile = (
            profile
            if profile is not None
            else configured_profile
            if configured_profile in {"full_body", "pruned"}
            else DEFAULT_RENDER_PROFILE
        )
        try:
            result = render_to_markdown(
                Path(context["xml_path"]),
                paper_id=context["paper_id"],
                overwrite=overwrite,
                profile=effective_profile,
                version=context.get("renderer_version") or RENDERER_VERSION,
            )
        except Exception as exc:
            message = f"Render error: {exc}"
            repo.finish_item_stage(item_id, PipelineStage.RENDER_DOCUMENT, "failed", {"message": message}, str(exc))
            return _item_result(PipelineStage.RENDER_DOCUMENT, "failed", item_id, message)
        repo.persist_render_results([result], context["pipeline_run_id"])
        status = "successful" if result.success else "failed"
        metrics = _json_safe(vars(result))
        repo.finish_item_stage(item_id, PipelineStage.RENDER_DOCUMENT, status, metrics, result.error)
        return _item_result(PipelineStage.RENDER_DOCUMENT, status, item_id, result.message, metrics)


def detect_mentions_pipeline_item(
    item_id: int,
    task_id: str | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not claimed and not repo.start_item_stage(item_id, PipelineStage.DETECT_MENTIONS, task_id):
            return _item_result(PipelineStage.DETECT_MENTIONS, "skipped", item_id, "Item stage was already terminal.")
        context = repo.get_pipeline_item_context(item_id)
        if not context:
            return _missing_item_result(repo, item_id, PipelineStage.DETECT_MENTIONS)
        if not context.get("markdown_path"):
            message = "Markdown artifact is missing."
            repo.finish_item_stage(item_id, PipelineStage.DETECT_MENTIONS, "failed", {"message": message}, message)
            return _item_result(PipelineStage.DETECT_MENTIONS, "failed", item_id, message)
        if not context.get("markdown_artifact_id") or not context.get("markdown_sha256"):
            message = "Markdown lineage is missing; rerender the document before detection."
            repo.finish_item_stage(item_id, PipelineStage.DETECT_MENTIONS, "failed", {"message": message}, message)
            return _item_result(PipelineStage.DETECT_MENTIONS, "failed", item_id, message)
        client = GatewayClient()
        try:
            metrics, reused = _screen_artifact(repo, client, {
                **context,
                "path": context["markdown_path"],
                "render_sha256": context["markdown_sha256"],
                "xml_path": context.get("screening_xml_path"),
            })
        except Exception as exc:
            message = f"Mention detection error: {exc}"
            repo.conn.rollback()
            repo.finish_item_stage(item_id, PipelineStage.DETECT_MENTIONS, "failed", {"message": message}, str(exc))
            return _item_result(PipelineStage.DETECT_MENTIONS, "failed", item_id, message)
        finally:
            client.close()
        metrics = {**metrics, "screening_reused": reused}
        repo.finish_item_stage(item_id, PipelineStage.DETECT_MENTIONS, "successful", metrics)
        return _item_result(
            PipelineStage.DETECT_MENTIONS,
            "successful",
            item_id,
            "Publication screened out." if metrics["decision"] == "no_use" else "Publication forwarded for extraction.",
            metrics,
        )


def extract_features_pipeline_item(
    item_id: int,
    task_id: str | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not claimed and not repo.start_item_stage(item_id, PipelineStage.EXTRACT_FEATURES, task_id):
            return _item_result(PipelineStage.EXTRACT_FEATURES, "skipped", item_id, "Item stage was already terminal.")
        context = repo.get_pipeline_item_context(item_id)
        if not context:
            return _missing_item_result(repo, item_id, PipelineStage.EXTRACT_FEATURES)
    from datasight.application.llm_extraction import extract_run

    result = extract_run(context["pipeline_run_id"], publication_id=context["publication_row_id"])
    with PipelineRepository() as repo:
        status = result["status"]
        item_status = status if status in {"successful", "interrupted", "skipped"} else "failed"
        repo.finish_item_stage(item_id, PipelineStage.EXTRACT_FEATURES, item_status, result)
    return {**result, "item_id": item_id}


def export_insights_csv(
    output_path: str,
    rows: list[dict[str, Any]] | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    with PipelineRepository() as repo:
        repo.record_stage_start(PipelineStage.EXPORT_INSIGHTS, pipeline_run_id)
        export_rows = (
            rows if rows is not None else repo.export_insight_rows(pipeline_run_id=pipeline_run_id)
        )
        path = _project_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            handle.write(serialize_insights_csv(export_rows, INSIGHT_COLUMNS))
        result = _stage_result(
            PipelineStage.EXPORT_INSIGHTS,
            "successful",
            len(export_rows),
            f"Exported insights to {path}.",
            {"output_path": str(path)},
        )
        repo.record_stage_result(PipelineStage.EXPORT_INSIGHTS, result["status"], result, pipeline_run_id)
        return result


EVALUATION_PAPER_COLUMNS = [
    "run_id", "run_key", "run_created_at", "run_config", "publication_row_id",
    "paper_id", "title", "doi", "year", "language", "publication_type",
    "source_url", "open_access_url", "discovery_score", "discovery_evidence_tier",
    "evidence_reasons", "matched_um_dataset_ids", "pdf_url_available",
    "download_status", "download_failure_category", "download_checked_at",
    "pipeline_item_status", "stage_statuses", "pdf_path", "pdf_sha256",
    "pdf_metadata", "tei_path", "tei_sha256", "tei_metadata", "markdown_path",
    "markdown_sha256", "markdown_metadata", "detection_status", "detector_version",
    "candidate_count", "detection_metrics", "detection_error",
]
EVALUATION_CANDIDATE_COLUMNS = [
    "run_id", "paper_id", "title", "doi", "discovery_score",
    "discovery_evidence_tier", "markdown_artifact_id", "markdown_path",
    "render_sha256", "renderer_version", "detection_run_id", "detector_version",
    "detected_at", "candidate_id", "dataset_name", "evidence_tier",
    "trigger_type", "trigger_text", "triggers", "evidence_text", "section_heading",
    "standardized_section", "char_start", "char_end", "legacy_tier_score", "source",
]


def export_evaluation_bundle(pipeline_run_id: int, output_dir: str) -> dict[str, Any]:
    """Write run-scoped detection and extraction evidence."""
    with PipelineRepository() as repo:
        run = repo.get_pipeline_run(pipeline_run_id)
        if run is None:
            raise ValueError(f"Pipeline run {pipeline_run_id} does not exist")
        papers = repo.evaluation_paper_rows(pipeline_run_id)
        candidates = repo.evaluation_candidate_rows(pipeline_run_id)
        extractions = repo.evaluation_extraction_rows(pipeline_run_id)
        request_attempts = repo.evaluation_request_attempt_rows(pipeline_run_id)

    destination = _project_path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    papers_path = destination / "papers.csv"
    candidates_path = destination / "candidates.csv"
    _write_dict_rows(papers_path, papers, EVALUATION_PAPER_COLUMNS)
    _write_dict_rows(candidates_path, candidates, EVALUATION_CANDIDATE_COLUMNS)
    extraction_columns = ["paper_id", "extraction_id", "status", "dataset_count", "source_sha256", "source_path", "fingerprint", "config", "coverage", "metrics", "error"]
    _write_dict_rows(destination / "extractions.csv", [
        {**row, "dataset_count": len((row.get("result") or {}).get("datasets", []))} for row in extractions
    ], extraction_columns)
    _write_dict_rows(destination / "request_attempts.csv", request_attempts, [
        "paper_id", "extraction_id", "work_key", "request_status", "attempt_number",
        "http_status", "elapsed_seconds", "prompt_tokens", "completion_tokens",
        "total_tokens", "error_category",
    ])
    datasets, occurrences, observations = [], [], []
    for extraction in extractions:
        for dataset in (extraction.get("result") or {}).get("datasets", []):
            identity = {"paper_id": extraction["paper_id"], "extraction_id": extraction["extraction_id"], "dataset_local_id": dataset["dataset_local_id"]}
            datasets.append({**identity, "display_name": dataset["display_name"], "features": dataset})
            for occurrence in dataset["occurrences"]:
                occurrences.append({**identity, **occurrence})
            for group in ("identity", "use", "findability", "accessibility", "interoperability", "reusability"):
                for field, observed in dataset.get(group, {}).items():
                    observations.append({**identity, "field": f"{group}.{field}", **observed})
    identity_columns = ["paper_id", "extraction_id", "dataset_local_id"]
    _write_dict_rows(destination / "datasets.csv", datasets, [*identity_columns, "display_name", "features"])
    _write_dict_rows(destination / "occurrences.csv", occurrences, [*identity_columns, "occurrence_id", "evidence_ids", "use", "citation", "citation_links", "bibliography_only"])
    _write_dict_rows(destination / "field_observations.csv", observations, [*identity_columns, "field", "state", "values"])
    (destination / "extractions.json").write_text(json.dumps(_json_safe(extractions), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "successful",
        "run_id": pipeline_run_id,
        "papers": len(papers),
        "candidates": len(candidates),
        "detector_version": (run.get("config") or {}).get("detector_version", DETECTOR_VERSION),
        "papers_path": str(papers_path),
        "candidates_path": str(candidates_path),
        "extractions_path": str(destination / "extractions.json"),
        "request_attempts_path": str(destination / "request_attempts.csv"),
    }


def _write_dict_rows(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_cell(row.get(column)) for column in columns})


def load_um_dataset_records(path: str) -> list[UMDatasetRecord]:
    source = _project_path(path)
    if looks_like_openalex_export(source):
        return load_um_openalex_exports(source)

    if source.suffix.lower() == ".csv":
        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return [UMDatasetRecord.model_validate(_coerce_csv_record(row)) for row in rows]

    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("datasets", [data])
    return [UMDatasetRecord.model_validate(item) for item in data]


def _result_from_operation(stage: PipelineStage, results: list[Any]) -> dict[str, Any]:
    successful = sum(1 for result in results if result.success)
    failed = sum(1 for result in results if not result.success)
    if successful and failed:
        status = "completed_with_errors"
    elif successful:
        status = "successful"
    elif failed:
        status = "failed"
    else:
        status = "skipped"
    return _stage_result(
        stage,
        status,
        len(results),
        f"{stage.value}: {successful} successful, {failed} failed.",
        {
            "successful": successful,
            "failed": failed,
            "results": [_json_safe(vars(result)) for result in results],
        },
    )


def _stage_result(
    stage: PipelineStage,
    status: Literal["successful", "completed_with_errors", "failed", "skipped"],
    count: int,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return StageResult(
        stage=stage,
        status=status,
        count=count,
        message=message,
        payload=payload or {},
    ).model_dump(mode="json")


def _item_result(
    stage: PipelineStage,
    status: Literal["successful", "failed", "skipped"],
    item_id: int,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage.value,
        "status": status,
        "item_id": item_id,
        "message": message,
        "payload": payload or {},
    }


def _missing_item_result(repo: PipelineRepository, item_id: int, stage: PipelineStage) -> dict[str, Any]:
    message = f"Pipeline item {item_id} was not found."
    repo.finish_item_stage(item_id, stage, "failed", {"message": message}, message)
    return _item_result(stage, "failed", item_id, message)


def _coerce_csv_record(row: dict[str, str]) -> dict[str, Any]:
    coerced: dict[str, Any] = dict(row)
    for list_key in ("aliases", "creators", "keywords"):
        value = coerced.get(list_key)
        if isinstance(value, str):
            coerced[list_key] = [part.strip() for part in value.split(";") if part.strip()]
    if coerced.get("year"):
        coerced["year"] = int(coerced["year"])
    coerced["raw"] = dict(row)
    return coerced


def _project_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return PROJECT_ROOT / resolved


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
