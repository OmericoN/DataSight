from types import SimpleNamespace

import pytest

from datasight.application.stage_registry import (
    MissingStageArgument,
    PipelineStage,
    StageRunOptions,
    stage_args,
    stage_catalog,
    stage_kwargs,
    task_for_stage,
)


def test_stage_catalog_exposes_canonical_order_and_descriptions():
    catalog = stage_catalog()

    assert [stage["name"] for stage in catalog] == [
        "discover",
        "download_pdf",
        "grobid_convert",
        "render_document",
        "detect_mentions",
        "extract_features",
        "match_um_dataset",
        "export_insights",
    ]
    assert str(catalog[0]["description"]).startswith("Find publication metadata")
    assert [stage["label"] for stage in catalog[:6]] == [
        "Discover",
        "Download PDF",
        "GROBID Convert",
        "Render Document",
        "Detect Mentions",
        "Extract Features",
    ]
    assert [stage["label"] for stage in catalog[6:]] == [None, None]


def test_stage_registry_builds_discover_arguments():
    options = StageRunOptions(
        query="dataset reuse",
        limit=12,
        open_access_only=False,
        topic_ids=["T123"],
        keyword_terms=["biobank"],
        from_year=2020,
        to_year=2024,
        use_um_profile=True,
    )

    assert stage_args(PipelineStage.DISCOVER, options) == ["dataset reuse"]
    assert stage_kwargs(PipelineStage.DISCOVER, options) == {
        "limit": 12,
        "open_access_only": False,
        "topic_ids": ["T123"],
        "keyword_terms": ["biobank"],
        "from_year": 2020,
        "to_year": 2024,
        "use_um_profile": True,
    }


def test_stage_registry_requires_stage_specific_inputs():
    assert StageRunOptions().render_profile == "pruned"
    with pytest.raises(MissingStageArgument):
        stage_args(PipelineStage.DISCOVER, StageRunOptions())

    with pytest.raises(MissingStageArgument):
        stage_args(PipelineStage.EXPORT_INSIGHTS, StageRunOptions())


def test_stage_registry_resolves_worker_task_by_stage():
    tasks = SimpleNamespace(
        discover_publications=object(),
        download_pdf=object(),
        grobid_convert=object(),
        render_document=object(),
        detect_mentions=object(),
        extract_features=object(),
        match_um_dataset=object(),
        export_insights=object(),
    )

    assert task_for_stage("match_um_dataset", tasks) is tasks.match_um_dataset
