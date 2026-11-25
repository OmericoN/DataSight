"""Shared extraction boundary for standard runs, item workers and manual resume."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import psycopg2.extras

from datasight import config
from datasight.application.bounded_extraction import BoundedExtractionEngine, PROMPT_VERSION as BOUNDED_PROMPT_VERSION
from datasight.application.extraction_engine import CHUNK_CHARACTERS, LOCATOR_BLOCK_TOKENS, OVERLAP_CHARACTERS, ExtractionEngine
from datasight.application.catalog_extraction import (
    CATALOG_PROMPT_VERSIONS, CATALOG_SPLIT_DEPTH, CATALOG_SPLIT_VERSIONS,
    PROMPT_VERSION as CATALOG_PROMPT_VERSION, run_catalog_extraction,
)
from datasight.application.whole_document_extraction import (
    PROMPT_VERSION as WHOLE_DOCUMENT_PROMPT_VERSION,
    extract_whole_document,
    publication_result,
    read_frozen_source,
    whole_document_settings,
)
from datasight.domain.extraction import PROMPT_VERSION, SCHEMA_VERSION, PublicationExtraction
from datasight.infrastructure.ingestion.file_integrity import sha256_file
from datasight.infrastructure.ingestion.renderer import (
    DEFAULT_RENDER_PROFILE, RENDERER_VERSION, RENDERER_VERSIONS, RenderProfile, _write_atomic, extract_markdown,
)
from datasight.infrastructure.llm.client import CONTEXT_TOKENS, MAX_PROMPT_CHARS, MAX_TOKENS, TEMPLATE_RESERVE, GatewayClient, InferenceUnavailable
from datasight.infrastructure.llm.tokenizer import TOKENIZER_REVISION, TOKENIZER_SHA256
from datasight.infrastructure.persistence.repository import PipelineRepository


def extraction_configuration(
    legacy: bool | None = None, *, render_profile: RenderProfile = DEFAULT_RENDER_PROFILE,
) -> dict[str, Any]:
    if legacy is None:
        strategy = os.getenv('LLM_EXTRACTION_STRATEGY', 'catalog')
        if strategy in {'catalog', 'fast'}:
            return catalog_configuration(render_profile)
        if strategy == 'whole_document':
            return whole_document_configuration()
        if strategy not in {'legacy', 'bounded'}:
            raise ValueError('LLM_EXTRACTION_STRATEGY must be catalog, whole_document, bounded, or legacy')
        legacy = strategy == 'legacy'
    settings = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION if legacy else BOUNDED_PROMPT_VERSION,
        "llm_model": config.LLM_MODEL,
        "model_revision": config.LLM_MODEL_REVISION,
        "renderer_version": RENDERER_VERSION,
        "render_profile": "full_body",
        "chunk_characters": None,
        "fallback_chunk_characters": CHUNK_CHARACTERS if legacy else None,
        "locator_block_tokens": LOCATOR_BLOCK_TOKENS,
        "locator_block_policy": "model_tokens_paragraph_v1" if legacy else "indexed_model_tokens_paragraph_v1",
        "overlap_characters": OVERLAP_CHARACTERS,
        "prompt_budget_version": "model_tokenizer_v1",
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "feature_batching": "group_then_bisect_v1" if legacy else 'fused_singletons_budgeted_occurrences_v1',
        "context_tokens": CONTEXT_TOKENS,
        "max_prompt_characters": MAX_PROMPT_CHARS,
        "template_reserve_tokens": TEMPLATE_RESERVE,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "enable_thinking": False,
    }
    if not legacy:
        settings.update({
            'ordinary_seconds': 300,
            'ordinary_attempts': 50,
            'long_seconds': 900,
            'long_attempts': 100,
            'long_source_characters': 60000,
            'long_reference_occurrences': 30,
            'occurrence_batch_size': 8,
        })
    return settings


def whole_document_configuration() -> dict[str, Any]:
    return {
        **whole_document_settings(),
        "schema_version": SCHEMA_VERSION,
        "llm_model": config.LLM_MODEL,
        "renderer_version": RENDERER_VERSION,
        "render_profile": "full_body",
    }


def catalog_configuration(render_profile: RenderProfile, prompt_version: str = CATALOG_PROMPT_VERSION) -> dict[str, Any]:
    return {
        **whole_document_configuration(),
        "prompt_version": prompt_version,
        "render_profile": render_profile,
        "max_output_splits_per_chunk": CATALOG_SPLIT_DEPTH if prompt_version in CATALOG_SPLIT_VERSIONS else 1,
    }


def supported_extraction_configurations() -> tuple[dict[str, Any], ...]:
    current = (
        *(catalog_configuration(profile, version) for version in CATALOG_PROMPT_VERSIONS for profile in ("pruned", "full_body")),
        whole_document_configuration(), extraction_configuration(legacy=False), extraction_configuration(legacy=True),
    )
    return tuple({**settings, "renderer_version": version} for version in RENDERER_VERSIONS for settings in current)


def fingerprint(source_hash: str, settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"source_sha256": source_hash, **settings}, sort_keys=True).encode()).hexdigest()


def prepare_source(row: dict[str, Any], *, renderer_version: str = RENDERER_VERSION) -> tuple[str, Path, str]:
    if row.get("xml_path"):
        text = extract_markdown(Path(row["xml_path"]), profile="full_body", version=renderer_version)
    else:
        metadata = row.get("markdown_metadata") or {}
        if metadata.get("profile") != "full_body" or metadata.get("producer_version") != renderer_version:
            raise ValueError("Retained TEI is required to prepare complete full-body source")
        path = Path(row["markdown_path"])
        if sha256_file(path) != row.get("markdown_sha256"):
            raise ValueError("Markdown hash mismatch; rerender required")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("Full-body source is empty")
    path, digest = freeze_source(text.encode("utf-8"))
    return text, path, digest


def freeze_source(raw: bytes, profile: RenderProfile | None = None) -> tuple[Path, str]:
    digest = hashlib.sha256(raw).hexdigest()
    directory = config.EXTRACTIONS_DIR / "sources"
    if profile is not None:
        directory = directory / profile
    destination = directory / f"{digest}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != digest:
            raise ValueError("Immutable source snapshot has changed")
    else:
        _write_atomic(destination, raw)
    return destination, digest


def prepare_catalog_source(
    row: dict[str, Any], profile: RenderProfile, *, renderer_version: str = RENDERER_VERSION,
) -> tuple[str, Path, str]:
    metadata = row.get("markdown_metadata") or {}
    if not row.get("markdown_path") or metadata.get("profile") != profile or metadata.get("producer_version") != renderer_version:
        raise ValueError(f"No detected {profile} Markdown is available")
    raw = Path(row["markdown_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row.get("markdown_sha256"):
        raise ValueError(f"The {profile} Markdown hash changed after mention detection")
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError(f"The {profile} Markdown is empty")
    path, digest = freeze_source(raw, profile)
    return text, path, digest


def run_whole_document(
    repo: Any, client: GatewayClient, extraction_id: int, snapshot: dict[str, Any],
) -> PublicationExtraction:
    work_key = WHOLE_DOCUMENT_PROMPT_VERSION

    def record_attempt(payload: dict[str, Any], attempt: dict[str, Any]) -> None:
        repo.begin_extraction_request(extraction_id, work_key, payload)
        repo.record_extraction_attempt(extraction_id, work_key, attempt)

    checkpoint = repo.get_extraction_request(extraction_id, work_key)
    if checkpoint is not None and checkpoint["status"] == "successful":
        cached, report = True, checkpoint["result"]
    else:
        cached, report = False, extract_whole_document(client, snapshot, record_attempt=record_attempt)
    metrics = report["metrics"]
    error = None
    if report["status"] != "complete":
        reasons = report["reasons"]
        if reasons == ["over_budget"]:
            error = "Whole-document extraction incomplete. The full text exceeds the request budget."
        else:
            error = "Whole-document extraction incomplete (" + ", ".join(reasons).replace("_", " ") + ")."
    if not cached:
        repo.record_extraction_progress(extraction_id, {
            **metrics, "phase": "whole_document", "attempts_this_invocation": metrics["http_requests"],
            "elapsed_seconds": metrics["extraction_seconds"], "call_limit": 1,
            "time_limit_seconds": config.LLM_TIMEOUT_SEC,
            "calls_by_phase": {"whole_document": metrics["http_requests"]},
        })
        if metrics["http_requests"]:
            repo.finish_extraction_request(extraction_id, work_key, report, error)
    if error:
        if "gateway_unavailable" in report["reasons"]:
            raise InferenceUnavailable(error)
        raise ValueError(error)
    return publication_result(report)


def extract_publication(repo: Any, client: GatewayClient, run_id: int, row: dict[str, Any]) -> dict[str, Any]:
    previous = repo.latest_publication_extraction(run_id, row["publication_id"])
    run_config = (repo.get_pipeline_run(run_id) or {}).get("config", {})
    settings = run_config.get("extraction") or (previous or {}).get("config") or extraction_configuration(
        render_profile=run_config.get("render_profile", DEFAULT_RENDER_PROFILE),
    )
    catalog = settings.get("prompt_version") in CATALOG_PROMPT_VERSIONS
    renderer_version = settings.get("renderer_version", RENDERER_VERSION)
    try:
        if previous and previous.get("source_path"):
            if previous["config"] != settings:
                raise ValueError("Extraction configuration changed; start a new run")
            path = Path(previous["source_path"])
            digest = previous["source_sha256"]
            if catalog:
                if digest != row.get("markdown_sha256"):
                    raise ValueError("The detected Markdown changed since extraction started; start a new run")
                text = read_frozen_source(str(path), digest)
            else:
                if sha256_file(path) != digest:
                    raise ValueError("Immutable source snapshot has changed")
                text = path.read_text(encoding="utf-8")
        elif catalog:
            text, path, digest = prepare_catalog_source(row, settings["render_profile"], renderer_version=renderer_version)
        else:
            text, path, digest = row.get('_prepared_source') or prepare_source(row, renderer_version=renderer_version)
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
    if settings not in supported_extraction_configurations():
        repo.fail_publication_extraction(extraction_id, "failed", "Extraction configuration changed; start a new run")
        return {"paper_id": row["paper_id"], "status": "failed", "error": "Extraction configuration changed; start a new run"}
    repo.cursor.execute("""
        INSERT INTO artifacts(publication_id,artifact_type,path,sha256,bytes,metadata)
        VALUES (%s,'extraction_source',%s,%s,%s,%s)
        ON CONFLICT(publication_id,artifact_type,path) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id
    """, (row["publication_id"], str(path), digest, path.stat().st_size, psycopg2.extras.Json({
        "profile": settings["render_profile"], "producer_version": renderer_version,
    })))
    artifact_id = repo.cursor.fetchone()["id"]
    repo.attach_extraction_source(extraction_id, str(path), digest, artifact_id)
    try:
        if catalog:
            result = run_catalog_extraction(
                repo, client, extraction_id, text, row["paper_id"], digest, prompt_version=settings["prompt_version"],
            )
        elif settings["prompt_version"] == WHOLE_DOCUMENT_PROMPT_VERSION:
            result = run_whole_document(repo, client, extraction_id, {
                "paper_id": row["paper_id"], "source_path": str(path), "source_sha256": digest,
            })
        else:
            budget = BoundedExtractionEngine(repo, client, extraction_id, text)
            try:
                if settings['prompt_version'] == PROMPT_VERSION:
                    result = ExtractionEngine(repo, client, extraction_id, text, budget).run(row['paper_id'], digest)
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
        repo.publish_extraction(extraction_id, result)
        return {
            "paper_id": row["paper_id"],
            "status": "successful",
            "datasets": len(result.datasets),
            "extraction_id": extraction_id,
        }
    except InferenceUnavailable as exc:
        repo.fail_publication_extraction(extraction_id, "interrupted", str(exc))
        repo.interrupt_extraction_run(run_id, str(exc))
        return {"paper_id": row["paper_id"], "status": "interrupted", "error": str(exc)}
    except Exception as exc:
        repo.fail_publication_extraction(extraction_id, "failed", str(exc))
        return {"paper_id": row["paper_id"], "status": "failed", "error": str(exc)}


def require_compatible_run(repo: PipelineRepository, run_id: int) -> dict[str, Any]:
    run = repo.get_pipeline_run(run_id)
    if not run:
        raise LookupError("Pipeline run not found")
    frozen = run.get("config", {}).get("extraction")
    if frozen and frozen not in supported_extraction_configurations():
        raise ValueError("Run extraction configuration differs from this worker. Start a new run.")
    return run


def extract_run(run_id: int | None, limit: int | None = None, publication_id: int | None = None) -> dict[str, Any]:
    if run_id is None:
        raise ValueError("Whole-publication extraction requires an existing pipeline run ID")
    client = GatewayClient()
    try:
        with PipelineRepository() as repo:
            if not repo.extraction_lock(run_id, publication_id):
                raise ValueError("This run already has active extraction")
            run = require_compatible_run(repo, run_id)
            if run["status"] == "interrupted":
                return {"stage": "extract_features", "status": "interrupted", "count": 0, "message": "Inference interrupted; resume explicitly.", "payload": {}}
            repo.record_stage_start("extract_features", run_id)
            results: list[dict[str, Any]] = []
            frozen = (run.get("config") or {}).get("extraction") or {}
            if frozen.get("prompt_version") in CATALOG_PROMPT_VERSIONS:
                prepared = repo.extraction_publications(
                    run_id, limit, publication_id, render_profile=frozen["render_profile"],
                    renderer_version=frozen["renderer_version"],
                )
            else:
                prepared = []
                for row in repo.extraction_publications(run_id, limit, publication_id):
                    previous = repo.latest_publication_extraction(run_id, row['publication_id'])
                    if not previous or not previous.get('source_path'):
                        try:
                            row['_prepared_source'] = prepare_source(
                                row, renderer_version=frozen.get("renderer_version", RENDERER_VERSION),
                            )
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
                      "message": f"Extracted {successful} publications; {failed} failed; {sum(item['status'] == 'interrupted' for item in results)} interrupted."
                      if results else "No selected publications are eligible for extraction after screening.",
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
        require_compatible_run(repo, run_id)
        repo.cursor.execute("""SELECT DISTINCT config FROM publication_extractions e WHERE pipeline_run_id=%s
            AND id=(SELECT max(id) FROM publication_extractions latest WHERE latest.pipeline_run_id=e.pipeline_run_id AND latest.publication_id=e.publication_id)""", (run_id,))
        supported = supported_extraction_configurations()
        if any(row["config"] not in supported for row in repo.cursor.fetchall()):
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
