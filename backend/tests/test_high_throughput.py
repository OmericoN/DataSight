from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.application import high_throughput
from datasight.domain.stages import PipelineStage


class FakeRepo:
    all_terminal = False
    queued_counts = {}
    final_status: str | None = None
    claimed_items = []
    finished = []
    outcome = "successful"

    def get_pipeline_run(self, pipeline_run_id):
        return {"status": "running"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None

    def all_item_stages_terminal(self, pipeline_run_id):
        assert pipeline_run_id == 9
        return self.all_terminal

    def get_queued_item_stage_counts(self, pipeline_run_id):
        assert pipeline_run_id == 9
        return self.queued_counts

    def finalize_high_throughput_run_if_ready(self, pipeline_run_id):
        assert pipeline_run_id == 9
        return self.final_status

    def claim_queued_item_stages(self, pipeline_run_id, stage, limit, task_id=None):
        assert pipeline_run_id == 9
        assert stage == PipelineStage.DOWNLOAD_PDF
        assert limit == 2
        assert task_id == "task-1"
        return self.claimed_items

    def finish_item_stage(self, item_id, stage, status, metrics=None, error=None):
        self.finished.append(
            {
                "item_id": item_id,
                "stage": stage,
                "status": status,
                "metrics": metrics,
                "error": error,
            }
        )

    def high_throughput_outcome(self, pipeline_run_id):
        assert pipeline_run_id == 9
        return self.outcome

    def finish_pipeline_run(self, pipeline_run_id, status):
        self.finished.append({"pipeline_run_id": pipeline_run_id, "status": status})


def test_dispatch_plan_schedules_bounded_batches(monkeypatch):
    FakeRepo.all_terminal = False
    FakeRepo.queued_counts = {"download_pdf": 9, "grobid_convert": 1}
    monkeypatch.setattr(high_throughput, "PipelineRepository", FakeRepo)

    plan = high_throughput.build_dispatch_plan(9, batch_size=4, max_batches=2)

    assert [batch.stage for batch in plan.batches] == [
        PipelineStage.DOWNLOAD_PDF,
        PipelineStage.DOWNLOAD_PDF,
        PipelineStage.GROBID_CONVERT,
    ]
    assert plan.final_status is None


def test_llm_dispatch_claims_one_publication_per_task(monkeypatch):
    FakeRepo.all_terminal = False
    FakeRepo.queued_counts = {"extract_features": 9}
    monkeypatch.setattr(high_throughput, "PipelineRepository", FakeRepo)

    plan = high_throughput.build_dispatch_plan(9, batch_size=4, max_batches=2)

    assert len(plan.batches) == 2
    assert all(batch.stage == PipelineStage.EXTRACT_FEATURES and batch.batch_size == 1 and batch.queue == "llm" for batch in plan.batches)


def test_dispatch_plan_finalizes_once_when_terminal(monkeypatch):
    FakeRepo.all_terminal = True
    FakeRepo.final_status = "successful"
    monkeypatch.setattr(high_throughput, "PipelineRepository", FakeRepo)

    plan = high_throughput.build_dispatch_plan(9)

    assert plan.batches == ()
    assert plan.final_status == "successful"


def test_process_stage_claims_batch_and_uses_claimed_service(monkeypatch):
    FakeRepo.claimed_items = [101, 102]
    monkeypatch.setattr(high_throughput, "PipelineRepository", FakeRepo)
    calls = []

    def fake_download(item_id, overwrite=False, task_id=None, claimed=False):
        calls.append((item_id, overwrite, task_id, claimed))
        return {"stage": "download_pdf", "status": "successful", "item_id": item_id}

    monkeypatch.setattr(high_throughput.services, "download_pipeline_item", fake_download)

    result = high_throughput.process_high_throughput_stage(
        9,
        PipelineStage.DOWNLOAD_PDF,
        batch_size=2,
        overwrite=True,
        task_id="task-1",
    )

    assert result["claimed"] == 2
    assert result["successful"] == 2
    assert calls == [(101, True, "task-1", True), (102, True, "task-1", True)]


def test_finalize_uses_completed_with_errors_outcome(monkeypatch):
    FakeRepo.final_status = "completed_with_errors"
    FakeRepo.finished = []
    monkeypatch.setattr(high_throughput, "PipelineRepository", FakeRepo)

    result = high_throughput.finalize_high_throughput_run("storage/exports/insights.csv", 9)

    assert result["status"] == "completed_with_errors"
    assert "export" not in result
