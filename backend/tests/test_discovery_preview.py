from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.application import discovery_preview
from datasight.domain.schemas import UMDatasetRecord
from datasight.infrastructure.pubfetcher.openalex import OpenAlexSearchResult
from datasight.interfaces.api.schemas import DiscoveryPreviewResponse


class FakeRepository:
    def lock_pool_selection(self, *, session=False):
        pass

    def active_pool_claims(self, paper_ids, run_id):
        return set()

    saved_preview: dict | None = None
    records: list[UMDatasetRecord] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def list_um_dataset_records(self):
        return self.records

    def list_topic_resolution_cache(self, catalog_fingerprint):
        return {}

    def processed_discovery_paper_ids(self, paper_ids, pipeline_run_id=None):
        return set()

    def save_topic_resolutions(self, catalog_fingerprint, requested_names, resolved):
        return None

    def save_discovery_preview(self, **kwargs):
        type(self).saved_preview = kwargs


class FakeOpenAlexClient:
    api_key = "test-key"
    calls: list[dict] = []
    work_count = 125

    def search_works_with_meta(self, **kwargs):
        type(self).calls.append(kwargs)
        limit = kwargs["limit"]
        if kwargs["sample_size"] is not None:
            offset = (kwargs["sample_seed"] - 42) * limit
        else:
            offset = 0 if kwargs["cursor"] == "*" else int(kwargs["cursor"])
        works = [
            {
                "paperId": f"W{900 + index}",
                "title": f"Candidate {index}",
                "open_access_url": f"https://example.org/{index}.pdf",
                "raw": {},
            }
            for index in range(offset, min(self.work_count, offset + limit))
        ]
        return OpenAlexSearchResult(
            works=works,
            total_count=self.work_count,
            cost_usd=0.001 if kwargs.get("query") else 0.0001,
            calls=1,
            rate_limit={},
            next_cursor=str(offset + limit) if kwargs["sample_size"] is None and offset + limit < self.work_count else None,
        )

    def list_entities_with_meta(self, *args, **kwargs):
        raise AssertionError("Topic resolution should be lazy")


def test_processed_papers_do_not_count_toward_preview_target(monkeypatch):
    class Repository(FakeRepository):
        def processed_discovery_paper_ids(self, paper_ids, pipeline_run_id=None):
            return {"W900", "W901"} & set(paper_ids)

    monkeypatch.setattr(discovery_preview, "PipelineRepository", Repository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)
    monkeypatch.setattr(FakeOpenAlexClient, "work_count", 3)

    preview = discovery_preview.create_discovery_preview({
        "strategy_version": 3,
        "mode": "manual",
        "manual_query": "dataset reuse",
        "processing_limit": 2,
    })

    assert [candidate["paper_id"] for candidate in preview["candidates"]] == ["W902"]
    assert preview["included_count"] == 1
    assert preview["metrics"]["target_met"] is False
    assert preview["metrics"]["unique_processed_works_skipped"] == 2
    assert preview["phase_results"]["manual"]["duplicate_count"] == 0


def test_manual_discovery_fills_pool_across_pages_before_applying_processing_limit(monkeypatch):
    class Repository(FakeRepository):
        def processed_discovery_paper_ids(self, paper_ids, pipeline_run_id=None):
            return {paper_id for paper_id in paper_ids if 900 <= int(paper_id[1:]) < 995}

    monkeypatch.setattr(discovery_preview, "PipelineRepository", Repository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)
    monkeypatch.setattr(FakeOpenAlexClient, "calls", [])
    monkeypatch.setattr(FakeOpenAlexClient, "work_count", 200)

    preview = discovery_preview.create_discovery_preview({
        "strategy_version": 3,
        "mode": "manual",
        "manual_query": "dataset reuse",
        "discovery_limit": 20,
        "processing_limit": 3,
    })

    assert [call["cursor"] for call in FakeOpenAlexClient.calls] == ["*", "100"]
    assert preview["candidate_count"] == 20
    assert preview["included_count"] == 3
    assert preview["stop_reason"] == "pool_target_met"
    assert DiscoveryPreviewResponse.model_validate(preview).stop_reason == "pool_target_met"
    assert preview["metrics"]["unique_processed_works_skipped"] == 95
    assert all(int(candidate["paper_id"][1:]) >= 995 for candidate in preview["candidates"])


