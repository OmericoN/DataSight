"""Shared extraction boundary for standard runs, item workers and manual resume."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import psycopg2.extras

from datasight import config
from datasight.application.extraction_engine import CHUNK_CHARACTERS, OVERLAP_CHARACTERS, ExtractionEngine
from datasight.domain.extraction import PROMPT_VERSION, SCHEMA_VERSION
from datasight.infrastructure.ingestion.file_integrity import sha256_file
from datasight.infrastructure.ingestion.renderer import RENDERER_VERSION, _write_atomic, extract_markdown
from datasight.infrastructure.llm.client import CONTEXT_TOKENS, MAX_PROMPT_CHARS, MAX_TOKENS, TEMPLATE_RESERVE, GatewayClient, InferenceUnavailable
from datasight.infrastructure.persistence.repository import PipelineRepository


def extraction_configuration(legacy: bool | None = None) -> dict[str, Any]:
    if legacy is None:
        strategy = os.getenv('LLM_EXTRACTION_STRATEGY', 'legacy')
        if strategy not in {'legacy', 'bounded'}:
            raise ValueError('LLM_EXTRACTION_STRATEGY must be legacy or bounded')
        legacy = strategy == 'legacy'
    settings = {
        "schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION if legacy else 'indexed-sparse-v10',
        "llm_model": config.LLM_MODEL, "model_revision": config.LLM_MODEL_REVISION,
        "renderer_version": RENDERER_VERSION, "render_profile": "full_body",
        "chunk_characters": CHUNK_CHARACTERS if legacy else 6000, "overlap_characters": OVERLAP_CHARACTERS,
        "feature_batching": "group_then_bisect_v1" if legacy else 'sparse_groups_occurrences8_v2',
        "context_tokens": CONTEXT_TOKENS, "max_prompt_characters": MAX_PROMPT_CHARS,
        "template_reserve_tokens": TEMPLATE_RESERVE,
        "temperature": 0, "max_tokens": MAX_TOKENS, "enable_thinking": False,
    }
    if not legacy:
        settings.update({'ordinary_seconds': 300, 'ordinary_attempts': 50,
                         'long_seconds': 900, 'long_attempts': 100,
                         'long_source_characters': 60000, 'long_reference_occurrences': 30,
                         'occurrence_batch_size': 8})
    return settings


def fingerprint(source_hash: str, settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"source_sha256": source_hash, **settings}, sort_keys=True).encode()).hexdigest()


def prepare_source(row: dict[str, Any]) -> tuple[str, Path, str]:
    if row.get("xml_path"):
        text = extract_markdown(Path(row["xml_path"]), profile="full_body")
    else:
        metadata = row.get("markdown_metadata") or {}
        if metadata.get("profile") != "full_body" or metadata.get("producer_version") != RENDERER_VERSION:
            raise ValueError("Retained TEI is required to prepare complete full-body source")
        path = Path(row["markdown_path"])
        if sha256_file(path) != row.get("markdown_sha256"):
            raise ValueError("Markdown hash mismatch; rerender required")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("Full-body source is empty")
    raw = text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    destination = config.EXTRACTIONS_DIR / "sources" / f"{digest}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != digest:
            raise ValueError("Immutable source snapshot has changed")
    else:
        _write_atomic(destination, raw)
    return text, destination, digest


def extract_publication(repo: Any, client: GatewayClient, run_id: int, row: dict[str, Any]) -> dict[str, Any]:
    previous = repo.latest_publication_extraction(run_id, row["publication_id"])
    frozen = (repo.get_pipeline_run(run_id) or {}).get('config', {}).get('extraction') if hasattr(repo, 'get_pipeline_run') else None
    settings = frozen or extraction_configuration()
    try:
        if previous and previous.get("source_path"):
            if previous["config"] != settings:
                raise ValueError("Extraction configuration changed; start a new run")
            path = Path(previous["source_path"])
            digest = sha256_file(path)
            if digest != previous["source_sha256"]:
                raise ValueError("Immutable source snapshot has changed")
            text = path.read_text(encoding="utf-8")
        else:
            text, path, digest = row.get('_prepared_source') or prepare_source(row)
    except Exception as exc:
        if previous:
            repo.fail_publication_extraction(previous["id"], "failed", str(exc))
            return {"paper_id": row["paper_id"], "status": "failed", "error": str(exc)}
        key = fingerprint("preparation:" + str(row.get("markdown_sha256") or row.get("xml_path")), settings)
        record = repo.begin_publication_extraction(run_id, row["publication_id"], key, settings)
        repo.fail_publication_extraction(record["id"], "failed", f"Source preparation failed: {exc}")
        return {"paper_id": row["paper_id"], "status": "failed", "error": str(exc)}
    record = repo.begin_publication_extraction(run_id, row["publication_id"], fingerprint(digest, settings), settings)
    extraction_id = int(record["id"])
    if record["status"] == "successful":
        return {"paper_id": row["paper_id"], "status": "successful", "datasets": len(record["result"]["datasets"]), "cached": True}
    repo.cursor.execute("""
        INSERT INTO artifacts(publication_id,artifact_type,path,sha256,bytes,metadata)
        VALUES (%s,'extraction_source',%s,%s,%s,%s)
        ON CONFLICT(publication_id,artifact_type,path) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id
    """, (row["publication_id"], str(path), digest, path.stat().st_size, psycopg2.extras.Json({"profile": "full_body", "producer_version": RENDERER_VERSION})))
    artifact_id = repo.cursor.fetchone()["id"]
    repo.attach_extraction_source(extraction_id, str(path), digest, artifact_id)
    try:
        from datasight.application.bounded_extraction import BoundedExtractionEngine
        budget = BoundedExtractionEngine(repo, client, extraction_id, text)
        client.extraction_budget = budget
        try:
            if settings['prompt_version'] == PROMPT_VERSION:
                result = ExtractionEngine(repo, client, extraction_id, text).run(row['paper_id'], digest)
                budget.remaining()
            else:
                result = budget.run(row['paper_id'], digest)
        finally:
            try:
                if settings['prompt_version'] == PROMPT_VERSION:
                    budget.progress()
            finally:
                client.before_attempt = None
                client.remaining_time = None
                client.extraction_budget = None
        repo.publish_extraction(extraction_id, result)
        return {"paper_id": row["paper_id"], "status": "successful", "datasets": len(result.datasets), "extraction_id": extraction_id}
    except InferenceUnavailable as exc:
        repo.fail_publication_extraction(extraction_id, "interrupted", str(exc))
        repo.interrupt_extraction_run(run_id, str(exc))
        return {"paper_id": row["paper_id"], "status": "interrupted", "error": str(exc)}
    except Exception as exc:
        repo.fail_publication_extraction(extraction_id, "failed", str(exc))
        return {"paper_id": row["paper_id"], "status": "failed", "error": str(exc)}


def extract_run(run_id: int | None, limit: int | None = None, publication_id: int | None = None) -> dict[str, Any]:
    if run_id is None:
        raise ValueError("Whole-publication extraction requires an existing pipeline run ID")
    client = GatewayClient()
    try:
        with PipelineRepository() as repo:
            if not repo.extraction_lock(run_id, publication_id):
                raise ValueError("This run already has active extraction")
            run = repo.get_pipeline_run(run_id)
            if not run:
                raise LookupError("Pipeline run not found")
            frozen = run.get("config", {}).get("extraction")
            if frozen and frozen not in (extraction_configuration(legacy=False), extraction_configuration(legacy=True)):
                raise ValueError("Run extraction configuration differs from this worker")
            if run["status"] == "interrupted":
                return {"stage": "extract_features", "status": "interrupted", "count": 0, "message": "Inference interrupted; resume explicitly.", "payload": {}}
            rows = repo.extraction_publications(run_id, limit, publication_id)
            results: list[dict[str, Any]] = []
            prepared = []
            for row in rows:
                previous = repo.latest_publication_extraction(run_id, row['publication_id'])
                if not previous or not previous.get('source_path'):
                    try:
                        row['_prepared_source'] = prepare_source(row)
                    except Exception:
                        results.append(extract_publication(repo, client, run_id, row))
                        continue
                prepared.append(row)
            for row in prepared:
                result = extract_publication(repo, client, run_id, row)
                results.append(result)
                if result["status"] == "interrupted":
                    break
            successful = sum(item["status"] == "successful" for item in results)
            failed = sum(item["status"] == "failed" for item in results)
            interrupted = any(item["status"] == "interrupted" for item in results)
            status = "interrupted" if interrupted else "completed_with_errors" if failed and successful else "failed" if failed else "successful" if results else "skipped"
            output = {"stage": "extract_features", "status": status, "count": sum(item.get("datasets", 0) for item in results),
                      "message": f"Extracted {successful} publications; {failed} failed; {sum(item['status'] == 'interrupted' for item in results)} interrupted.",
                      "payload": {"publications": len(results), "successful": successful, "failed": failed, "results": results}}
            repo.record_stage_result("extract_features", status, output, run_id)
            return output
    finally:
        client.close()


def resume_extraction(run_id: int) -> dict[str, Any]:
    result = extract_run(run_id)
    with PipelineRepository() as repo:
        repo.finish_resumed_extraction_items(run_id)
        status = result["status"]
        if status == "successful":
            status = repo.standard_run_outcome(run_id)
        repo.finish_pipeline_run(run_id, status)
    return {"pipeline_run_id": run_id, **result}


def queue_resume(run_id: int) -> dict[str, Any]:
    with PipelineRepository() as repo:
        if not repo.extraction_lock(run_id):
            raise ValueError("This run already has active extraction")
        repo.cursor.execute("""SELECT DISTINCT config FROM publication_extractions e WHERE pipeline_run_id=%s
            AND id=(SELECT max(id) FROM publication_extractions latest WHERE latest.pipeline_run_id=e.pipeline_run_id AND latest.publication_id=e.publication_id)""", (run_id,))
        if any(row["config"] not in (extraction_configuration(legacy=False), extraction_configuration(legacy=True)) for row in repo.cursor.fetchall()):
            raise ValueError("Extraction configuration changed; start a new run to preserve reproducibility")
        repo.claim_extraction_resume(run_id)
    from datasight.infrastructure.worker.tasks import resume_extractions
    try:
        task = resume_extractions.delay(run_id)
    except Exception:
        with PipelineRepository() as repo:
            repo.interrupt_extraction_run(run_id, "Resume could not be queued")
        raise
    return {"pipeline_run_id": run_id, "task_id": task.id, "status": "queued"}
