from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.domain.results import DownloadResult
from datasight.domain.schemas import MentionCandidate, UMDatasetRecord
from datasight.infrastructure.persistence.repository import PipelineRepository


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.fetchone_results = []
        self.fetchall_results = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def executemany(self, query, params):
        for values in params:
            self.execute(query, values)

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self.fetchall_results:
            return self.fetchall_results.pop(0)
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def make_repo():
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    repo = PipelineRepository.__new__(PipelineRepository)
    repo.cursor = cursor
    repo.conn = conn
    return repo, cursor, conn


def test_upsert_um_datasets_uses_canonical_table():
    repo, cursor, conn = make_repo()
    record = UMDatasetRecord(
        um_dataset_id="um-1",
        title="Maastricht Health Survey",
        aliases=["MHS"],
        creators=["Jane Doe"],
        year=2024,
    )

    assert repo.upsert_um_datasets([record]) == 1

    query, params = cursor.executed[-1]
    assert "INSERT INTO um_datasets" in query
    assert params[0] == "um-1"
    assert params[2] == ["MHS"]
    assert conn.commits == 1


def test_artifact_persistence_records_hash_version_metrics_and_downloadability(tmp_path):
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 1}, {"id": 1}]
    pdf = tmp_path / "W1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = DownloadResult(
        paper_id="W1",
        success=True,
        message="downloaded",
        filepath=pdf,
        file_size_bytes=pdf.stat().st_size,
        sha256="known-hash",
        warnings=["fixture warning"],
        quality_metrics={"attempts": 1},
    )

    assert repo.persist_download_results([result]) == 1

    artifact_query, artifact_params = next(
        (query, params) for query, params in cursor.executed if "INSERT INTO artifacts" in query
    )
    assert "sha256" in artifact_query
    assert "known-hash" in artifact_params
    assert artifact_params[-1].adapted["producer_version"] == "pdf-downloader-v2"
    download_query, download_params = cursor.executed[-1]
    assert "download_status" in download_query
    assert download_params == ("downloaded", None, 1)
    assert conn.commits == 2


def test_sync_um_datasets_upserts_then_removes_stale_records():
    repo, cursor, conn = make_repo()
    cursor.rowcount = 1
    record = UMDatasetRecord(um_dataset_id="W123", title="Dataset")

    assert repo.sync_um_datasets([record]) == (1, 1)

    assert any("INSERT INTO um_datasets" in query for query, _ in cursor.executed)
    delete_query, delete_params = cursor.executed[-1]
    assert "DELETE FROM um_datasets" in delete_query
    assert delete_params == (["W123"],)
    assert conn.commits == 1


def test_list_um_dataset_catalog_applies_filters_and_returns_facets():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 1}]
    cursor.fetchall_results = [
        [
            {
                "um_dataset_id": "W123",
                "title": "Health Dataset",
                "aliases": [],
                "creators": ["Jane Doe"],
                "doi": "10.1234/demo",
                "url": "https://example.org",
                "year": 2024,
                "repository": "Dataverse",
                "keywords": ["health"],
                "created_at": None,
                "updated_at": None,
            }
        ],
        [{"repository": "Dataverse"}],
        [{"year": 2024}],
    ]

    result = repo.list_um_dataset_catalog(
        query="health", repository="Dataverse", year=2024, offset=50, limit=50
    )

    assert result["total"] == 1
    assert result["items"][0]["um_dataset_id"] == "W123"
    assert result["repositories"] == ["Dataverse"]
    assert result["years"] == [2024]
    count_query, count_params = cursor.executed[0]
    assert "array_to_string(creators" in count_query
    assert count_params[-2:] == ["Dataverse", 2024]
    page_query, page_params = cursor.executed[1]
    assert "LIMIT %s OFFSET %s" in page_query
    assert page_params[-2:] == [50, 50]


def test_get_um_dataset_catalog_record_returns_full_record():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"um_dataset_id": "W123", "title": "Dataset", "raw": {"a": 1}}]

    result = repo.get_um_dataset_catalog_record("W123")

    assert result == {"um_dataset_id": "W123", "title": "Dataset", "raw": {"a": 1}}
    assert cursor.executed[-1][1] == ("W123",)


