import csv
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.application.pipeline_services import (
    _mention_from_candidate,
    _result_from_operation,
    export_insights_csv,
    load_um_dataset_records,
)
from datasight.domain.schemas import DatasetRole, ReferenceDirectness
from datasight.domain.stages import PipelineStage


def test_export_insights_csv_includes_discovery_provenance_and_json_cells(tmp_path, monkeypatch):
    class FakeRepo:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def export_insight_rows(self, pipeline_run_id=None):
            assert pipeline_run_id == 9
            return [
                {
                    "paper_id": "W123",
                    "discovery_mode": "catalog_funnel",
                    "discovery_methods": ["dataset_citation", "title_mention"],
                    "evidence": {"quote": "Dataset, reused"},
                }
            ]

        def record_stage_result(self, *args):
            return None

    monkeypatch.setattr("datasight.application.pipeline_services.PipelineRepository", FakeRepo)
    output = tmp_path / "insights.csv"

    result = export_insights_csv(str(output), pipeline_run_id=9)

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert result["count"] == 1
    assert row["discovery_mode"] == "catalog_funnel"
    assert json.loads(row["discovery_methods"]) == ["dataset_citation", "title_mention"]
    assert json.loads(row["evidence"]) == {"quote": "Dataset, reused"}


def test_load_um_dataset_records_from_csv(tmp_path):
    csv_path = tmp_path / "um.csv"
    csv_path.write_text(
        "um_dataset_id,title,aliases,creators,year\n"
        "um-1,Maastricht Health Survey,MHS;Health Survey,Jane Doe;John Doe,2024\n",
        encoding="utf-8",
    )

    records = load_um_dataset_records(str(csv_path))

    assert len(records) == 1
    assert records[0].um_dataset_id == "um-1"
    assert records[0].aliases == ["MHS", "Health Survey"]
    assert records[0].creators == ["Jane Doe", "John Doe"]


def test_batch_result_reports_partial_failures():
    result = _result_from_operation(
        PipelineStage.DOWNLOAD_PDF,
        [SimpleNamespace(success=True), SimpleNamespace(success=False)],
    )

    assert result["status"] == "completed_with_errors"
    assert result["payload"]["successful"] == 1
    assert result["payload"]["failed"] == 1


def test_rule_feature_extraction_populates_matchable_metadata():
    mention = _mention_from_candidate(
        {
            "publication_id": "p1",
            "dataset_name": "Maastricht Health Survey",
            "evidence_text": (
                "We used the Maastricht Health Survey dataset (2024), "
                "available at https://doi.org/10.1234/example."
            ),
            "char_start": 10,
            "char_end": 120,
            "score": 0.8,
        }
    )

    assert mention.metadata.persistent_identifier == "10.1234/example"
    assert str(mention.metadata.dataset_url) == "https://doi.org/10.1234/example"
    assert mention.metadata.dataset_year == 2024
    assert mention.dataset_role == DatasetRole.USED
    assert mention.reference_directness == ReferenceDirectness.DIRECT
    assert mention.provenance.prompt_version == "rules-v3"


def test_evaluation_bundle_preserves_extraction_outcomes_and_source_evidence(tmp_path, monkeypatch):
    from datasight.application.pipeline_services import export_evaluation_bundle
    from datasight.domain.extraction import DatasetEntry, Evidence, Occurrence, PublicationExtraction

    result = PublicationExtraction(paper_id="W1", source_sha256="hash", evidence=[Evidence(
        evidence_id="e1", block_id="b1", quote="Alpha", char_start=0, char_end=5,
        section_heading=None, standardized_section="other", placement="body")],
        datasets=[DatasetEntry(dataset_local_id="d1", display_name="=unsafe-formula", occurrences=[Occurrence(
            occurrence_id="o1", evidence_ids=["e1"], bibliography_only=False)])],
        coverage={"source_characters": 5, "covered_characters": 5})
    class Repo:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_pipeline_run(self, run_id): return {"id": run_id}
        def evaluation_paper_rows(self, run_id): return []
        def evaluation_candidate_rows(self, run_id): return []
        def evaluation_extraction_rows(self, run_id):
            return [{"paper_id": "W1", "extraction_id": 1, "status": "successful", "result": result.model_dump(mode="json")},
                    {"paper_id": "W2", "status": "failed", "error": "GPU off", "result": None}]
        def evaluation_request_attempt_rows(self, run_id):
            return [{"paper_id": "W1", "extraction_id": 1, "work_key": "hash", "request_status": "successful",
                     "attempt_number": 1, "http_status": 200, "elapsed_seconds": 1.2,
                     "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                     "error_category": None}]
    monkeypatch.setattr("datasight.application.pipeline_services.PipelineRepository", Repo)
    export_evaluation_bundle(1, str(tmp_path))
    assert json.loads((tmp_path / "extractions.json").read_text())[1]["status"] == "failed"
    with (tmp_path / "extractions.csv").open(newline="", encoding="utf-8") as handle:
        assert [row["dataset_count"] for row in csv.DictReader(handle)] == ["1", "0"]
    with (tmp_path / "datasets.csv").open(newline="", encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["display_name"] == "'=unsafe-formula"
    with (tmp_path / "field_observations.csv").open(newline="", encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["state"] == "not_reported"
    assert (tmp_path / "occurrences.csv").exists()
    attempts = (tmp_path / "request_attempts.csv").read_text(encoding="utf-8")
    assert "hash" in attempts and "GPU off" not in attempts