def test_launch_rechecks_processed_papers_and_fills_from_remaining_candidates(monkeypatch):
    candidates = [
        {"paper_id": paper_id, "pipeline_ready": True}
        for paper_id in ("W1", "W2", "W3")
    ]
    monkeypatch.setattr(discovery_preview, "validate_discovery_preview", lambda *_: {
        "request": {"processing_limit": 2},
        "payload": {"candidates": candidates},
    })

    class Repository(FakeRepository):
        def processed_discovery_paper_ids(self, paper_ids, pipeline_run_id=None):
            assert pipeline_run_id == 1
            return {"W1"}

        def upsert_publications(self, papers, source):
            assert [paper["paper_id"] for paper in papers] == ["W2", "W3"]

        def persist_discovery_candidates(self, pipeline_run_id, candidates):
            assert candidates[0]["exclusion_reason"] == "Already processed"
            self.included = [row["paper_id"] for row in candidates if row["included"]]
            return len(candidates)

        def included_discovery_paper_ids(self, pipeline_run_id):
            return self.included

    monkeypatch.setattr(discovery_preview, "PipelineRepository", Repository)

    result = discovery_preview.materialize_discovery_preview("preview", pipeline_run_id=1)

    assert result["paper_ids"] == ["W2", "W3"]
    assert result["processed_skipped"] == 1


def test_manual_preview_returns_every_retained_candidate(monkeypatch):
    FakeRepository.records = []
    FakeRepository.saved_preview = None
    FakeOpenAlexClient.calls = []
    FakeOpenAlexClient.work_count = 125
    monkeypatch.setattr(discovery_preview, "PipelineRepository", FakeRepository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)

    preview = discovery_preview.create_discovery_preview(
        {
            "strategy_version": 3,
            "mode": "manual",
            "manual_query": "dataset reuse",
            "focus_query": "",
            "discovery_limit": 125,
            "processing_limit": 125,
            "publication_types": [],
            "max_cost_usd": 0.25,
        }
    )

    assert preview["candidate_count"] == 125
    assert preview["ready_count"] == 125
    assert preview["watchlist_count"] == 0
    assert preview["stop_reason"] == "pool_target_met"
    assert preview["completed_phases"] == ["manual"]
    assert all(candidate["evidence_tier"] == "expanded" for candidate in preview["candidates"])
    assert FakeRepository.saved_preview is not None


def test_random_preview_uses_seeded_sample_without_catalog(monkeypatch):
    class Repository(FakeRepository):
        records = []

        def processed_discovery_paper_ids(self, paper_ids, pipeline_run_id=None):
            return {"W900"} & set(paper_ids)

    FakeRepository.saved_preview = None
    FakeOpenAlexClient.calls = []
    FakeOpenAlexClient.work_count = 126
    monkeypatch.setattr(discovery_preview, "PipelineRepository", Repository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)

    preview = discovery_preview.create_discovery_preview(
        {
            "strategy_version": 3,
            "mode": "random",
            "focus_query": "",
            "manual_query": None,
            "random_seed": 42,
            "discovery_limit": 125,
            "processing_limit": 50,
            "from_year": 2020,
            "to_year": 2025,
            "publication_types": ["article"],
            "max_cost_usd": 0.25,
        }
    )

    assert preview["random_seed"] == 42
    assert preview["completed_phases"] == ["random"]
    assert preview["stop_reason"] == "pool_target_met"
    assert preview["candidate_count"] == 125
    assert preview["included_count"] == 50
    assert preview["candidates"][0]["paper_id"] == "W901"
    assert all(candidate["evidence_reasons"] == ["random_sample"] for candidate in preview["candidates"])
    first_call, second_call = FakeOpenAlexClient.calls
    assert first_call == {
        "query": None,
        "limit": 100,
        "filters": {"publication_year": "2020-2025", "type": ["article"]},
        "sort": None,
        "cursor": "*",
        "search_mode": "search",
        "sample_size": 100,
        "sample_seed": 42,
        "max_cost_usd": 0.25,
    }
    assert second_call == {**first_call, "sample_seed": 43, "max_cost_usd": 0.2499}


