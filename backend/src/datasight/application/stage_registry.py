"""Shared stage metadata and argument mapping for API, CLI, and workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from datasight.domain.stages import PipelineStage, STAGE_ORDER, stage_values
from datasight.infrastructure.ingestion.renderer import DEFAULT_RENDER_PROFILE, RenderProfile


STAGE_DESCRIPTIONS: dict[PipelineStage, str] = {
    PipelineStage.DISCOVER: "Find publication metadata and open-access PDF links.",
    PipelineStage.DOWNLOAD_PDF: "Download PDF artifacts for discovered publications.",
    PipelineStage.GROBID_CONVERT: "Convert downloaded PDFs to TEI XML with GROBID.",
    PipelineStage.RENDER_DOCUMENT: "Render TEI XML into markdown text for mention detection.",
    PipelineStage.DETECT_MENTIONS: "Detect high-recall candidate dataset mentions.",
    PipelineStage.EXTRACT_FEATURES: "Extract evidence-backed dataset features from complete publications using the LLM.",
    PipelineStage.MATCH_UM_DATASET: "Match extracted mentions against imported UM dataset metadata.",
    PipelineStage.EXPORT_INSIGHTS: "Export joined insight rows to CSV.",
}

STAGE_LABELS: dict[PipelineStage, str | None] = {
    PipelineStage.DISCOVER: "Discover",
    PipelineStage.DOWNLOAD_PDF: "Download PDF",
    PipelineStage.GROBID_CONVERT: "GROBID Convert",
    PipelineStage.RENDER_DOCUMENT: "Render Document",
    PipelineStage.DETECT_MENTIONS: "Detect Mentions",
    PipelineStage.EXTRACT_FEATURES: "Extract Features",
    PipelineStage.MATCH_UM_DATASET: None,
    PipelineStage.EXPORT_INSIGHTS: None,
}


class MissingStageArgument(ValueError):
    """Raised when a stage request is missing a required input."""


@dataclass(frozen=True)
class StageRunOptions:
    query: str | None = None
    limit: int | None = None
    output_path: str | None = None
    overwrite: bool = False
    delete_pdf: bool = False
    open_access_only: bool = True
    topic_ids: str | list[str] | None = None
    keyword_terms: str | list[str] | None = None
    from_year: int | None = None
    to_year: int | None = None
    use_um_profile: bool = True
    rows: list[dict[str, Any]] | None = None
    pipeline_run_id: int | None = None
    render_profile: RenderProfile = DEFAULT_RENDER_PROFILE


def stage_description(stage: PipelineStage | str) -> str:
    stage_value = PipelineStage(stage)
    return STAGE_DESCRIPTIONS.get(stage_value, stage_value.value)


def stage_catalog() -> list[dict[str, str | None]]:
    return [
        {
            "name": stage.value,
            "label": STAGE_LABELS[stage],
            "description": stage_description(stage),
        }
        for stage in STAGE_ORDER
    ]


def task_for_stage(stage: PipelineStage | str, tasks_module: Any) -> Any:
    return {
        PipelineStage.DISCOVER: tasks_module.discover_publications,
        PipelineStage.DOWNLOAD_PDF: tasks_module.download_pdf,
        PipelineStage.GROBID_CONVERT: tasks_module.grobid_convert,
        PipelineStage.RENDER_DOCUMENT: tasks_module.render_document,
        PipelineStage.DETECT_MENTIONS: tasks_module.detect_mentions,
        PipelineStage.EXTRACT_FEATURES: tasks_module.extract_features,
        PipelineStage.MATCH_UM_DATASET: tasks_module.match_um_dataset,
        PipelineStage.EXPORT_INSIGHTS: tasks_module.export_insights,
    }[PipelineStage(stage)]


def stage_args(stage: PipelineStage | str, options: StageRunOptions) -> list[Any]:
    stage_value = PipelineStage(stage)
    if stage_value == PipelineStage.DISCOVER:
        if not options.query:
            raise MissingStageArgument("query is required.")
        return [options.query]
    if stage_value == PipelineStage.EXPORT_INSIGHTS:
        if not options.output_path:
            raise MissingStageArgument("output_path is required.")
        return [options.output_path]
    return []


def stage_kwargs(stage: PipelineStage | str, options: StageRunOptions) -> dict[str, Any]:
    stage_value = PipelineStage(stage)
    kwargs: dict[str, Any]
    if stage_value == PipelineStage.DISCOVER:
        kwargs = {
            "limit": options.limit or 100,
            "open_access_only": options.open_access_only,
            "topic_ids": options.topic_ids,
            "keyword_terms": options.keyword_terms,
            "from_year": options.from_year,
            "to_year": options.to_year,
            "use_um_profile": options.use_um_profile,
        }
    elif stage_value == PipelineStage.DOWNLOAD_PDF:
        kwargs = {"limit": options.limit, "overwrite": options.overwrite}
    elif stage_value == PipelineStage.RENDER_DOCUMENT:
        kwargs = {
            "limit": options.limit,
            "overwrite": options.overwrite,
            "profile": options.render_profile,
        }
    elif stage_value == PipelineStage.GROBID_CONVERT:
        kwargs = {
            "limit": options.limit,
            "overwrite": options.overwrite,
            "delete_pdf": options.delete_pdf,
        }
    elif stage_value in {
        PipelineStage.DETECT_MENTIONS,
        PipelineStage.EXTRACT_FEATURES,
        PipelineStage.MATCH_UM_DATASET,
    }:
        kwargs = {"limit": options.limit}
    elif stage_value == PipelineStage.EXPORT_INSIGHTS:
        kwargs = {"rows": options.rows}
    else:
        kwargs = {}

    if options.pipeline_run_id is not None:
        kwargs["pipeline_run_id"] = options.pipeline_run_id
    return kwargs


__all__ = [
    "MissingStageArgument",
    "PipelineStage",
    "StageRunOptions",
    "stage_args",
    "stage_catalog",
    "stage_description",
    "stage_kwargs",
    "stage_values",
    "task_for_stage",
]
