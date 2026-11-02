"""Rebuild a completed result from read-only checkpoints, without inference."""

import argparse
import json
from pathlib import Path

from datasight.application.bounded_extraction import BoundedExtractionEngine
from datasight.application.llm_extraction import extraction_configuration
from datasight.infrastructure.ingestion.file_integrity import sha256_file
from datasight.infrastructure.persistence.repository import PipelineRepository


def audit(run_id, run_key):
    with PipelineRepository() as repo:
        if (repo.get_pipeline_run(run_id) or {}).get('run_key') != run_key:
            raise ValueError('Run identity mismatch')
        repo.cursor.execute('SELECT e.*,p.paper_id FROM publication_extractions e JOIN publications p ON p.id=e.publication_id WHERE e.pipeline_run_id=%s', (run_id,))
        rows = list(repo.cursor.fetchall())
        if not rows:
            raise ValueError('Run has no extraction records to audit')
        output = []
        for row in rows:
            if row['status'] != 'successful' or row['config'] != extraction_configuration(legacy=False):
                raise ValueError('Audit requires successful results with this exact extraction configuration')
            path = Path(row['source_path'])
            assert sha256_file(path) == row['source_sha256'], 'Source changed'

            class ReadOnly:
                def get_extraction_request(self, extraction_id, key):
                    return repo.get_extraction_request(extraction_id, key)
                def record_extraction_progress(self, *args):
                    pass
                def begin_extraction_request(self, *args):
                    raise ValueError('Checkpoint miss: audit cannot generate new work')

            class NoInference:
                def complete(self, *args):
                    raise AssertionError('Audit must never call inference')

            result = BoundedExtractionEngine(ReadOnly(), NoInference(), row['id'], path.read_text(encoding='utf-8')).run(row['paper_id'], row['source_sha256'])
            def local_values(data):
                return {(d['dataset_local_id'], tuple(o['evidence_ids']), group, field, json.dumps(value, sort_keys=True))
                    for d in data['datasets'] for o in d['occurrences'] for group in ('use', 'citation')
                    for field, observed in o[group].items() for value in observed['values']}
            before, after = local_values(row['result']), local_values(result.model_dump(mode='json'))
            assert before <= after, 'Previously published local observations were lost'
            output.append({'run_key': run_key, 'paper_id': row['paper_id'], 'new_http_attempts': 0,
                'datasets': len(result.datasets), 'published_occurrences': sum(len(d.occurrences) for d in result.datasets),
                'previous_local_values': len(before), 'retained_local_values': len(after),
                'restored_local_values': len(after-before), 'lost_local_values': len(before-after)})
        return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--run-key', required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.run_id, args.run_key)))
