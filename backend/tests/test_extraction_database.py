"""Opt-in PostgreSQL integration tests; never use the application's DSN."""

import os
from hashlib import sha256
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
from datasight.domain.results import ConversionResult, DownloadResult, RenderResult
from datasight.infrastructure.persistence.repository import PipelineRepository
from datasight.application import llm_extraction, pipeline_services
from datasight.domain.candidate_detection import SCREENING_VERSION
from datasight.infrastructure.ingestion.renderer import LEGACY_RENDERER_VERSION, RENDERER_VERSION
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
    config = {"extraction": llm_extraction.extraction_configuration(legacy=True)}
    run1 = repo.create_pipeline_run("one", config)
    run2 = repo.create_pipeline_run("two", config)
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


def test_publish_scopes_passages_to_dataset():
    from unittest.mock import MagicMock

    repo = MagicMock()
    repo.cursor.fetchone.return_value = {"publication_id": 1, "status": "running", "config": {}}
    publication = result()
    other = publication.evidence[0].model_copy(update={"evidence_id": "e2"})
    support = other.model_copy(update={"evidence_id": "e3"})
    publication.evidence.extend([other, support])
    publication.datasets[0].identity.name = type(publication.datasets[0].identity.name).model_validate({
        "state": "reported", "values": [{"value": "Alpha", "evidence_ids": ["e3"]}],
    })
    PipelineRepository.publish_extraction(repo, 1, publication)
    insert = next(call for call in repo.cursor.execute.call_args_list if "INSERT INTO dataset_mentions" in call.args[0])
    assert [item["evidence_id"] for item in insert.args[1][4].adapted["passages"]] == ["e1", "e3"]


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


def test_discovery_skips_terminal_publications_and_keeps_unfinished_ones(database):
    repo = database
    prior_run, current_run, publication = seed(repo)
    extraction = repo.begin_publication_extraction(prior_run, publication, "zero", {})
    repo.publish_extraction(extraction["id"], result().model_copy(update={"datasets": []}))

    for paper_id, decision in (("W2", "no_use"), ("W3", "likely_use")):
        repo.cursor.execute(
            "INSERT INTO publications(paper_id,title,source) VALUES (%s,%s,'test') RETURNING id",
            (paper_id, paper_id),
        )
        publication_id = repo.cursor.fetchone()["id"]
        repo.cursor.execute(
            """INSERT INTO artifacts(publication_id,artifact_type,path,sha256)
               VALUES (%s,'markdown',%s,'hash') RETURNING id""",
            (publication_id, f"{paper_id}.md"),
        )
        artifact_id = repo.cursor.fetchone()["id"]
        repo.record_mention_screening(
            publication_id, artifact_id, "hash", SCREENING_VERSION, {"decision": decision},
        )
    assert repo.processed_discovery_paper_ids(["W1", "W2", "W3", "W4"], current_run) == {"W1", "W2"}
    assert repo.processed_discovery_paper_ids(["W1"], prior_run) == set()