def test_upsert_openalex_publications_replaces_child_rows():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 11}]
    paper = {
        "paperId": "W123",
        "id": "https://openalex.org/W123",
        "doi": "10.123/example",
        "title": "OpenAlex work",
        "abstract": "A study using data.",
        "year": 2024,
        "publication_date": "2024-01-02",
        "language": "en",
        "publication_type": "article",
        "source_url": "https://openalex.org/W123",
        "open_access_url": "https://example.org/work.pdf",
        "oa_status": "gold",
        "cited_by_count": 12,
        "is_retracted": False,
        "has_fulltext": True,
        "primary_source_name": "Journal",
        "raw": {
            "authorships": [
                {
                    "author": {"id": "https://openalex.org/A1", "display_name": "Jane Doe"},
                    "is_corresponding": True,
                    "institutions": [{"id": "https://openalex.org/I1", "display_name": "UM"}],
                }
            ],
            "topics": [{"id": "https://openalex.org/T1", "display_name": "Health data", "score": 0.9}],
            "keywords": [{"display_name": "biobank", "score": 0.8}],
            "concepts": [{"display_name": "Epidemiology", "level": 1, "score": 0.7}],
            "related_works": ["https://openalex.org/W999"],
        },
    }

    assert repo.upsert_publications([paper], source="openalex") == 1

    queries = [query for query, _ in cursor.executed]
    assert "RETURNING id" in queries[0]
    assert any("DELETE FROM publication_openalex_topics" in query for query in queries)
    assert any("INSERT INTO publication_openalex_affiliations" in query for query in queries)
    assert any("INSERT INTO publication_openalex_topics" in query for query in queries)
    assert any("INSERT INTO publication_openalex_keywords" in query for query in queries)
    assert any("INSERT INTO publication_openalex_concepts" in query for query in queries)
    assert not any("publication_openalex_mesh" in query for query in queries)
    assert any("INSERT INTO publication_openalex_related_works" in query for query in queries)
    assert conn.commits == 1


def test_preview_backed_batch_queries_scope_to_selected_candidates():
    repo, cursor, _ = make_repo()

    assert repo.get_papers_needing_download(limit=25, pipeline_run_id=9) == []

    query, params = cursor.executed[-1]
    assert "NULLIF(pr.config->>'preview_id', '') IS NOT NULL" in query
    assert "dc.included" in query
    assert "dc.pipeline_ready" in query
    assert params == [9, 9, 25]


def test_detection_selection_is_keyed_by_render_hash_and_detector_version():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[{
        "publication_row_id": 1,
        "paper_id": "W1",
        "markdown_artifact_id": 2,
        "path": "paper.md",
        "render_sha256": "abc",
    }]]

    rows = repo.get_markdown_artifacts_needing_detection(
        detector_version="rules-v3", limit=10, pipeline_run_id=9
    )

    assert rows[0]["render_sha256"] == "abc"
    query, params = cursor.executed[-1]
    assert "mention_detection_runs" in query
    assert "dr.render_sha256 = md.sha256" in query
    assert "dr.status = 'successful'" in query
    assert params == ["rules-v3", 9, 9, 10]


def test_zero_candidate_detection_is_persisted_as_successful_completion():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 77}]

    detection_run_id = repo.begin_detection_run(1, 2, "abc", "rules-v3")
    repo.finish_detection_run(detection_run_id, 0, {"candidate_text_fraction": 0})

    assert detection_run_id == 77
    assert any("DELETE FROM mention_candidates" in query for query, _ in cursor.executed)
    finish_query, finish_params = cursor.executed[-1]
    assert "status = 'successful'" in finish_query
    assert finish_params[0] == 0
    assert finish_params[-1] == 77
    assert conn.commits == 2


def test_failed_detection_rolls_back_before_recording_failure():
    repo, cursor, conn = make_repo()

    repo.fail_detection_run(77, "duplicate mention candidate")

    assert conn.rollbacks == 1
    query, params = cursor.executed[-1]
    assert "status = 'failed'" in query
    assert params == ("duplicate mention candidate", 77)
    assert conn.commits == 1


