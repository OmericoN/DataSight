"""Database-backed high-throughput pipeline execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from datasight.application import pipeline_services as services
from datasight.config import (
    HIGH_THROUGHPUT_MAX_BATCHES_PER_DISPATCH,
    HIGH_THROUGHPUT_STAGE_BATCH_SIZE,
)
from datasight.domain.stages import PipelineStage
from datasight.infrastructure.persistence.items import ITEM_STAGES
from datasight.infrastructure.persistence.repository import PipelineRepository

STAGE_QUEUES: dict[PipelineStage, str] = {
    PipelineStage.DOWNLOAD_PDF: "download",
    PipelineStage.GROBID_CONVERT: "grobid",
    PipelineStage.RENDER_DOCUMENT: "processing",
    PipelineStage.DETECT_MENTIONS: "processing",
    PipelineStage.EXTRACT_FEATURES: "llm",
}


@dataclass(frozen=True)
class StageBatch:
    stage: PipelineStage
    queue: str
    batch_size: int


@dataclass(frozen=True)
class DispatchPlan:
    batches: tuple[StageBatch, ...]
    final_status: str | None = None


def bootstrap_high_throughput_run(
    preview_id: str,
    processing_limit: int,
    pipeline_run_id: int | None = None,
    overwrite: bool = False,
    excluded_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    if pipeline_run_id is None:
        raise ValueError("pipeline_run_id is required for high-throughput bootstrap.")
    result = services.discover_publications(
        query="preview-backed discovery",
        limit=processing_limit,
        pipeline_run_id=pipeline_run_id,
        preview_id=preview_id,
        processing_limit=processing_limit,
        excluded_candidate_ids=excluded_candidate_ids,
    )
    payload = result.get("payload", {})
    raw_paper_ids = payload.get("paper_ids", []) if isinstance(payload, dict) else []
    paper_ids = [paper_id for paper_id in raw_paper_ids if isinstance(paper_id, str)]
    with PipelineRepository() as repo:
        item_ids = repo.create_pipeline_items(pipeline_run_id, paper_ids)
        queued = (
            repo.queue_item_stage_for_run(pipeline_run_id, PipelineStage.DOWNLOAD_PDF)
            if item_ids
            else 0
        )
    return {
        "pipeline_run_id": pipeline_run_id,
        "items": len(item_ids),
        "queued": queued,
        "overwrite": overwrite,
        "status": "queued" if queued else "skipped",
    }


def build_dispatch_plan(
    pipeline_run_id: int,
    batch_size: int = HIGH_THROUGHPUT_STAGE_BATCH_SIZE,
    max_batches: int = HIGH_THROUGHPUT_MAX_BATCHES_PER_DISPATCH,
) -> DispatchPlan:
    with PipelineRepository() as repo:
        run = repo.get_pipeline_run(pipeline_run_id)
        interrupted = bool(run and run["status"] == "interrupted")
        if repo.all_item_stages_terminal(pipeline_run_id):
            status = repo.finalize_high_throughput_run_if_ready(pipeline_run_id)
            return DispatchPlan(batches=(), final_status=status)

        queued_counts = repo.get_queued_item_stage_counts(pipeline_run_id)

    batches: list[StageBatch] = []
    safe_batch_size = max(1, batch_size)
    safe_max_batches = max(1, max_batches)
    for stage in ITEM_STAGES:
        if interrupted and stage == PipelineStage.EXTRACT_FEATURES:
            continue
        queued = queued_counts.get(stage.value, 0)
        if queued <= 0:
            continue
        stage_batch_size = 1 if stage == PipelineStage.EXTRACT_FEATURES else safe_batch_size
        batch_count = min(safe_max_batches, ceil(queued / stage_batch_size))
        batches.extend(
            StageBatch(
                stage=stage, queue=STAGE_QUEUES[stage], batch_size=stage_batch_size
            )
            for _ in range(batch_count)
        )
    return DispatchPlan(batches=tuple(batches))


def process_high_throughput_stage(
    pipeline_run_id: int,
    stage: PipelineStage | str,
    batch_size: int = HIGH_THROUGHPUT_STAGE_BATCH_SIZE,
    overwrite: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    stage_value = PipelineStage(stage)
    with PipelineRepository() as repo:
        item_ids = repo.claim_queued_item_stages(
            pipeline_run_id=pipeline_run_id,
            stage=stage_value,
            limit=1 if stage_value == PipelineStage.EXTRACT_FEATURES else max(1, batch_size),
            task_id=task_id,
        )

    results: list[dict[str, Any]] = []
    for item_id in item_ids:
        try:
            results.append(
                _process_claimed_item_stage(item_id, stage_value, overwrite, task_id)
            )
            if results[-1]["status"] == "interrupted":
                break
        except (
            Exception
        ) as exc:  # pragma: no cover - defensive boundary around per-item work
            message = f"{stage_value.value} item processing error: {exc}"
            with PipelineRepository() as repo:
                repo.finish_item_stage(
                    item_id, stage_value, "failed", {"message": message}, str(exc)
                )
            results.append(
                {
                    "stage": stage_value.value,
                    "status": "failed",
                    "item_id": item_id,
                    "message": message,
                    "payload": {"error": str(exc)},
                }
            )

    return {
        "pipeline_run_id": pipeline_run_id,
        "stage": stage_value.value,
        "claimed": len(item_ids),
        "successful": sum(result["status"] == "successful" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
        "results": results,
    }


def finalize_high_throughput_run(
    output_path: str, pipeline_run_id: int | None = None
) -> dict[str, Any]:
    if pipeline_run_id is None:
        raise ValueError(
            "pipeline_run_id is required for high-throughput finalization."
        )
    with PipelineRepository() as repo:
        status = repo.finalize_high_throughput_run_if_ready(pipeline_run_id)
    return {
        "pipeline_run_id": pipeline_run_id,
        "status": status or "not_ready",
        "output_path": output_path,
    }


def _process_claimed_item_stage(
    item_id: int,
    stage: PipelineStage,
    overwrite: bool,
    task_id: str | None,
) -> dict[str, Any]:
    if stage == PipelineStage.DOWNLOAD_PDF:
        return services.download_pipeline_item(
            item_id, overwrite=overwrite, task_id=task_id, claimed=True
        )
    if stage == PipelineStage.GROBID_CONVERT:
        return services.grobid_convert_pipeline_item(
            item_id,
            overwrite=overwrite,
            delete_pdf=False,
            task_id=task_id,
            claimed=True,
        )
    if stage == PipelineStage.RENDER_DOCUMENT:
        return services.render_pipeline_item(
            item_id, overwrite=overwrite, task_id=task_id, claimed=True
        )
    if stage == PipelineStage.DETECT_MENTIONS:
        return services.detect_mentions_pipeline_item(
            item_id, task_id=task_id, claimed=True
        )
    if stage == PipelineStage.EXTRACT_FEATURES:
        return services.extract_features_pipeline_item(
            item_id, task_id=task_id, claimed=True
        )
    raise ValueError(f"Unsupported high-throughput item stage: {stage.value}")