def test_pdf_pool_replaces_failed_download_and_preserves_lineage_across_connections(database, tmp_path):
    repo = database
    old_run = repo.create_pipeline_run("previous", {})
    run = repo.create_pipeline_run("replacement test", {"preview_id": "preview", "validated_pdf_target": True})
    candidates = [
        {"paperId": "W1", "included": True, "exclusion_reason": None},
        {"paperId": "W2", "included": False, "exclusion_reason": "Excluded in preview"},
        {"paperId": "W3", "included": False, "exclusion_reason": "Processing limit reached"},
        {"paperId": "W4", "included": False, "exclusion_reason": "Processing limit reached"},
    ]
    for candidate in candidates:
        candidate.update(title=candidate["paperId"], pipeline_ready=True, open_access_url="https://example.org/paper.pdf")
    repo.upsert_publications(candidates, source="test")
    repo.persist_discovery_candidates(old_run, candidates[:1])
    repo.finish_pipeline_run(old_run, "completed_with_errors")
    repo.persist_discovery_candidates(run, candidates)

    original = repo.next_pool_download(run, 1)
    assert original is not None and original["paperId"] == "W1"
    repo.persist_download_results([DownloadResult("W1", False, "Access blocked", failure_category="access_blocked")], run)
    replacement = repo.next_pool_download(run, 1)
    assert replacement is not None and replacement["paperId"] == "W3"
    # A restarted worker must reclaim the same uncommitted slot.
    dsn = make_url(config.POSTGRES_URI).set(drivername="postgresql").render_as_string(hide_password=False)
    with PipelineRepository(dsn) as restarted:
        resumed = restarted.next_pool_download(run, 1)
        assert resumed is not None and resumed["paperId"] == "W3"
        pdf = tmp_path / "W3.pdf"
        pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
        pdf_hash = sha256(pdf.read_bytes()).hexdigest()
        restarted.persist_download_results([DownloadResult("W3", True, "Validated", filepath=pdf, sha256=pdf_hash)], run)
        assert restarted.next_pool_download(run, 1) is None
        assert restarted.pool_download_counts(run) == {"attempted": 2, "validated": 1, "failed": 1, "replacements": 1}
    assert repo.list_discovery_candidates(old_run)["items"][0]["download_status"] == "not_attempted"
    assert repo.included_discovery_paper_ids(run) == ["W3"]
    assert [paper["paperId"] for paper in repo.get_papers_needing_conversion(pipeline_run_id=run)] == ["W3"]

    xml = tmp_path / "W3.xml"
    xml.write_bytes(b"<TEI/>")
    stale = ConversionResult("W3", True, "Converted", xml_path=xml, source_sha256="other PDF")
    assert repo.persist_conversion_results([stale], run) == 0
    assert not stale.success
    assert not repo.get_papers_needing_rendering(pipeline_run_id=run)
    conversion = ConversionResult("W3", True, "Converted", xml_path=xml, source_sha256=pdf_hash)
    assert repo.persist_conversion_results([conversion], run) == 1
    assert [paper["paperId"] for paper in repo.get_papers_needing_rendering(pipeline_run_id=run)] == ["W3"]
    markdown = tmp_path / "W3.md"
    markdown.write_text("Run-verified content", encoding="utf8")
    rendered = RenderResult("W3", xml, markdown, True, "Rendered", source_sha256=sha256(xml.read_bytes()).hexdigest())
    assert not repo.get_markdown_artifacts_needing_detection(pipeline_run_id=run)
    assert repo.persist_render_results([rendered], run) == 1
    assert [row["paper_id"] for row in repo.get_markdown_artifacts_needing_detection(pipeline_run_id=run)] == ["W3"]


def test_single_stage_run_finishes_when_its_stage_finishes(database):
    repo = database
    automatic = repo.create_pipeline_run("preview launch", {"preview_id": "preview"})
    standalone = repo.create_pipeline_run("manual render", {"stage": "render_document"})
    for run_id in (automatic, standalone):
        repo.record_stage_start("render_document", run_id)
        assert repo.active_run_count() == 2
        repo.record_stage_result("render_document", "completed_with_errors", pipeline_run_id=run_id)
    assert repo.get_pipeline_run(automatic)["status"] == "running"
    finished = repo.get_pipeline_run(standalone)
    assert finished["status"] == "completed_with_errors" and finished["finished_at"] is not None
    assert repo.active_run_count() == 1


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
    assert repo.extraction_publications(run1) == []
    repo.cursor.execute("""INSERT INTO artifacts(publication_id,artifact_type,path,sha256)
        VALUES (%s,'markdown',%s,'rendered-hash') RETURNING id""", (publication, str(tmp_path / "paper.md")))
    markdown_id = repo.cursor.fetchone()["id"]
    stale = repo.begin_detection_run(publication, markdown_id, "old-hash")
    repo.finish_detection_run(stale, 1)
    detected = repo.begin_detection_run(publication, markdown_id, "rendered-hash")
    assert repo.extraction_publications(run1) == []
    repo.fail_detection_run(detected, "detector error")
    assert repo.extraction_publications(run1) == []
    repo.finish_detection_run(detected, 0)
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


