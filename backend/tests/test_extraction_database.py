"""Opt-in PostgreSQL integration tests; never use the application's DSN."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg2
import psycopg2.extras
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from datasight import config
from datasight.domain.extraction import DatasetEntry, Evidence, Occurrence, PublicationExtraction
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.application import llm_extraction
from datasight.infrastructure.llm.client import InferenceUnavailable
from datasight.domain.stages import PipelineStage
from test_llm_extraction import GroundedClient


@pytest.fixture
def database(monkeypatch):
    uri = os.getenv("DATASIGHT_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("Set DATASIGHT_TEST_POSTGRES_URI to an isolated test database")
    url = make_url(uri)
    if "test" not in (url.database or ""):
        pytest.fail("Integration tests require a database with 'test' in its name")
    schema = "extraction_test_" + uuid4().hex
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    test_url = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    monkeypatch.setattr(config, "POSTGRES_URI", test_url.render_as_string(hide_password=False))
    backend = Path(__file__).resolve().parents[1]
    alembic = Config(str(backend / "alembic.ini"))
    alembic.set_main_option("script_location", str(backend / "migrations"))
    try:
        command.upgrade(alembic, "head")
        with PipelineRepository(test_url.set(drivername="postgresql").render_as_string(hide_password=False)) as repo:
            yield repo
        command.downgrade(alembic, "20260824_0012")
        command.upgrade(alembic, "head")
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def seed(repo):
    run1 = repo.create_pipeline_run("one", {"extraction": {"schema_version": "v1"}})
    run2 = repo.create_pipeline_run("two", {"extraction": {"schema_version": "v1"}})
    repo.cursor.execute("INSERT INTO publications(paper_id,title,source) VALUES ('W1','Paper','test') RETURNING id")
    publication_id = repo.cursor.fetchone()["id"]
    for run in (run1, run2):
        repo.cursor.execute("""INSERT INTO discovery_candidates(pipeline_run_id,publication_id,candidate_strength,evidence_tier,
            evidence_reasons,pipeline_ready,included) VALUES (%s,%s,90,'direct',ARRAY['dataset_citation'],true,true)""", (run, publication_id))
    repo.conn.commit()
    return run1, run2, publication_id


def result():
    evidence = Evidence(evidence_id="e1", block_id="b1", quote="Alpha", char_start=0, char_end=5,
                        section_heading="Methods", standardized_section="methods", placement="body")
    return PublicationExtraction(paper_id="W1", source_sha256="hash", evidence=[evidence],
        datasets=[DatasetEntry(dataset_local_id="d1", display_name="Alpha", occurrences=[Occurrence(
            occurrence_id="o1", evidence_ids=["e1"], bibliography_only=False)])],
        coverage={"source_characters": 5, "covered_characters": 5})


def test_migration_atomic_publish_zero_result_and_run_isolation(database):
    repo = database
    run1, run2, publication = seed(repo)
    repo.cursor.execute("UPDATE publications SET open_access_url='https://example.test/selected.pdf' WHERE id=%s", (publication,))
    repo.cursor.execute("INSERT INTO publications(paper_id,title,source,open_access_url) VALUES ('W-excluded','Excluded','test','https://example.test/other.pdf')")
    repo.conn.commit()
    assert [row['paperId'] for row in repo.get_papers_needing_download(pipeline_run_id=run1)] == ['W1']
    first = repo.begin_publication_extraction(run1, publication, "first", {"schema_version": "v1", 'prompt_version': 'test-strategy'})
    repo.attach_extraction_source(first["id"], "snapshot.md", "hash", None)
    repo.record_extraction_progress(first['id'], {'phase': 'locator', 'attempts_this_invocation': 1})
    repo.begin_extraction_request(first["id"], "scan1", {"messages": []})
    repo.record_extraction_attempt(first["id"], "scan1", {"elapsed_seconds": 0.1, "response": {"usage": {"total_tokens": 10}}})
    repo.finish_extraction_request(first["id"], "scan1", {"passages": []}, None)
    repo.publish_extraction(first["id"], result())
    assert repo.extraction_detail(run1, first['id'])['result']['prompt_version'] == 'test-strategy'
    assert repo.extraction_detail(run1, first['id'])['metrics']['progress']['phase'] == 'locator'
    repo.publish_extraction(first["id"], result())
    assert len(repo.export_insight_rows(run1)) == 1
    assert repo.export_insight_rows(run2) == []
    assert repo.extraction_detail(run2, first["id"]) is None
    assert repo.extraction_detail(run1, first["id"])["requests"][0]["total_tokens"] == 10
    assert repo.evaluation_request_attempt_rows(run1)[0]["total_tokens"] == 10
    assert repo.evaluation_request_attempt_rows(run2) == []
    zero = repo.begin_publication_extraction(run2, publication, "zero", {"schema_version": "v1"})
    repo.publish_extraction(zero["id"], result().model_copy(update={"datasets": []}))
    assert repo.list_extractions(run2)[0][0]["dataset_count"] == 0
    assert repo.export_run_result_rows(run2)[0]["dataset_name"] is None
    assert len(repo.export_insight_rows(run1)) == 1
    assert repo.export_insight_rows() == []  # newest global result is a completed zero


def test_distinct_papers_share_run_lock_and_only_original_task_reclaims(database):
    repo = database
    run1, _, publication = seed(repo)
    item_id = repo.create_pipeline_items(run1, ["W1"])[0]
    repo.cursor.execute("UPDATE pipeline_item_stages SET status='queued' WHERE pipeline_item_id=%s AND stage='extract_features'", (item_id,))
    repo.conn.commit()
    assert repo.claim_queued_item_stages(run1, PipelineStage.EXTRACT_FEATURES, 1, "task-1") == [item_id]
    assert repo.claim_queued_item_stages(run1, PipelineStage.EXTRACT_FEATURES, 1, "task-2") == []
    assert repo.claim_queued_item_stages(run1, PipelineStage.EXTRACT_FEATURES, 1, "task-1") == [item_id]
    assert repo.extraction_lock(run1, publication)
    repo.cursor.execute("SELECT current_schema()")
    schema = repo.cursor.fetchone()["current_schema"]
    url = make_url(os.environ["DATASIGHT_TEST_POSTGRES_URI"]).update_query_dict({"options": f"-csearch_path={schema},public"})
    with PipelineRepository(url.set(drivername="postgresql").render_as_string(hide_password=False)) as another:
        assert another.extraction_lock(run1, publication + 1)
        assert not another.extraction_lock(run1)


def test_interrupt_cannot_be_overwritten_and_resume_is_exclusive(database):
    repo = database
    run1, _, publication = seed(repo)
    extraction = repo.begin_publication_extraction(run1, publication, "one", {})
    repo.fail_publication_extraction(extraction["id"], "interrupted", "GPU off")
    repo.interrupt_extraction_run(run1, "GPU off")
    repo.finish_pipeline_run(run1, "successful")
    assert repo.get_pipeline_run(run1)["status"] == "interrupted"
    repo.claim_extraction_resume(run1)
    with pytest.raises(ValueError, match="active"):
        repo.claim_extraction_resume(run1)
    repo.conn.rollback()
    items = repo.create_pipeline_items(run1, ["W1"])
    repo.cursor.execute("UPDATE pipeline_item_stages SET status='successful' WHERE pipeline_item_id=%s", (items[0],))
    repo.conn.commit()
    repo.publish_extraction(extraction["id"], result())
    repo.finish_resumed_extraction_items(run1)
    repo.cursor.execute("SELECT status FROM pipeline_items WHERE id=%s", (items[0],))
    assert repo.cursor.fetchone()["status"] == "successful"


def test_whole_publication_service_persists_and_resumes_with_frozen_source(database, tmp_path, monkeypatch):
    repo = database
    run1, run2, publication = seed(repo)
    monkeypatch.setattr(config, "EXTRACTIONS_DIR", tmp_path / "snapshots")
    xml = tmp_path / "paper.xml"
    xml.write_text('<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><p>We used the Alpha cohort. Access is controlled.</p></body></text></TEI>')
    repo.cursor.execute("INSERT INTO artifacts(publication_id,artifact_type,path) VALUES (%s,'tei_xml',%s)", (publication, str(xml)))
    repo.conn.commit()
    row = repo.extraction_publications(run1)[0]
    client = GroundedClient()
    assert llm_extraction.extract_publication(repo, client, run1, row)["status"] == "successful"
    calls = client.calls
    assert llm_extraction.extract_publication(repo, client, run1, row)["cached"]
    assert client.calls == calls
    assert repo.export_insight_rows(run1)[0]["confidence"] is None

    class Unavailable(GroundedClient):
        def complete(self, *args, **kwargs):
            raise InferenceUnavailable("GPU off")

    assert llm_extraction.extract_publication(repo, Unavailable(), run2, row)["status"] == "interrupted"
    frozen = repo.latest_publication_extraction(run2, publication)["source_sha256"]
    xml.write_text("changed upstream XML must not affect a resumed snapshot")
    repo.claim_extraction_resume(run2)
    assert llm_extraction.extract_publication(repo, GroundedClient(), run2, row)["status"] == "successful"
    assert repo.latest_publication_extraction(run2, publication)["source_sha256"] == frozen
    assert repo.export_insight_rows(run1)[0]["features"]["display_name"] == "Alpha cohort"
