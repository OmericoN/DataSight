"""Validate a selected run's downloads without dispatching inference."""

import argparse
import json

from datasight.application.pipeline_services import download_pdf_batch, discover_publications
from datasight.infrastructure.persistence.repository import PipelineRepository

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--run-id', type=int)
    source.add_argument('--preview-id')
    parser.add_argument('--processing-limit', type=int, default=2)
    args = parser.parse_args()
    run_id = args.run_id
    if run_id is not None:
        with PipelineRepository() as repo:
            if not repo.get_pipeline_run(run_id):
                raise ValueError('Preflight run does not exist')
            repo.cursor.execute('SELECT count(*) AS count FROM discovery_candidates WHERE pipeline_run_id=%s AND included', (run_id,))
            if not repo.cursor.fetchone()['count']:
                raise ValueError('Preflight requires an explicitly selected cohort')
    if args.preview_id:
        with PipelineRepository() as repo:
            run_id = repo.create_pipeline_run('Download-only preflight', {
                'preview_id': args.preview_id, 'processing_limit': args.processing_limit})
        try:
            discover_publications(query='Download-only preflight', limit=args.processing_limit,
                                  pipeline_run_id=run_id, preview_id=args.preview_id, processing_limit=args.processing_limit)
        except Exception:
            with PipelineRepository() as repo:
                repo.finish_pipeline_run(run_id, 'failed')
            raise
    result = download_pdf_batch(pipeline_run_id=run_id)
    if args.preview_id:
        with PipelineRepository() as repo:
            repo.finish_pipeline_run(run_id, result['status'])
    print(json.dumps({'run_id': run_id, 'status': result['status'],
                      'papers': [{'paper_id': row['paper_id'], 'success': row['success'],
                                  'failure_category': row.get('failure_category'),
                                  'attempts': (row.get('quality_metrics') or {}).get('attempts'),
                                  'locations_attempted': (row.get('quality_metrics') or {}).get('locations_attempted')}
                                 for row in result.get('payload', {}).get('results', [])]}))
