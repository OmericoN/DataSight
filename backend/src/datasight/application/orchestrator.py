"""Full-pipeline orchestration. Production runs are always preview-backed."""

from __future__ import annotations

from typing import Any

from celery import chain

from datasight.application import pipeline_services as services
from datasight.application.discovery_preview import validate_discovery_preview
from datasight.config import DEFAULT_UM_DATASETS_PATH
from datasight.domain.run_strategy import RunStrategy
from datasight.domain.candidate_detection import DETECTOR_VERSION
from datasight.infrastructure.ingestion.renderer import (
    DEFAULT_RENDER_PROFILE,
    RENDERER_VERSION,
    RenderProfile,
)
from datasight.infrastructure.persistence.repository import PipelineRepository


def create_run(query: str, config: dict[str, Any]) -> int:
    with PipelineRepository() as repo:
        return repo.create_pipeline_run(query=query, config=config)


def run_all_local(
    preview_id: str,
    output: str,
    processing_limit: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
    um_datasets_path: str | None = DEFAULT_UM_DATASETS_PATH,
    overwrite: bool = False,
    render_profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    preview, effective_limit, query = _preview_context(preview_id, processing_limit)
    config = _run_config(
        preview_id=preview_id,
        processing_limit=effective_limit,
        output=output,
        overwrite=overwrite,
        um_datasets_path=um_datasets_path,
        strategy=RunStrategy.STANDARD,
        excluded_candidate_ids=excluded_candidate_ids,
        preview=preview,
        render_profile=render_profile,
    )
    pipeline_run_id = create_run(query, config)
    results: list[dict[str, Any]] = []
    try:
        results.extend(
            [
                services.discover_publications(
                    query=query,
                    limit=effective_limit,
                    pipeline_run_id=pipeline_run_id,
                    preview_id=preview_id,
                    processing_limit=effective_limit,
                    excluded_candidate_ids=excluded_candidate_ids,
                ),
                services.download_pdf_batch(
                    limit=effective_limit, overwrite=overwrite, pipeline_run_id=pipeline_run_id
                ),
                services.grobid_convert_batch(
                    limit=effective_limit, overwrite=overwrite, pipeline_run_id=pipeline_run_id
                ),
                services.render_document_batch(
                    limit=effective_limit,
                    overwrite=overwrite,
                    pipeline_run_id=pipeline_run_id,
                    profile=render_profile,
                ),
                services.detect_mentions_batch(limit=effective_limit, pipeline_run_id=pipeline_run_id),
                services.extract_features_from_candidates(
                    limit=effective_limit, pipeline_run_id=pipeline_run_id
                ),
            ]
        )
    except Exception as exc:
        _fail_run(pipeline_run_id, str(exc))
        raise
    status = _run_outcome(results)
    _finish_run(pipeline_run_id, status)
    return {"pipeline_run_id": pipeline_run_id, "status": status, "results": results}


def enqueue_run_all(
    preview_id: str,
    output: str,
    processing_limit: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
    um_datasets_path: str | None = DEFAULT_UM_DATASETS_PATH,
    overwrite: bool = False,
    strategy: RunStrategy | str = RunStrategy.STANDARD,
    render_profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    preview, effective_limit, query = _preview_context(preview_id, processing_limit)
    strategy_value = RunStrategy(strategy)
    if strategy_value == RunStrategy.HIGH_THROUGHPUT:
        return enqueue_high_throughput_run(
            preview_id=preview_id,
            output=output,
            processing_limit=effective_limit,
            excluded_candidate_ids=excluded_candidate_ids,
            um_datasets_path=um_datasets_path,
            overwrite=overwrite,
            preview=preview,
            query=query,
            render_profile=render_profile,
        )

    config = _run_config(
        preview_id=preview_id,
        processing_limit=effective_limit,
        output=output,
        overwrite=overwrite,
        um_datasets_path=um_datasets_path,
        strategy=strategy_value,
        excluded_candidate_ids=excluded_candidate_ids,
        preview=preview,
        render_profile=render_profile,
    )
    pipeline_run_id = create_run(query, config)
    try:
        from datasight.infrastructure.worker import tasks

        workflow = chain(
            tasks.discover_publications.si(
                query=query,
                limit=effective_limit,
                open_access_only=False,
                use_um_profile=False,
                pipeline_run_id=pipeline_run_id,
                preview_id=preview_id,
                processing_limit=effective_limit,
                excluded_candidate_ids=excluded_candidate_ids,
            ),
            tasks.download_pdf.si(effective_limit, overwrite, pipeline_run_id),
            tasks.grobid_convert.si(effective_limit, overwrite, False, pipeline_run_id),
            tasks.render_document.si(
                effective_limit, overwrite, pipeline_run_id, render_profile
            ),
            tasks.detect_mentions.si(effective_limit, pipeline_run_id),
            tasks.extract_features.si(effective_limit, pipeline_run_id),
            tasks.finish_pipeline_run.si(pipeline_run_id, "successful"),
        )
        async_result = workflow.apply_async()
    except Exception as exc:
        _fail_run(pipeline_run_id, str(exc))
        raise
    task_id = getattr(async_result, "id", None)
    with PipelineRepository() as repo:
        repo.update_pipeline_run_task_id(pipeline_run_id, task_id)
    return {"pipeline_run_id": pipeline_run_id, "task_id": task_id, "status": "queued"}


def enqueue_high_throughput_run(
    preview_id: str,
    output: str,
    processing_limit: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
    um_datasets_path: str | None = DEFAULT_UM_DATASETS_PATH,
    overwrite: bool = False,
    *,
    preview: dict[str, Any] | None = None,
    query: str | None = None,
    render_profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    if preview is None or query is None:
        preview, effective_limit, query = _preview_context(preview_id, processing_limit)
    else:
        effective_limit = int(processing_limit or 0)
    config = _run_config(
        preview_id=preview_id,
        processing_limit=effective_limit,
        output=output,
        overwrite=overwrite,
        um_datasets_path=um_datasets_path,
        strategy=RunStrategy.HIGH_THROUGHPUT,
        excluded_candidate_ids=excluded_candidate_ids,
        preview=preview,
        render_profile=render_profile,
    )
    pipeline_run_id = create_run(query, config)
    from datasight.infrastructure.worker import tasks

    try:
        async_result = tasks.bootstrap_high_throughput_run.apply_async(
            kwargs={
                "preview_id": preview_id,
                "processing_limit": effective_limit,
                "output_path": output,
                "overwrite": overwrite,
                "pipeline_run_id": pipeline_run_id,
                "excluded_candidate_ids": excluded_candidate_ids,
            },
            queue="processing",
        )
    except Exception as exc:
        _fail_run(pipeline_run_id, str(exc))
        raise
    task_id = getattr(async_result, "id", None)
    with PipelineRepository() as repo:
        repo.update_pipeline_run_task_id(pipeline_run_id, task_id)
    return {"pipeline_run_id": pipeline_run_id, "task_id": task_id, "status": "queued"}


def _preview_context(
    preview_id: str,
    processing_limit: int | None,
) -> tuple[dict[str, Any], int, str]:
    preview = validate_discovery_preview(preview_id, processing_limit)
    request = dict(preview.get("request") or {})
    effective_limit = int(processing_limit or request["processing_limit"])
    if request.get("mode") == "random":
        query = "OpenAlex random sample"
    else:
        query = str(
            request.get("manual_query") or request.get("focus_query") or "UM adaptive discovery"
        )
    return preview, effective_limit, query


def _run_config(
    *,
    preview_id: str,
    processing_limit: int,
    output: str,
    overwrite: bool,
    um_datasets_path: str | None,
    strategy: RunStrategy,
    excluded_candidate_ids: list[str] | None,
    preview: dict[str, Any],
    render_profile: RenderProfile,
) -> dict[str, Any]:
    from datasight.application.llm_extraction import extraction_configuration

    request = dict(preview.get("request") or {})
    return {
        "preview_id": preview_id,
        "strategy_version": request.get("strategy_version"),
        "discovery_mode": request.get("mode"),
        "random_seed": request.get("random_seed"),
        "language": request.get("language"),
        "discovery_filters": {
            "from_year": request.get("from_year"),
            "to_year": request.get("to_year"),
            "publication_types": request.get("publication_types") or [],
            "language": request.get("language"),
        },
        "strategy_fingerprint": (preview.get("payload") or {}).get("strategy_fingerprint"),
        "catalog_fingerprint": preview.get("catalog_fingerprint"),
        "openalex_snapshot_at": (preview.get("payload") or {}).get("provider_snapshot_at"),
        "openalex_calls": (preview.get("payload") or {}).get("actual_calls"),
        "openalex_cost_usd": (preview.get("payload") or {}).get("actual_cost_usd"),
        "openalex_queries": (preview.get("payload") or {}).get("executed_queries") or [],
        "code_version": (preview.get("payload") or {}).get("code_version"),
        "renderer_version": RENDERER_VERSION,
        "render_profile": render_profile,
        "detector_version": DETECTOR_VERSION,
        "extraction": extraction_configuration(),
        "processing_limit": processing_limit,
        "excluded_candidate_ids": excluded_candidate_ids or [],
        "output": output,
        "overwrite": overwrite,
        "um_datasets_path": um_datasets_path,
        "strategy": strategy.value,
    }


def _finish_run(pipeline_run_id: int, status: str) -> None:
    with PipelineRepository() as repo:
        repo.finish_pipeline_run(pipeline_run_id, status)


def _fail_run(pipeline_run_id: int, error: str) -> None:
    with PipelineRepository() as repo:
        repo.fail_pipeline_run(pipeline_run_id, error)


def _run_outcome(results: list[dict[str, Any]]) -> str:
    if any(result.get("status") == "interrupted" for result in results):
        return "interrupted"
    return (
        "completed_with_errors"
        if any(result.get("status") in {"failed", "completed_with_errors"} for result in results)
        else "successful"
    )
