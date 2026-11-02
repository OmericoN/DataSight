"""Typed schemas for dataset mention extraction and UM matching."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class DatasetRole(StrEnum):
    USED = "used"
    CREATED = "created"
    DISCUSSED = "discussed"
    COMPARISON = "comparison"
    UNCLEAR = "unclear"


class ReferenceDirectness(StrEnum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"
    INFORMAL = "informal"
    UNCLEAR = "unclear"


class MatchStatus(StrEnum):
    MATCHED = "matched"
    POSSIBLE = "possible"
    NO_MATCH = "no_match"
    REVIEW_REQUIRED = "review_required"


class MentionEvidence(BaseModel):
    abstract_quote: str | None = None
    body_quote: str
    section_heading: str | None = None
    standardized_section: str | None = None
    placement_type: str | None = None
    placement_content: str | None = None
    citation_marker: str | None = None
    bibliography_entry: str | None = None


class DatasetMetadata(BaseModel):
    features: dict[str, Any] | None = None
    reference_title: str | None = None
    persistent_identifier: str | None = None
    dataset_authors: list[str] = Field(default_factory=list)
    dataset_year: int | None = None
    dataset_url: HttpUrl | str | None = None
    reference_material: str | None = None
    material_year: int | None = None
    dataset_version: str | None = None
    access_date: str | None = None


class ExtractionProvenance(BaseModel):
    model_config = {"extra": "allow"}
    tei_path: str | None = None
    markdown_path: str | None = None
    section_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    llm_model: str | None = None
    prompt_version: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DatasetMention(BaseModel):
    publication_id: str
    dataset_name: str
    aliases: list[str] = Field(default_factory=list)
    dataset_role: DatasetRole = DatasetRole.UNCLEAR
    reference_directness: ReferenceDirectness = ReferenceDirectness.UNCLEAR
    evidence: MentionEvidence
    metadata: DatasetMetadata = Field(default_factory=DatasetMetadata)
    provenance: ExtractionProvenance = Field(default_factory=ExtractionProvenance)


class MentionCandidate(BaseModel):
    publication_id: str
    dataset_name: str
    evidence_text: str
    section_heading: str | None = None
    standardized_section: str | None = None
    char_start: int
    char_end: int
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    source: Literal["rule", "citation_context", "llm"] = "rule"
    evidence_tier: Literal["strong", "medium", "broad"] = "broad"
    trigger_type: str
    trigger_text: str
    triggers: list[dict[str, str]] = Field(default_factory=list)
    detector_version: str = "rules-v3"
    render_sha256: str | None = None
    detection_run_id: int | None = None


class UMDatasetRecord(BaseModel):
    um_dataset_id: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    creators: list[str] = Field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    year: int | None = None
    repository: str | None = None
    keywords: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class UMMatchDecision(BaseModel):
    mention_id: str | None = None
    publication_id: str
    dataset_name: str
    status: MatchStatus
    um_dataset_id: str | None = None
    candidate_um_dataset_ids: list[str] = Field(default_factory=list)
    match_method: str
    match_score: float = Field(ge=0.0, le=1.0)
    matched_fields: list[str] = Field(default_factory=list)
    review_required: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StageResult(BaseModel):
    stage: str
    status: Literal["successful", "completed_with_errors", "failed", "skipped", "interrupted"]
    count: int = 0
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