def test_rules_v3_candidate_persistence_carries_full_lineage():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"id": 1}]
    candidate = MentionCandidate(
        publication_id="W1",
        dataset_name="PRJNA123456",
        evidence_text="Data are available as PRJNA123456.",
        char_start=10,
        char_end=45,
        evidence_tier="strong",
        trigger_type="accession",
        trigger_text="PRJNA123456",
        triggers=[{"type": "accession", "text": "PRJNA123456", "tier": "strong"}],
        render_sha256="abc",
    )

    assert repo.upsert_mention_candidates([candidate], detection_run_id=77) == 1
    query, params = cursor.executed[-1]
    assert "detection_run_id" in query
    assert "detector_version" in query
    assert "WHERE detection_run_id IS NOT NULL" in query
    assert 77 in params
    assert "rules-v3" in params


def test_evaluation_queries_are_strictly_run_scoped_and_include_zero_result_papers():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[{"paper_id": "W1", "candidate_count": 0}], []]

    papers = repo.evaluation_paper_rows(9)
    candidates = repo.evaluation_candidate_rows(9)

    assert papers[0]["candidate_count"] == 0
    paper_query, paper_params = cursor.executed[-2]
    candidate_query, candidate_params = cursor.executed[-1]
    assert "dc.pipeline_run_id = %s" in paper_query
    assert "dc.included" in paper_query
    assert paper_params == ("rules-v3", 9)
    assert "dr.render_sha256 = md.sha256" in candidate_query
    assert "dr.status = 'successful'" in candidate_query
    assert candidate_params == ("rules-v3", 9)
    assert candidates == []


def test_discovery_candidate_pages_include_the_full_api_contract():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 0}]
    cursor.fetchall_results = [[]]

    result = repo.list_discovery_candidates(9)

    page_query = cursor.executed[-1][0]
    assert "p.primary_source_name" in page_query
    assert result["items"] == []


def test_selected_discovery_candidates_filter_before_count_and_pagination():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 1}]
    cursor.fetchall_results = [[{"paper_id": "W123"}]]

    result = repo.list_discovery_candidates(9, offset=50, limit=50, selected_only=True)

    count_query, count_params = cursor.executed[-2]
    page_query, page_params = cursor.executed[-1]
    assert "dc.included AND dc.pipeline_ready" in count_query
    assert count_params == (9,)
    assert "dc.included AND dc.pipeline_ready" in page_query
    assert page_params == (9, 50, 50)
    assert result["total"] == 1


def test_insight_rows_resolve_run_specific_discovery_provenance_without_duplicates():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[{"paper_id": "W123", "discovery_mode": "random"}]]

    rows = repo.export_insight_rows(pipeline_run_id=9)

    query, params = cursor.executed[-1]
    assert rows[0]["discovery_mode"] == "random"
    assert "JOIN discovery_candidates dc" in query
    assert "COALESCE(dc.evidence_reasons" in query
    assert "dc.included" in query
    assert "dc.pipeline_ready" in query
    assert "dc.pipeline_run_id = %s" in query
    assert params == (9,)


def test_run_result_rows_start_from_selected_candidates_and_left_join_insights():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[{
        "paper_id": "W123",
        "pdf_downloaded": True,
        "dataset_name": None,
    }]]

    rows = repo.export_run_result_rows(9)

    query, params = cursor.executed[-1]
    assert rows[0]["dataset_name"] is None
    assert "FROM discovery_candidates dc" in query
    assert "LEFT JOIN dataset_mentions dm" in query
    assert "dc.pipeline_run_id = %s" in query
    assert "dc.included" in query
    assert "dc.pipeline_ready" in query
    assert "pis.stage = 'download_pdf'" in query
    assert "artifact.artifact_type = 'pdf'" in query
    assert "artifact.artifact_type = 'tei_xml'" in query
    assert "artifact.artifact_type = 'markdown'" in query
    assert params == (9,)


def test_global_insight_rows_use_latest_provenance_and_label_legacy_rows():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[]]

    assert repo.export_insight_rows() == []

    query, params = cursor.executed[-1]
    assert "dc.updated_at DESC" in query
    assert "COALESCE(discovery.discovery_mode, 'unrecorded')" in query
    assert "COALESCE(discovery.discovery_methods, ARRAY[]::TEXT[])" in query
    assert params == (None, None)


