"""Versioned, evidence-backed dataset identity extraction contract."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "dataset-evidence-v2"
PROMPT_VERSION = "whole-publication-v3"
T = TypeVar("T")
Role = Literal["created", "used", "comparison", "discussed", "unclear"]
Target = Literal["dataset", "data_paper", "research_article", "repository", "other", "unknown"]
Placement = Literal["abstract", "body", "table", "caption", "footnote", "appendix", "bibliography"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceValue(StrictModel, Generic[T]):
    value: T
    evidence_ids: list[str] = Field(min_length=1)


class Observed(StrictModel, Generic[T]):
    state: Literal["reported", "not_reported", "unclear", "conflicting"] = "not_reported"
    values: list[EvidenceValue[T]] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_state(self) -> Observed[T]:
        if self.state == "not_reported" and self.values:
            raise ValueError("not_reported must have no values")
        if self.state in {"reported", "conflicting"} and not self.values:
            raise ValueError("reported/conflicting observations require evidence-backed values")
        if self.state == "conflicting" and len(self.values) < 2:
            raise ValueError("a conflict requires at least two observations")
        return self


class Identifier(StrictModel):
    scheme: str = Field(min_length=1)
    literal: str = Field(min_length=1)
    target: Target


class ResourceURL(StrictModel):
    url: str = Field(min_length=1)
    target: Literal["landing_page", "download", "documentation", "code", "publication", "unknown"]


class IdentityFeatures(StrictModel):
    name: Observed[str] = Field(default_factory=Observed[str])
    description: Observed[str] = Field(default_factory=Observed[str])
    aliases: Observed[str] = Field(default_factory=Observed[str])
    creators: Observed[str] = Field(default_factory=Observed[str])
    year: Observed[int] = Field(default_factory=Observed[int])
    identifiers: Observed[Identifier] = Field(default_factory=Observed[Identifier])
    repository: Observed[str] = Field(default_factory=Observed[str])
    accessions: Observed[str] = Field(default_factory=Observed[str])
    urls: Observed[ResourceURL] = Field(default_factory=Observed[ResourceURL])
    versions: Observed[str] = Field(default_factory=Observed[str])
    subsets: Observed[str] = Field(default_factory=Observed[str])
    access_dates: Observed[str] = Field(default_factory=Observed[str])


class UseFeatures(StrictModel):
    roles: Observed[Role] = Field(default_factory=Observed[Role])
    origin: Observed[Literal["primary_collection", "secondary_reuse", "derived_or_combined", "unclear"]] = Field(default_factory=Observed[Literal["primary_collection", "secondary_reuse", "derived_or_combined", "unclear"]])
    purposes: Observed[Literal["analysis", "training", "testing", "validation", "benchmarking", "other", "unclear"]] = Field(default_factory=Observed[Literal["analysis", "training", "testing", "validation", "benchmarking", "other", "unclear"]])
    claim_subject: Observed[Literal["current_publication", "prior_work", "unclear"]] = Field(default_factory=Observed[Literal["current_publication", "prior_work", "unclear"]])
    description: Observed[str] = Field(default_factory=Observed[str])


class Evidence(StrictModel):
    evidence_id: str
    block_id: str
    quote: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    section_heading: str | None
    standardized_section: str
    placement: Placement
    citation_markers: list[str] = Field(default_factory=list)
    bibliography_reference_ids: list[str] = Field(default_factory=list)


class CitationFeatures(StrictModel):
    mention_form: Observed[Literal["named", "descriptive", "unclear"]] = Field(default_factory=Observed[Literal["named", "descriptive", "unclear"]])
    reference_forms: Observed[Literal["narrative_mention", "inline_identifier", "inline_url", "citation_marker", "bibliography_entry", "footnote"]] = Field(default_factory=Observed[Literal["narrative_mention", "inline_identifier", "inline_url", "citation_marker", "bibliography_entry", "footnote"]])
    target_type: Observed[Target] = Field(default_factory=Observed[Target])
    relation: Observed[Literal["direct_to_data", "via_publication", "unclear"]] = Field(default_factory=Observed[Literal["direct_to_data", "via_publication", "unclear"]])
    creator_attribution: Observed[str] = Field(default_factory=Observed[str])
    title: Observed[str] = Field(default_factory=Observed[str])
    year: Observed[int] = Field(default_factory=Observed[int])
    repository: Observed[str] = Field(default_factory=Observed[str])
    identifier: Observed[Identifier] = Field(default_factory=Observed[Identifier])
    version: Observed[str] = Field(default_factory=Observed[str])
    subset: Observed[str] = Field(default_factory=Observed[str])
    access_date: Observed[str] = Field(default_factory=Observed[str])


class CitationLink(StrictModel):
    marker: str
    reference_id: str | None
    raw_reference: str | None
    status: Literal["linked", "unresolved", "ambiguous"]


class Occurrence(StrictModel):
    occurrence_id: str
    evidence_ids: list[str] = Field(min_length=1)
    use: UseFeatures = Field(default_factory=UseFeatures)
    citation: CitationFeatures = Field(default_factory=CitationFeatures)
    citation_links: list[CitationLink] = Field(default_factory=list)
    bibliography_only: bool


class DatasetEntry(StrictModel):
    dataset_local_id: str
    display_name: str
    identity: IdentityFeatures = Field(default_factory=IdentityFeatures)
    use: UseFeatures = Field(default_factory=UseFeatures)
    occurrences: list[Occurrence] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class PublicationExtraction(StrictModel):
    schema_version: str = SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    paper_id: str
    source_sha256: str
    evidence: list[Evidence]
    datasets: list[DatasetEntry]
    unresolved_mentions: list[str] = Field(default_factory=list)
    coverage: dict[str, int]

    @model_validator(mode="after")
    def valid_evidence_graph(self) -> PublicationExtraction:
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique")
        if len({entry.dataset_local_id for entry in self.datasets}) != len(self.datasets):
            raise ValueError("Dataset IDs must be unique within the publication")
        for passage in self.evidence:
            if passage.char_end - passage.char_start != len(passage.quote):
                raise ValueError("Evidence offsets do not match its quotation length")
        for dataset in self.datasets:
            if not dataset.occurrences:
                raise ValueError("Each dataset requires a reference occurrence")
            for occurrence in dataset.occurrences:
                if not set(occurrence.evidence_ids) <= evidence_ids:
                    raise ValueError("Occurrence refers to unknown evidence")
            def check_references(value: object) -> None:
                if isinstance(value, dict):
                    if "evidence_ids" in value and not set(value["evidence_ids"]) <= evidence_ids:
                        raise ValueError("Field refers to unknown evidence")
                    for child in value.values():
                        check_references(child)
                elif isinstance(value, list):
                    for child in value:
                        check_references(child)

            check_references(dataset.model_dump())
        if not set(self.unresolved_mentions) <= evidence_ids:
            raise ValueError("Unresolved mentions refer to unknown evidence")
        if self.coverage.get("covered_characters") != self.coverage.get("source_characters"):
            raise ValueError("Completed extraction requires complete source coverage")
        return self


# The locator emits source anchors only; IDs, offsets and grouping are assigned locally.
class LocatedPassage(StrictModel):
    block_id: str
    quote: str = Field(min_length=1)
    quote_occurrence: int = Field(ge=0)
    dataset_name: str | None
    kind: Literal["reference", "support", "ambiguous"]


class LocatedPassages(StrictModel):
    passages: list[LocatedPassage]
