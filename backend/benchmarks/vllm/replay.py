"""Replay an existing cohort's immutable sources with bounded extraction or Stage A."""

import argparse
import json
import hashlib
import time
from datetime import datetime, UTC
from uuid import uuid4

from psycopg2 import sql

from datasight.application.llm_extraction import extraction_configuration, extract_run, fingerprint
from datasight.application.pipeline_services import export_evaluation_bundle
from datasight.application.whole_document_extraction import extract_whole_document, whole_document_settings
from datasight.infrastructure.ingestion.renderer import _write_atomic
from datasight.infrastructure.llm.client import GatewayClient
from datasight.infrastructure.persistence.repository import PipelineRepository


def replay(source_run_id, legacy=False, paper_id=None, code_revision=None):
    from datasight import config
    def code_hashes():
        paths = ['application/bounded_extraction.py', 'application/extraction_engine.py',
                 'application/llm_extraction.py', 'infrastructure/llm/client.py',
                 'infrastructure/llm/tokenizer.py', 'assets/tokenizer.json']
        return {path: hashlib.sha256((config.PROJECT_ROOT / 'backend/src/datasight' / path).read_bytes()).hexdigest() for path in paths}
    initial_code = code_hashes()
    settings = extraction_configuration(legacy=legacy)
    with PipelineRepository() as repo:
        source = repo.get_pipeline_run(source_run_id)
        if not source:
            raise ValueError('Source run does not exist')
        run_id = repo.create_pipeline_run('Frozen source extraction replay', {
            'extraction': settings, 'source_run_key': source['run_key'], 'strategy': 'standard'})
        repo.cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='discovery_candidates' AND column_name NOT IN ('id','pipeline_run_id') ORDER BY ordinal_position")
        columns = [row['column_name'] for row in repo.cursor.fetchall()]
        identifiers = sql.SQL(',').join(map(sql.Identifier, columns))
        repo.cursor.execute(sql.SQL('INSERT INTO discovery_candidates (pipeline_run_id,{cols}) SELECT %s,{cols} FROM discovery_candidates WHERE pipeline_run_id=%s AND included AND (%s::text IS NULL OR publication_id IN (SELECT id FROM publications WHERE paper_id=%s))').format(cols=identifiers), (run_id, source_run_id, paper_id, paper_id))
        repo.conn.commit()
        repo.cursor.execute('SELECT DISTINCT ON (publication_id) * FROM publication_extractions WHERE pipeline_run_id=%s AND source_path IS NOT NULL AND publication_id IN (SELECT publication_id FROM discovery_candidates WHERE pipeline_run_id=%s AND included) ORDER BY publication_id,id DESC', (source_run_id, run_id))
        snapshots = list(repo.cursor.fetchall())
        for row in snapshots:
            record = repo.begin_publication_extraction(run_id, row['publication_id'], fingerprint(row['source_sha256'], settings), settings)
            repo.attach_extraction_source(record['id'], row['source_path'], row['source_sha256'], row['source_artifact_id'])
        run_key = repo.get_pipeline_run(run_id)['run_key']
    started = datetime.now(UTC).isoformat()
    result = extract_run(run_id)
    finished = datetime.now(UTC).isoformat()
    with PipelineRepository() as repo:
        repo.finish_pipeline_run(run_id, result['status'])
    directory = f'storage/evaluation/{run_key}'
    export_evaluation_bundle(run_id, directory)
    metadata = {'run_id': run_id, 'run_key': run_key, 'source_run_key': source['run_key'],
                'code_revision': code_revision,
                'started_utc': started, 'finished_utc': finished, 'settings': settings, 'result': result,
                'code_files_sha256': initial_code, 'code_changed_during_run': initial_code != code_hashes(),
                'source_hashes': {row['publication_id']: row['source_sha256'] for row in snapshots}}
    (config.PROJECT_ROOT / directory / 'replay.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'run_id': run_id, 'run_key': run_key, 'status': result['status'], 'export_dir': directory}))


def replay_whole_document(source_run_id, paper_id=None, code_revision=None):
    from datasight import config

    with PipelineRepository() as repo:
        source = repo.get_pipeline_run(source_run_id)
        if not source:
            raise ValueError("Source run does not exist")
        eligible = {row["paper_id"] for row in repo.extraction_publications(source_run_id)}
        repo.cursor.execute("""
            SELECT p.paper_id, e.source_path, e.source_sha256
            FROM discovery_candidates dc
            JOIN publications p ON p.id = dc.publication_id
            LEFT JOIN LATERAL (
                SELECT source_path, source_sha256 FROM publication_extractions
                WHERE pipeline_run_id = dc.pipeline_run_id AND publication_id = p.id
                    AND source_path IS NOT NULL
                ORDER BY id DESC LIMIT 1
            ) e ON TRUE
            WHERE dc.pipeline_run_id = %s AND dc.included AND dc.pipeline_ready
                AND (%s::text IS NULL OR p.paper_id = %s)
            ORDER BY p.id
        """, (source_run_id, paper_id, paper_id))
        snapshots = [row for row in repo.cursor.fetchall() if row["paper_id"] in eligible]
    if not snapshots:
        raise ValueError("Source run has no eligible papers for this selection")

    started_utc = datetime.now(UTC).isoformat()
    started = time.monotonic()
    client = GatewayClient()
    try:
        papers = [extract_whole_document(client, snapshot) for snapshot in snapshots]
    finally:
        client.close()
    extraction_seconds = round(time.monotonic() - started, 3)
    finished_utc = datetime.now(UTC).isoformat()

    totals = {}
    for name in papers[0]["metrics"]:
        if name != "extraction_seconds":
            values = [paper["metrics"][name] for paper in papers]
            totals[name] = None if None in values else sum(values)
    incomplete_papers = sum(paper["status"] == "incomplete" for paper in papers)
    replay_key = f"stage-a-{uuid4()}"
    directory = config.PROJECT_ROOT / "storage" / "evaluation" / replay_key
    report = {
        "strategy": "whole_document",
        "replay_key": replay_key,
        "source_run_id": source_run_id,
        "source_run_key": source["run_key"],
        "code_revision": code_revision or config.CODE_VERSION,
        "code_version": config.CODE_VERSION,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "settings": whole_document_settings(),
        "metrics": {
            **totals,
            "extraction_seconds": extraction_seconds,
            "complete_papers": len(papers) - incomplete_papers,
            "incomplete_papers": incomplete_papers,
        },
        "papers": papers,
    }
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "replay.json"
    _write_atomic(path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"replay_key": replay_key, "incomplete_papers": incomplete_papers, "report": str(path)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run-id', type=int, required=True)
    strategy = parser.add_mutually_exclusive_group()
    strategy.add_argument('--legacy', action='store_true')
    strategy.add_argument('--whole-document', action='store_true', help='run experimental Stage A and write a benchmark report only')
    parser.add_argument('--paper-id')
    parser.add_argument('--code-revision')
    args = parser.parse_args()
    if args.whole_document:
        replay_whole_document(args.source_run_id, args.paper_id, args.code_revision)
    else:
        replay(args.source_run_id, args.legacy, args.paper_id, args.code_revision)
