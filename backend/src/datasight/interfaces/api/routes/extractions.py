"""Inspection and explicit resume of run-scoped LLM extraction."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from datasight.application.llm_extraction import queue_resume
from datasight.domain.extraction import PublicationExtraction
from datasight.infrastructure.llm.client import GatewayClient
from datasight.infrastructure.persistence.repository import PipelineRepository

router = APIRouter(tags=["extraction"])


class ExtractionList(BaseModel):
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


class ExtractionDetail(BaseModel):
    id: int
    pipeline_run_id: int
    publication_id: int
    status: str
    source_sha256: str | None
    source_path: str | None
    fingerprint: str
    config: dict[str, Any]
    coverage: dict[str, Any]
    metrics: dict[str, Any]
    error: str | None
    result: PublicationExtraction | None
    requests: list[dict[str, Any]]
    progress: dict[str, Any] | None = None
    error_category: str | None = None


def processing_status(item: dict[str, Any]) -> dict[str, Any]:
    item['progress'] = (item.get('metrics') or {}).get('progress')
    item['error_category'] = 'extraction_budget_exceeded' if (item.get('error') or '').startswith('extraction_budget_exceeded:') else None
    return item


@router.get("/llm/status")
def llm_status() -> dict[str, Any]:
    client = GatewayClient()
    try:
        return client.status()
    finally:
        client.close()


@router.get("/runs/{run_id}/extractions", response_model=ExtractionList)
def list_extractions(run_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> ExtractionList:
    with PipelineRepository() as repo:
        if not repo.get_pipeline_run(run_id):
            raise HTTPException(404, "Pipeline run not found")
        items, total = repo.list_extractions(run_id, limit, offset)
    return ExtractionList(items=[processing_status(item) for item in items], total=total, offset=offset, limit=limit)


@router.get("/runs/{run_id}/extractions/{extraction_id}", response_model=ExtractionDetail)
def extraction_detail(run_id: int, extraction_id: int) -> Any:
    with PipelineRepository() as repo:
        result = repo.extraction_detail(run_id, extraction_id)
    if not result:
        raise HTTPException(404, "Extraction not found in this run")
    return processing_status(result)


@router.post("/runs/{run_id}/extractions/resume", status_code=202)
def resume_extractions(run_id: int) -> dict[str, Any]:
    try:
        return queue_resume(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Extraction could not be queued") from exc
