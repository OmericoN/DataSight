from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.application import orchestrator


def test_run_outcome_surfaces_stage_errors():
    results = [
        {"stage": "discover", "status": "successful"},
        {"stage": "download_pdf", "status": "completed_with_errors"},
    ]

    assert orchestrator._run_outcome(results) == "completed_with_errors"


def test_local_workflow_stops_after_extract_features(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_preview_context",
        lambda preview_id, processing_limit: ({"request": {}}, 5, "dataset reuse"),
    )
    monkeypatch.setattr(orchestrator, "create_run", lambda query, config: 23)
    finished = []
    monkeypatch.setattr(
        orchestrator,
        "_finish_run",
        lambda pipeline_run_id, status: finished.append((pipeline_run_id, status)),
    )

    stage_names = [
        "discover",
        "download_pdf",
        "grobid_convert",
        "render_document",
        "detect_mentions",
        "extract_features",
    ]
    service_names = [
        "discover_publications",
        "download_pdf_batch",
        "grobid_convert_batch",
        "render_document_batch",
        "detect_mentions_batch",
        "extract_features_from_candidates",
    ]
    for service_name, stage_name in zip(service_names, stage_names, strict=True):
        monkeypatch.setattr(
            orchestrator.services,
            service_name,
            lambda _stage=stage_name, **kwargs: {"stage": _stage, "status": "successful"},
        )

    monkeypatch.setattr(
        orchestrator.services,
        "match_um_dataset_batch",
        lambda **kwargs: pytest.fail("automatic workflow called disabled matching stage"),
    )
    monkeypatch.setattr(
        orchestrator.services,
        "export_insights_csv",
        lambda *args, **kwargs: pytest.fail("automatic workflow called disabled export stage"),
    )

    result = orchestrator.run_all_local("preview-123", "storage/exports/insights.csv")

    assert [stage["stage"] for stage in result["results"]] == stage_names
    assert finished == [(23, "successful")]


def test_queued_standard_workflow_stops_after_extract_features(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_preview_context",
        lambda preview_id, processing_limit: ({"request": {}}, 5, "dataset reuse"),
    )
    monkeypatch.setattr(orchestrator, "create_run", lambda query, config: 23)
    task_names = []

    class QueuedWorkflow:
        def apply_async(self):
            return type("AsyncResult", (), {"id": "task-23"})()

    def capture_chain(*signatures):
        task_names.extend(signature.task for signature in signatures)
        return QueuedWorkflow()

    class FakeRepo:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def update_pipeline_run_task_id(self, pipeline_run_id, task_id):
            assert (pipeline_run_id, task_id) == (23, "task-23")

    monkeypatch.setattr(orchestrator, "chain", capture_chain)
    monkeypatch.setattr(orchestrator, "PipelineRepository", FakeRepo)

    result = orchestrator.enqueue_run_all(
        "preview-123",
        "storage/exports/insights.csv",
        processing_limit=5,
    )

    assert task_names == [
        "datasight.discover_publications",
        "datasight.download_pdf",
        "datasight.grobid_convert",
        "datasight.render_document",
        "datasight.detect_mentions",
        "datasight.extract_features",
        "datasight.finish_pipeline_run",
    ]
    assert result["task_id"] == "task-23"


def test_enqueue_failure_marks_the_run_failed(monkeypatch):
    failed = []
    monkeypatch.setattr(orchestrator, "create_run", lambda query, config: 23)
    monkeypatch.setattr(
        orchestrator.services,
        "import_um_datasets",
        lambda path: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_discovery_preview",
        lambda preview_id, processing_limit: {
            "request": {"strategy_version": 3, "processing_limit": 5, "mode": "catalog_funnel"}
        },
    )

    class FailedWorkflow:
        def apply_async(self):
            raise ValueError("Queue unavailable")

    monkeypatch.setattr(orchestrator, "chain", lambda *tasks: FailedWorkflow())
    monkeypatch.setattr(
        orchestrator,
        "_fail_run",
        lambda run_id, error: failed.append((run_id, error)),
    )

    with pytest.raises(ValueError, match="Queue unavailable"):
        orchestrator.enqueue_run_all(
            preview_id="preview-123",
            processing_limit=5,
            output="storage/exports/insights.csv",
            um_datasets_path="data/um_dataset",
        )

    assert failed == [(23, "Queue unavailable")]
