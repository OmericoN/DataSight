"""Replay an existing cohort's immutable sources in a new versioned run."""

import argparse
import json
import hashlib
from datetime import datetime, UTC

from psycopg2 import sql

from datasight.application.llm_extraction import extraction_configuration, extract_run, fingerprint
from datasight.application.pipeline_services import export_evaluation_bundle
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run-id', type=int, required=True)
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--paper-id')
    parser.add_argument('--code-revision')
    args = parser.parse_args()
    replay(args.source_run_id, args.legacy, args.paper_id, args.code_revision)