def test_healthcheck_reports_candidate_table_readiness():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [
        {"database": "datasight_pipeline", "user": "postgres"},
        {"relation": "publications"},
        {"relation": "mention_candidates"},
        {"relation": "alembic_version"},
        {"version_num": "20260527_0002"},
        {"installed": True},
    ]

    health = repo.healthcheck()

    assert health["ready"] is True
    assert health["mention_candidates_table"] is True
    assert health["vector_search_ready"] is True


def test_active_run_count_reads_running_statuses():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 2}]

    assert repo.active_run_count() == 2
    query, _ = cursor.executed[-1]
    assert "status IN ('queued', 'running', 'started')" in query


def test_create_pipeline_run_records_creation_event():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 9}]

    run_id = repo.create_pipeline_run("dataset reuse", {"limit": 5})

    assert run_id == 9
    event_query, event_params = cursor.executed[-1]
    assert "INSERT INTO pipeline_run_events" in event_query
    assert event_params[:4] == (9, None, "info", 'Pipeline run created for "dataset reuse".')
    assert conn.commits == 1


def test_record_stage_result_records_stage_event():
    repo, cursor, conn = make_repo()

    repo.record_stage_result(
        stage="discover",
        status="successful",
        metrics={"message": "Discovered 3 publications.", "count": 3},
        pipeline_run_id=9,
    )

    assert "SELECT id, attempt_count" in cursor.executed[0][0]
    assert "INSERT INTO stage_runs" in cursor.executed[1][0]
    event_query, event_params = cursor.executed[2]
    assert "INSERT INTO pipeline_run_events" in event_query
    assert event_params[:4] == (9, "discover", "info", "Discovered 3 publications.")
    assert conn.commits == 1


def test_create_pipeline_items_initializes_item_stages():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 100}, {"id": 200}]

    item_ids = repo.create_pipeline_items(9, ["paper-1"])

    assert item_ids == [200]
    queries = [query for query, _ in cursor.executed]
    assert any("INSERT INTO pipeline_items" in query for query in queries)
    stage_inserts = [params for query, params in cursor.executed if "INSERT INTO pipeline_item_stages" in query]
    assert len(stage_inserts) == 5
    assert stage_inserts[0] == (200, "download_pdf")
    assert stage_inserts[-1] == (200, "extract_features")
    assert conn.commits == 1


def test_extract_features_is_the_terminal_successful_item_stage():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [
        {"pipeline_run_id": 9},
        {
            "total": 1,
            "successful": 1,
            "failed": 0,
            "skipped": 0,
            "queued": 0,
            "running": 0,
            "pending": 0,
        },
        None,
    ]

    repo.finish_item_stage(200, "extract_features", "successful", {"mentions": 2})

    assert any(
        "UPDATE pipeline_items SET status = 'successful'" in query
        for query, _ in cursor.executed
    )


def test_start_item_stage_marks_stage_and_item_running():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [{"id": 10}, {"pipeline_run_id": 9}, {"total": 1, "successful": 0, "failed": 0, "skipped": 0, "active": 1, "pending": 0}, None]

    assert repo.start_item_stage(200, "download_pdf", "task-1") is True

    assert "UPDATE pipeline_item_stages" in cursor.executed[0][0]
    assert "UPDATE pipeline_items SET status = 'running'" in cursor.executed[1][0]
    assert conn.commits >= 1


def test_claim_queued_item_stages_uses_skip_locked():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [[{"pipeline_item_id": 200}, {"pipeline_item_id": 201}]]
    cursor.fetchone_results = [
        {"total": 2, "successful": 0, "failed": 0, "skipped": 0, "queued": 0, "running": 2, "pending": 0},
        None,
    ]

    item_ids = repo.claim_queued_item_stages(9, "download_pdf", 2, "task-1")

    assert item_ids == [200, 201]
    claim_query, claim_params = cursor.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in claim_query
    assert "pis.status = 'queued'" in claim_query
    assert claim_params == (9, "download_pdf", 2, "task-1")


def test_high_throughput_outcome_reports_partial_errors():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"failed": 1, "skipped": 3}]

    assert repo.high_throughput_outcome(9) == "completed_with_errors"
    query, params = cursor.executed[-1]
    assert "pis.stage = ANY(%s)" in query
    assert "match_um_dataset" not in params[1]


