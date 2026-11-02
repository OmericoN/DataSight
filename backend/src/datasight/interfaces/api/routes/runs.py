"""Pipeline run API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from datasight.interfaces.api.schemas import (
    PipelineRunEventSummary,
    PipelineRunSummary,
    RunCreateRequest,
    RunCreateResponse,
    RunEventsResponse,
    RunsResponse,
    StageRunCreateRequest,
)
from datasight.application import orchestrator
from datasight.application.discovery_preview import DiscoveryPreviewError, validate_discovery_preview
from datasight.application.stage_registry import (
    MissingStageArgument,
    PipelineStage,
    StageRunOptions,
    stage_args,
    stage_kwargs,
    task_for_stage,
)
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.infrastructure.worker import tasks

router = APIRouter(tags=["runs"])


@router.post("/runs", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def create_run(request: RunCreateRequest) -> RunCreateResponse:
    try:
        validate_discovery_preview(request.preview_id, request.processing_limit)
        result = orchestrator.enqueue_run_all(
            preview_id=request.preview_id,
            processing_limit=request.processing_limit,
            excluded_candidate_ids=request.excluded_candidate_ids,
            output=request.output_path,
            um_datasets_path=request.um_datasets_path,
            overwrite=request.overwrite,
            strategy=request.strategy,
            render_profile=request.render_profile,
        )
    except DiscoveryPreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"kind": exc.kind, "message": str(exc)},
        ) from exc
    return RunCreateResponse.model_validate(result)


@router.post("/stages/{stage}/runs", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def create_stage_run(stage: PipelineStage, request: StageRunCreateRequest) -> RunCreateResponse:
    if stage == PipelineStage.EXTRACT_FEATURES:
        raise HTTPException(status_code=422, detail="Extraction requires a selected publication set; use POST /runs/{run_id}/extractions/resume.")
    task = task_for_stage(stage, tasks)
    options = StageRunOptions(
        query=request.query,
        limit=request.limit,
        output_path=request.output_path,
        overwrite=request.overwrite,
        delete_pdf=request.delete_pdf,
        open_access_only=request.open_access_only,
        topic_ids=request.topic_ids,
        keyword_terms=request.keyword_terms,
        from_year=request.from_year,
        to_year=request.to_year,
        use_um_profile=request.use_um_profile,
        render_profile=request.render_profile,
    )
    try:
        args = stage_args(stage, options)
        kwargs = stage_kwargs(stage, options)
    except MissingStageArgument as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    with PipelineRepository() as repo:
        pipeline_run_id = repo.create_pipeline_run(
            query=request.query or stage.value,
            config={"stage": stage.value, **request.model_dump(mode="json")},
        )
    kwargs["pipeline_run_id"] = pipeline_run_id
    try:
        async_result = task.delay(*args, **kwargs)
    except Exception as exc:
        with PipelineRepository() as repo:
            repo.fail_pipeline_run(pipeline_run_id, str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The stage run could not be queued.",
        ) from exc
    task_id = getattr(async_result, "id", None)
    with PipelineRepository() as repo:
        repo.update_pipeline_run_task_id(pipeline_run_id, task_id)
    return RunCreateResponse(pipeline_run_id=pipeline_run_id, task_id=task_id, status="queued")


@router.get("/runs", response_model=RunsResponse)
def list_runs(limit: int = Query(default=25, ge=1, le=100)) -> RunsResponse:
    with PipelineRepository() as repo:
        runs = repo.list_pipeline_runs(limit=limit)
    return RunsResponse(runs=[PipelineRunSummary.model_validate(run) for run in runs])


@router.get("/runs/{run_id}", response_model=PipelineRunSummary)
def get_run(run_id: int) -> PipelineRunSummary:
    with PipelineRepository() as repo:
        run = repo.get_pipeline_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline run not found.")
    return PipelineRunSummary.model_validate(run)


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(run_id: int, limit: int = Query(default=200, ge=1, le=1000)) -> RunEventsResponse:
    with PipelineRepository() as repo:
        if not repo.get_pipeline_run(run_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline run not found.")
        events = repo.list_pipeline_run_events(run_id, limit=limit)
    return RunEventsResponse(events=[PipelineRunEventSummary.model_validate(event) for event in events])
