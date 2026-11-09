"""Pydantic schemas for the DataSight HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from datasight.application.reset import RESET_CONFIRMATION
from datasight.config import DEFAULT_UM_DATASETS_PATH
from datasight.domain.run_strategy import RunStrategy
from datasight.infrastructure.ingestion.renderer import DEFAULT_RENDER_PROFILE


class HealthResponse(BaseModel):
    database_ready: bool
    redis_ready: bool
    worker_ready: bool
    grobid_ready: bool
    details: dict[str, Any] = Field(default_factory=dict)


class StageInfo(BaseModel):
    name: str
    label: str | None
    description: str


class StagesResponse(BaseModel):
    stages: list[StageInfo]


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=1)
    processing_limit: int | None = Field(default=None, ge=1, le=1000)
    excluded_candidate_ids: list[str] = Field(default_factory=list)
    overwrite: bool = False
    um_datasets_path: str | None = DEFAULT_UM_DATASETS_PATH
    output_path: str = "storage/exports/insights.csv"
    strategy: RunStrategy = RunStrategy.STANDARD
    render_profile: Literal["full_body", "pruned"] = DEFAULT_RENDER_PROFILE


class StageRunCreateRequest(BaseModel):
    query: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)
    topic_ids: list[str] | None = None
    keyword_terms: list[str] | None = None
    from_year: int | None = Field(default=None, ge=1800, le=3000)
    to_year: int | None = Field(default=None, ge=1800, le=3000)
    use_um_profile: bool = True
    open_access_only: bool = True
    overwrite: bool = False
    delete_pdf: bool = False
    render_profile: Literal["full_body", "pruned"] = DEFAULT_RENDER_PROFILE
    output_path: str | None = None


class RunCreateResponse(BaseModel):
    pipeline_run_id: int
    task_id: str | None = None
    status: Literal["queued", "running", "successful", "completed_with_errors", "failed", "skipped", "interrupted"]


class StageRunSummary(BaseModel):
    id: int
    stage: str
    status: str
    attempt_count: int = 0
    task_id: str | None = None
    error: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PipelineRunSummary(BaseModel):
    id: int
    run_key: str
    query: str | None = None
    status: str
    config: dict[str, Any] = Field(default_factory=dict)
    celery_task_id: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    stages: list[StageRunSummary] = Field(default_factory=list)


class RunsResponse(BaseModel):
    runs: list[PipelineRunSummary]


class PipelineRunEventSummary(BaseModel):
    id: int
    pipeline_run_id: int
    stage: str | None = None
    level: str = "info"
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RunEventsResponse(BaseModel):
    events: list[PipelineRunEventSummary]


class InsightsResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]


class PaginatedInsightsResponse(InsightsResponse):
    total: int
    offset: int
    limit: int


class UMProfilePhase(BaseModel):
    id: str
    label: str
    description: str
    coverage_count: int
    coverage_percent: float
    estimated_calls: int
    estimated_cost_usd: float


class UMDiscoveryProfileResponse(BaseModel):
    dataset_count: int
    catalog_fingerprint: str
    coverage: dict[str, float]
    counts: dict[str, int]
    topic_resolution: dict[str, Any]
    phases: list[UMProfilePhase]
    top_topics: list[str] = Field(default_factory=list)
    top_keywords: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OpenAlexStatusResponse(BaseModel):
    status: Literal["ready", "missing", "invalid", "unavailable"]
    available: bool
    remaining: float | None = None
    limit: float | None = None
    reset_seconds: float | None = None
    reset_at: datetime | None = None
    message: str


class DiscoveryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_version: Literal[2] = 2
    mode: Literal["catalog_funnel", "random", "manual"] = "catalog_funnel"
    focus_query: str = ""
    manual_query: str | None = None
    random_seed: int | None = Field(default=None, ge=1, le=2_147_483_647)
    from_year: int | None = Field(default=None, ge=1800, le=3000)
    to_year: int | None = Field(default=None, ge=1800, le=3000)
    publication_types: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, pattern=r"^[a-zA-Z]{2,3}$")
    discovery_limit: int = Field(default=500, ge=1, le=1000)
    processing_limit: int = Field(default=50, ge=1, le=1000)
    max_cost_usd: float = Field(default=0.25, gt=0, le=5)

    @model_validator(mode="after")
    def validate_preview(self) -> "DiscoveryPreviewRequest":
        if self.processing_limit > self.discovery_limit:
            raise ValueError("processing_limit cannot exceed discovery_limit")
        if self.from_year and self.to_year and self.from_year > self.to_year:
            raise ValueError("from_year cannot exceed to_year")
        if self.mode == "manual" and not (self.manual_query or "").strip():
            raise ValueError("manual_query is required for a manual preview")
        if self.mode != "manual" and self.manual_query:
            raise ValueError("manual_query is only valid in manual mode")
        if self.mode != "catalog_funnel" and self.focus_query.strip():
            raise ValueError("focus_query is only valid in catalog_funnel mode")
        if self.mode != "random" and self.random_seed is not None:
            raise ValueError("random_seed is only valid in random mode")
        return self


class DiscoveryCandidateSummary(BaseModel):
    paper_id: str
    title: str | None = None
    doi: str | None = None
    year: int | None = None
    source_url: str | None = None
    open_access_url: str | None = None
    oa_status: str | None = None
    cited_by_count: int | None = None
    primary_source_name: str | None = None
    candidate_strength: float
    evidence_tier: Literal["direct", "exact", "expanded"]
    evidence_reasons: list[str]
    matched_um_dataset_ids: list[str]
    pipeline_ready: bool
    included: bool
    exclusion_reason: str | None = None


class DiscoveryPreviewResponse(BaseModel):
    preview_id: str
    strategy_version: Literal[2]
    strategy_fingerprint: str
    catalog_fingerprint: str
    language: str | None = None
    code_version: str
    provider: Literal["openalex"]
    provider_snapshot_at: datetime
    request: dict[str, Any]
    executed_queries: list[dict[str, Any]]
    expires_at: datetime
    candidate_count: int
    included_count: int
    ready_count: int
    watchlist_count: int
    estimated_cost_usd: float
    actual_cost_usd: float
    actual_calls: int
    max_cost_usd: float
    random_seed: int | None = None
    partial: bool
    rate_limit: dict[str, str]
    stop_reason: Literal["ready_target_met", "cost_ceiling", "phases_exhausted", "provider_failure"]
    completed_phases: list[str]
    phase_results: dict[str, dict[str, Any]]
    warnings: list[str]
    metrics: dict[str, Any]
    profile: UMDiscoveryProfileResponse
    candidates: list[DiscoveryCandidateSummary]


class DiscoveryCandidateListResponse(BaseModel):
    items: list[DiscoveryCandidateSummary]
    total: int
    offset: int
    limit: int


class ImportUMDatasetsRequest(BaseModel):
    path: str = Field(min_length=1)


class ImportUMDatasetsResponse(BaseModel):
    status: str
    count: int
    deleted: int = 0
    path: str
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class UMDatasetSummary(BaseModel):
    um_dataset_id: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    creators: list[str] = Field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    year: int | None = None
    repository: str | None = None
    keywords: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UMDatasetDetail(UMDatasetSummary):
    raw: dict[str, Any] = Field(default_factory=dict)


class UMDatasetListResponse(BaseModel):
    items: list[UMDatasetSummary]
    total: int
    offset: int
    limit: int
    repositories: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)


class UMDatasetVerificationIssue(BaseModel):
    um_dataset_id: str
    title: str
    status: Literal["missing", "unexpected", "changed"]
    changed_fields: list[str] = Field(default_factory=list)


class UMDatasetVerificationResponse(BaseModel):
    status: Literal["verified", "mismatch", "not_imported", "unavailable"]
    source_path: str
    checked_at: datetime
    source_count: int | None = None
    stored_count: int
    verified_count: int
    issues: list[UMDatasetVerificationIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class ResetRequest(BaseModel):
    confirm: str = Field(description=f'Must equal "{RESET_CONFIRMATION}".')
    force: bool = False


class ResetResponse(BaseModel):
    status: str
    active_runs: int
    truncated_tables: list[str]
    deleted_paths: list[str]
    recreated_directories: list[str]