def test_terminal_check_ignores_disabled_item_stage_rows():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 0}]

    assert repo.all_item_stages_terminal(9) is True

    query, params = cursor.executed[-1]
    assert "pis.stage = ANY(%s)" in query
    assert "match_um_dataset" not in params[1]


def test_high_throughput_finalization_is_idempotent():
    repo, cursor, conn = make_repo()
    cursor.fetchone_results = [
        {"locked": True},
        {"status": "running"},
        {"count": 0},
        {"failed": 0, "skipped": 0},
        {"locked": True},
        {"status": "successful"},
    ]

    assert repo.finalize_high_throughput_run_if_ready(9) == "successful"
    assert repo.finalize_high_throughput_run_if_ready(9) == "successful"

    finish_updates = [
        params
        for query, params in cursor.executed
        if "UPDATE pipeline_runs SET status" in query
    ]
    finish_events = [
        params
        for query, params in cursor.executed
        if "INSERT INTO pipeline_run_events" in query
    ]
    assert finish_updates == [("successful", 9)]
    assert len(finish_events) == 1
    assert conn.commits == 2


def test_standard_run_outcome_reports_partial_errors():
    repo, cursor, _ = make_repo()
    cursor.fetchone_results = [{"count": 1}]

    assert repo.standard_run_outcome(9) == "completed_with_errors"
    query, params = cursor.executed[-1]
    assert "stage = ANY(%s)" in query
    assert "status IN ('failed', 'completed_with_errors')" in query
    assert params[0] == 9
    assert params[1][-1] == "extract_features"


def test_fail_pipeline_run_records_error_event():
    repo, cursor, conn = make_repo()

    repo.fail_pipeline_run(9, "No UM datasets are imported.")

    assert "UPDATE pipeline_runs" in cursor.executed[0][0]
    event_query, event_params = cursor.executed[1]
    assert "INSERT INTO pipeline_run_events" in event_query
    assert event_params[:4] == (9, None, "error", "Pipeline run failed.")
    assert conn.commits == 1


def test_list_pipeline_run_events_reads_chronological_events():
    repo, cursor, _ = make_repo()
    cursor.fetchall_results = [
        [
            {
                "id": 1,
                "pipeline_run_id": 9,
                "stage": "discover",
                "level": "info",
                "message": "Discovered 3 publications.",
                "payload": {},
            }
        ]
    ]

    events = repo.list_pipeline_run_events(9, limit=10)

    assert events[0]["stage"] == "discover"
    query, params = cursor.executed[-1]
    assert "FROM pipeline_run_events" in query
    assert params == (9, 10)


def test_reset_database_truncates_pipeline_tables_and_preserves_schema():
    repo, cursor, conn = make_repo()
    cursor.fetchall_results = [
        [
            {"tablename": "artifacts", "quoted_table_name": '"artifacts"'},
            {"tablename": "discovery_previews", "quoted_table_name": '"discovery_previews"'},
            {"tablename": "pipeline_runs", "quoted_table_name": '"pipeline_runs"'},
            {
                "tablename": "publication_openalex_topics",
                "quoted_table_name": '"publication_openalex_topics"',
            },
        ]
    ]

    tables = repo.reset_database()

    discovery_query, _ = cursor.executed[-2]
    truncate_query, _ = cursor.executed[-1]
    assert "FROM pg_tables" in discovery_query
    assert "tablename <> 'alembic_version'" in discovery_query
    assert "tablename <> 'um_datasets'" in discovery_query
    assert "TRUNCATE" in truncate_query
    assert '"discovery_previews"' in truncate_query
    assert '"publication_openalex_topics"' in truncate_query
    assert '"um_datasets"' not in truncate_query
    assert tables == [
        "artifacts",
        "discovery_previews",
        "pipeline_runs",
        "publication_openalex_topics",
    ]
    assert conn.commits == 1


def test_reset_database_is_a_noop_when_schema_has_no_pipeline_tables():
    repo, cursor, conn = make_repo()
    cursor.fetchall_results = [[]]

    assert repo.reset_database() == []

    assert len(cursor.executed) == 1
    assert "FROM pg_tables" in cursor.executed[0][0]
    assert conn.commits == 0
