"""Prepare a frozen regression cohort from retained PDFs, without GPU calls."""

import argparse
import json
from pathlib import Path

from datasight.application.llm_extraction import extraction_configuration, fingerprint, prepare_source
from datasight.infrastructure.ingestion.converter import GrobidConverter
from datasight.infrastructure.persistence.repository import PipelineRepository


def prepare(pdf_paths):
    converter = GrobidConverter()
    converter.ensure_available()
    sources = []
    for pdf in pdf_paths:
        converted = converter.convert_pdf(pdf, paper_id=pdf.stem)
        if not converted.success:
            raise ValueError(f'Conversion failed for {pdf.stem}: {converted.failure_category}')
        text, path, digest = prepare_source({'xml_path': str(converted.xml_path)})
        sources.append((pdf.stem, path, digest, len(text)))
    settings = extraction_configuration(legacy=True)
    with PipelineRepository() as repo:
        run_id = repo.create_pipeline_run('Frozen PDF regression sources', {'extraction': settings})
        for paper_id, path, digest, length in sources:
            repo.cursor.execute('SELECT id FROM publications WHERE paper_id=%s', (paper_id,))
            existing = repo.cursor.fetchone()
            if not existing:
                repo.upsert_publications([{'paperId': paper_id, 'title': paper_id}], 'benchmark')
                repo.cursor.execute('SELECT id FROM publications WHERE paper_id=%s', (paper_id,))
                existing = repo.cursor.fetchone()
            publication_id = existing['id']
            repo.cursor.execute("""INSERT INTO discovery_candidates(pipeline_run_id,publication_id,candidate_strength,
                evidence_tier,evidence_reasons,pipeline_ready,included) VALUES (%s,%s,90,'direct',ARRAY['regression'],true,true)""", (run_id, publication_id))
            repo.conn.commit()
            row = repo.begin_publication_extraction(run_id, publication_id, fingerprint(digest, settings), settings)
            repo.attach_extraction_source(row['id'], str(path), digest, None)
            repo.fail_publication_extraction(row['id'], 'interrupted', 'Prepared regression source; inference not started')
        repo.finish_pipeline_run(run_id, 'interrupted')
        print(json.dumps({'run_id': run_id, 'run_key': repo.get_pipeline_run(run_id)['run_key'],
                          'sources': [{'paper_id': paper, 'sha256': digest, 'characters': length} for paper, _, digest, length in sources]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, action='append', required=True)
    args = parser.parse_args()
    prepare(args.pdf)
