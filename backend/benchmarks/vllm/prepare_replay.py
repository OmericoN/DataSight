"""Prepare a frozen regression cohort from retained PDFs, without GPU calls."""

import argparse
import json
import time
from pathlib import Path

from datasight.application.llm_extraction import extraction_configuration, fingerprint, prepare_source
from datasight.domain.results import RenderResult
from datasight.infrastructure.ingestion.converter import GrobidConverter
from datasight.infrastructure.ingestion.renderer import RENDERER_VERSION
from datasight.infrastructure.persistence.repository import PipelineRepository

PRESELECTED_VERSION = "benchmark-preselected-v1"


def prepare(pdf_paths):
    converter = GrobidConverter()
    converter.ensure_available()
    sources = []
    renders = []
    for pdf in pdf_paths:
        conversion_started = time.monotonic()
        converted = converter.convert_pdf(pdf, paper_id=pdf.stem)
        conversion_seconds = time.monotonic() - conversion_started
        if not converted.success:
            raise ValueError(f'Conversion failed for {pdf.stem}: {converted.failure_category}')
        render_started = time.monotonic()
        text, path, digest = prepare_source({'xml_path': str(converted.xml_path)})
        print(json.dumps({'paper_id': pdf.stem, 'conversion_seconds': round(conversion_seconds, 3),
                          'conversion_cache_reused': converted.quality_metrics.get('cache_reused', False),
                          'source_preparation_seconds': round(time.monotonic()-render_started, 3)}), flush=True)
        sources.append((pdf.stem, path, digest, len(text)))
        renders.append(RenderResult(
            paper_id=pdf.stem, xml_path=converted.xml_path, md_path=path, success=True,
            message="Frozen benchmark render", sha256=digest, producer_version=RENDERER_VERSION,
            profile="full_body",
        ))
    settings = extraction_configuration(legacy=True)
    with PipelineRepository() as repo:
        run_id = repo.create_pipeline_run('Frozen PDF regression sources', {
            'extraction': settings, 'detector_version': PRESELECTED_VERSION, 'render_profile': 'full_body',
        })
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
        repo.persist_render_results(renders)
        for artifact in repo.get_markdown_artifacts_needing_detection(
            detector_version=PRESELECTED_VERSION, pipeline_run_id=run_id, render_profile="full_body",
        ):
            repo.record_mention_screening(
                artifact["publication_row_id"], artifact["markdown_artifact_id"], artifact["render_sha256"],
                PRESELECTED_VERSION, {"decision": "likely_use", "decision_reason": "benchmark_preselected"},
            )
        repo.finish_pipeline_run(run_id, 'interrupted')
        print(json.dumps({'run_id': run_id, 'run_key': repo.get_pipeline_run(run_id)['run_key'],
                          'sources': [{'paper_id': paper, 'sha256': digest, 'characters': length} for paper, _, digest, length in sources]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, action='append', required=True)
    args = parser.parse_args()
    prepare(args.pdf)
