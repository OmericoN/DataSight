"""Celery task entrypoints for high-throughput stage execution."""

from __future__ import annotations

from typing import Any

from celery.signals import task_failure

from datasight.application import high_throughput
from datasight.application import pipeline_services as services
from datasight.infrastructure.ingestion.renderer import DEFAULT_RENDER_PROFILE, RenderProfile
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.infrastructure.worker.celery_app import celery_app


@celery_app.task(name="datasight.discover_publications", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def discover_publications(
    self,
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
    return services.discover_publications(
        query=query,
        limit=limit,
        open_access_only=open_access_only,
        topic_ids=topic_ids,
        keyword_terms=keyword_terms,
        from_year=from_year,
        to_year=to_year,
        use_um_profile=use_um_profile,
        pipeline_run_id=pipeline_run_id,
        preview_id=preview_id,
        processing_limit=processing_limit,
        excluded_candidate_ids=excluded_candidate_ids,
    )


@celery_app.task(name="datasight.download_pdf", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def download_pdf(
    self,
    limit: int | None = None,
    overwrite: bool = False,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.download_pdf_batch(limit=limit, overwrite=overwrite, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.grobid_convert", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def grobid_convert(
    self,
    limit: int | None = None,
    overwrite: bool = False,
    delete_pdf: bool = False,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.grobid_convert_batch(
        limit=limit,
        overwrite=overwrite,
        delete_pdf=delete_pdf,
        pipeline_run_id=pipeline_run_id,
    )


@celery_app.task(name="datasight.render_document", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def render_document(
    self,
    limit: int | None = None,
    overwrite: bool = False,
    pipeline_run_id: int | None = None,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    return services.render_document_batch(
        limit=limit,
        overwrite=overwrite,
        pipeline_run_id=pipeline_run_id,
        profile=profile,
    )


@celery_app.task(name="datasight.detect_mentions", bind=True)
def detect_mentions(
    self,
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.detect_mentions_batch(limit=limit, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.extract_features", bind=True, reject_on_worker_lost=True)
def extract_features(
    self,
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.extract_features_from_candidates(limit=limit, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.resume_extractions", reject_on_worker_lost=True)
def resume_extractions(pipeline_run_id: int) -> dict[str, Any]:
    from datasight.application.llm_extraction import resume_extraction

    return resume_extraction(pipeline_run_id)


@celery_app.task(name="datasight.match_um_dataset", bind=True)
def match_um_dataset(
    self,
    limit: int | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.match_um_dataset_batch(limit=limit, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.export_insights", bind=True)
def export_insights(
    self,
    output_path: str,
    rows: list[dict[str, Any]] | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return services.export_insights_csv(output_path, rows, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.bootstrap_high_throughput_run", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def bootstrap_high_throughput_run(
    self,
    preview_id: str,
    processing_limit: int,
    output_path: str,
    overwrite: bool = False,
    pipeline_run_id: int | None = None,
    excluded_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    result = high_throughput.bootstrap_high_throughput_run(
        preview_id=preview_id,
        processing_limit=processing_limit,
        overwrite=overwrite,
        pipeline_run_id=pipeline_run_id,
        excluded_candidate_ids=excluded_candidate_ids,
    )
    dispatch_high_throughput_run.apply_async(
        kwargs={
            "pipeline_run_id": pipeline_run_id,
            "output_path": output_path,
            "overwrite": overwrite,
        },
        queue="processing",
    )
    return result


@celery_app.task(name="datasight.dispatch_high_throughput_run", bind=True)
def dispatch_high_throughput_run(
    self,
    pipeline_run_id: int,
    output_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    plan = high_throughput.build_dispatch_plan(pipeline_run_id)
    for batch in plan.batches:
        process_high_throughput_stage.apply_async(
            kwargs={
                "pipeline_run_id": pipeline_run_id,
                "stage": batch.stage.value,
                "batch_size": batch.batch_size,
                "overwrite": overwrite,
                "output_path": output_path,
            },
            queue=batch.queue,
        )
    return {
        "pipeline_run_id": pipeline_run_id,
        "batches": len(plan.batches),
        "finalize": plan.final_status is not None,
        "status": "queued" if plan.batches else (plan.final_status or "idle"),
    }


@celery_app.task(name="datasight.process_high_throughput_stage", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def process_high_throughput_stage(
    self,
    pipeline_run_id: int,
    stage: str,
    batch_size: int,
    overwrite: bool = False,
    output_path: str | None = None,
) -> dict[str, Any]:
    result = high_throughput.process_high_throughput_stage(
        pipeline_run_id=pipeline_run_id,
        stage=stage,
        batch_size=batch_size,
        overwrite=overwrite,
        task_id=self.request.id,
    )
    if output_path:
        dispatch_high_throughput_run.apply_async(
            kwargs={
                "pipeline_run_id": pipeline_run_id,
                "output_path": output_path,
                "overwrite": overwrite,
            },
            queue="processing",
        )
    return result


@celery_app.task(name="datasight.finalize_high_throughput_run", bind=True)
def finalize_high_throughput_run(
    self,
    output_path: str,
    pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    return high_throughput.finalize_high_throughput_run(output_path, pipeline_run_id=pipeline_run_id)


@celery_app.task(name="datasight.finish_pipeline_run", bind=True)
def finish_pipeline_run(self, pipeline_run_id: int, status: str) -> dict[str, Any]:
    with PipelineRepository() as repo:
        effective_status = (
            repo.standard_run_outcome(pipeline_run_id)
            if status == "successful"
            else status
        )
        repo.finish_pipeline_run(pipeline_run_id, effective_status)
    return {"pipeline_run_id": pipeline_run_id, "status": effective_status}


@task_failure.connect
def mark_pipeline_run_failed(
    sender=None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    if sender is None or not str(getattr(sender, "name", "")).startswith("datasight."):
        return
    pipeline_run_id = _extract_pipeline_run_id(args or (), kwargs or {})
    if pipeline_run_id is None:
        return
    error = str(exception) if exception else f"Task failed: {task_id}"
    with PipelineRepository() as repo:
        repo.fail_pipeline_run(pipeline_run_id, error)


def _extract_pipeline_run_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    value = kwargs.get("pipeline_run_id")
    if isinstance(value, int):
        return value
    for arg in reversed(args):
        if isinstance(arg, int):
            return arg
    return None