@pytest.mark.parametrize("decision", ["likely_use", "uncertain", "no_use"])
def test_screening_controls_batch_eligibility_and_item_dispatch(database, tmp_path, monkeypatch, decision):
    repo = database
    run_id, _, publication = seed(repo)
    repo.cursor.execute(
        "UPDATE pipeline_runs SET config = config || %s WHERE id = %s",
        (psycopg2.extras.Json({"detector_version": SCREENING_VERSION, "render_profile": "full_body"}), run_id),
    )
    path = tmp_path / "source.md"
    raw = b"This publication is the source presented for testing."
    path.write_bytes(raw)
    repo.cursor.execute(
        """INSERT INTO artifacts(publication_id,artifact_type,path,sha256,metadata)
           VALUES (%s,'markdown',%s,%s,%s)""",
        (publication, str(path), sha256(raw).hexdigest(),
         psycopg2.extras.Json({"profile": "full_body", "producer_version": RENDERER_VERSION})),
    )
    repo.conn.commit()

    class DecisionClient:
        calls = 0

        def complete(self, payload, response_model, record_attempt, validate, *, single_attempt):
            self.__class__.calls += 1
            return response_model(decision=decision)

        def close(self):
            pass

    monkeypatch.setattr(pipeline_services, "GatewayClient", DecisionClient)
    monkeypatch.setattr(pipeline_services, "PipelineRepository", lambda: PipelineRepository(make_url(config.POSTGRES_URI).set(drivername="postgresql").render_as_string(hide_password=False)))
    batch = pipeline_services.detect_mentions_batch(pipeline_run_id=run_id)
    assert batch["count"] == 1
    assert batch["payload"]["new_decisions"] == 1
    assert batch["payload"]["reused_decisions"] == 0
    assert batch["payload"]["decisions"][decision] == 1
    assert repo.evaluation_paper_rows(run_id)[0]["detection_metrics"]["decision"] == decision
    assert bool(repo.extraction_publications(run_id)) == (decision != "no_use")

    repeated = pipeline_services.detect_mentions_batch(pipeline_run_id=run_id)
    assert repeated["count"] == repeated["payload"]["documents"] == 1
    assert repeated["payload"]["new_decisions"] == 0
    assert repeated["payload"]["reused_decisions"] == 1
    assert repeated["payload"]["decisions"][decision] == 1
    assert repeated["payload"]["forwarded"] == int(decision != "no_use")

    item_id, = repo.create_pipeline_items(run_id, ["W1"])
    repo.queue_item_stage_for_run(run_id, PipelineStage.DETECT_MENTIONS)
    item = pipeline_services.detect_mentions_pipeline_item(item_id)
    assert item["status"] == "successful"
    assert item["payload"]["decision"] == decision
    assert item["payload"]["screening_reused"] is True
    assert DecisionClient.calls == 1
    repo.cursor.execute(
        """SELECT pis.status AS stage_status, pi.status AS item_status
           FROM pipeline_item_stages pis JOIN pipeline_items pi ON pi.id=pis.pipeline_item_id
           WHERE pi.id=%s AND pis.stage='extract_features'""",
        (item_id,),
    )
    row = repo.cursor.fetchone()
    assert row["stage_status"] == ("skipped" if decision == "no_use" else "queued")
    if decision == "no_use":
        assert row["item_status"] == "successful"
        assert repo.high_throughput_outcome(run_id) == "successful"
        repo.finish_resumed_extraction_items(run_id)
        assert repo.get_pipeline_item_context(item_id)["item_status"] == "successful"


def test_runs_keep_their_renderer_artifact_after_a_new_version_is_rendered(database):
    repo = database
    old_run, new_run, publication = seed(repo)
    for run_id, version in ((old_run, LEGACY_RENDERER_VERSION), (new_run, RENDERER_VERSION)):
        frozen = {**llm_extraction.catalog_configuration("pruned"), "renderer_version": version}
        repo.cursor.execute(
            "UPDATE pipeline_runs SET config = %s WHERE id = %s",
            (psycopg2.extras.Json({"renderer_version": version, "render_profile": "pruned", "extraction": frozen}),
             run_id),
        )
        repo.cursor.execute(
            """INSERT INTO artifacts(publication_id,artifact_type,path,sha256,metadata)
               VALUES (%s,'markdown',%s,%s,%s) RETURNING id""",
            (publication, f"{version}/paper.md", version,
             psycopg2.extras.Json({"profile": "pruned", "producer_version": version})),
        )
        artifact_id = repo.cursor.fetchone()["id"]
        artifact, = repo.get_markdown_artifacts_needing_detection(renderer_version=version, pipeline_run_id=run_id)
        assert artifact["markdown_artifact_id"] == artifact_id
        detection_id = repo.begin_detection_run(publication, artifact_id, version)
        repo.finish_detection_run(detection_id, 0)

    for run_id, version in ((old_run, LEGACY_RENDERER_VERSION), (new_run, RENDERER_VERSION)):
        assert llm_extraction.require_compatible_run(repo, run_id)["config"]["renderer_version"] == version
        assert repo.extraction_publications(run_id)[0]["markdown_sha256"] == version
        assert repo.evaluation_paper_rows(run_id)[0]["markdown_sha256"] == version