def test_catalog_funnel_completes_direct_phase_then_stops_before_exact(monkeypatch):
    FakeRepository.records = [
        UMDatasetRecord(
            um_dataset_id="W1",
            title="Distinctive Maastricht longitudinal research dataset",
            doi="10.1234/um.1",
            raw={"openalex": {"id": "W1", "related_works": ["W2"]}},
        )
    ]
    FakeOpenAlexClient.calls = []
    FakeOpenAlexClient.work_count = 10
    monkeypatch.setattr(discovery_preview, "PipelineRepository", FakeRepository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)

    preview = discovery_preview.create_discovery_preview(
        {
            "strategy_version": 3,
            "mode": "catalog_funnel",
            "focus_query": "must not narrow direct",
            "manual_query": None,
            "discovery_limit": 10,
            "processing_limit": 1,
            "publication_types": [],
            "max_cost_usd": 0.25,
        }
    )

    assert len(FakeOpenAlexClient.calls) == 3
    assert all("must not narrow direct" not in (call["query"] or "") for call in FakeOpenAlexClient.calls)
    assert "reuse_language" in preview["candidates"][0]["evidence_reasons"]
    assert preview["completed_phases"] == ["direct"]
    assert preview["stop_reason"] == "pool_target_met"
    assert set(preview["phase_results"]) == {"direct"}


def test_cost_ceiling_retains_partial_direct_results(monkeypatch):
    FakeRepository.records = [
        UMDatasetRecord(
            um_dataset_id="W1",
            title="Distinctive Maastricht longitudinal research dataset",
            raw={"openalex": {"id": "W1"}},
        )
    ]
    FakeOpenAlexClient.calls = []
    FakeOpenAlexClient.work_count = 1
    monkeypatch.setattr(discovery_preview, "PipelineRepository", FakeRepository)
    monkeypatch.setattr(discovery_preview, "OpenAlexClient", FakeOpenAlexClient)

    preview = discovery_preview.create_discovery_preview(
        {
            "strategy_version": 3,
            "mode": "catalog_funnel",
            "focus_query": "",
            "manual_query": None,
            "discovery_limit": 10,
            "processing_limit": 10,
            "publication_types": [],
            "max_cost_usd": 0.00015,
        }
    )

    assert len(FakeOpenAlexClient.calls) == 1
    assert preview["candidate_count"] == 1
    assert preview["partial"] is True
    assert preview["stop_reason"] == "cost_ceiling"
    assert preview["phase_results"]["direct"]["status"] == "partial"


def test_origin_paper_seeds_downstream_search_without_using_a_processing_slot(monkeypatch):
    source_title = "Changing patterns of participation in longitudinal surveys"
    record = UMDatasetRecord(
        um_dataset_id="survey", title=f"Replication Data for: {source_title}",
        doi="10.1234/survey", raw={"openalex": {"id": "W1", "related_works": ["W2"]}},
    )
    monkeypatch.setattr(FakeRepository, "records", [record])
    monkeypatch.setattr(discovery_preview, "PipelineRepository", FakeRepository)

    class Client(FakeOpenAlexClient):
        def search_works_with_meta(self, **kwargs):
            if kwargs["filters"].get("cites") == ["W3"]:
                assert kwargs["filters"]["language"] == "en" and kwargs["query"]
                rows = [("W4", "A secondary analysis of survey participation", None)]
            else:
                rows = [
                    ("W1", "Catalog record", None), ("W2", "Known related paper", None),
                    ("W3", source_title, None), ("W5", "Another deposit record", "https://doi.org/10.1234/SURVEY"),
                    ("W6", "Another eligible paper", None),
                ]
            works = [dict(paperId=pid, title=title, doi=doi, open_access_url="paper.pdf") for pid, title, doi in rows]
            return OpenAlexSearchResult(works=works, total_count=len(works), cost_usd=0.001, calls=1, rate_limit={})

    monkeypatch.setattr(discovery_preview, "OpenAlexClient", Client)
    preview = discovery_preview.create_discovery_preview({
        "strategy_version": 3, "mode": "catalog_funnel", "discovery_limit": 2, "processing_limit": 1, "language": "en",
    })
    assert preview["phase_results"]["direct"]["ready_after_phase"] == 1
    assert preview["metrics"]["unique_catalog_works_skipped"] == 4
    assert preview["completed_phases"] == ["direct", "source_citations"]
    assert {candidate["paper_id"] for candidate in preview["candidates"]} == {"W4", "W6"}
    assert preview["included_count"] == 1
    candidate = next(candidate for candidate in preview["candidates"] if candidate["paper_id"] == "W4")
    assert candidate["evidence_reasons"] == ["source_paper_citation"]
    assert candidate["evidence_tier"] == "expanded" and candidate["matched_um_dataset_ids"] == []
