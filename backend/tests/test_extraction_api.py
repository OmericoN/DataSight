from fastapi.testclient import TestClient

from datasight.interfaces.api.main import app
from datasight.interfaces.api.routes import extractions


def test_budget_category_and_progress_are_optional_additions():
    result = extractions.processing_status({'error': 'extraction_budget_exceeded: HTTP attempt limit',
                                           'metrics': {'progress': {'phase': 'locator'}}})
    assert result['error_category'] == 'extraction_budget_exceeded'
    assert result['progress'] == {'phase': 'locator'}


class Repository:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_pipeline_run(self, run_id):
        return {"id": run_id} if run_id == 1 else None

    def list_extractions(self, run_id, limit, offset):
        return [{"paper_id": "W1", "status": "successful", "dataset_count": 0}], 1

    def extraction_detail(self, run_id, extraction_id):
        return None


def test_extraction_listing_zero_results_pagination_and_run_boundaries(monkeypatch):
    monkeypatch.setattr(extractions, "PipelineRepository", Repository)
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/1/extractions")
        assert response.status_code == 200
        assert response.json()["items"][0]["dataset_count"] == 0
        assert client.get("/api/v1/runs/2/extractions").status_code == 404
        assert client.get("/api/v1/runs/1/extractions/99").status_code == 404
        assert client.get("/api/v1/runs/1/extractions?limit=0").status_code == 422


def test_manual_resume_conflict_and_missing_run(monkeypatch):
    def conflict(run_id):
        if run_id == 2:
            raise LookupError("not found")
        raise ValueError("already active")

    monkeypatch.setattr(extractions, "queue_resume", conflict)
    with TestClient(app) as client:
        assert client.post("/api/v1/runs/1/extractions/resume").status_code == 409
        assert client.post("/api/v1/runs/2/extractions/resume").status_code == 404


def test_status_exposes_no_credentials_and_does_not_require_inference(monkeypatch):
    monkeypatch.setattr("datasight.config.LLM_BASE_URL", "")
    monkeypatch.setattr("datasight.config.LLM_API_KEY", "private-test-key")
    with TestClient(app) as client:
        response = client.get("/api/v1/llm/status")
    assert response.status_code == 200
    assert not response.json()["available"]
    assert "private-test-key" not in response.text
