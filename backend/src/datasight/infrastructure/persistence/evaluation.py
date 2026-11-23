"""Strictly run-scoped rows for reproducible annotation exports."""

from __future__ import annotations

from typing import Any

from datasight.domain.candidate_detection import DETECTOR_VERSION, SCREENING_VERSION


class EvaluationRepositoryMixin:
    cursor: Any

    def evaluation_paper_rows(self, pipeline_run_id: int) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                pr.id AS run_id,
                pr.run_key,
                pr.created_at AS run_created_at,
                pr.config AS run_config,
                p.id AS publication_row_id,
                p.paper_id,
                p.title,
                p.doi,
                p.year,
                p.language,
                p.publication_type,
                p.source_url,
                p.open_access_url,
                dc.candidate_strength AS discovery_score,
                dc.evidence_tier AS discovery_evidence_tier,
                dc.evidence_reasons,
                dc.matched_um_dataset_ids,
                dc.pipeline_ready AS pdf_url_available,
                dc.download_status,
                dc.download_failure_category,
                dc.download_checked_at,
                item.status AS pipeline_item_status,
                item.stage_statuses,
                pdf.path AS pdf_path,
                pdf.sha256 AS pdf_sha256,
                pdf.metadata AS pdf_metadata,
                xml.path AS tei_path,
                xml.sha256 AS tei_sha256,
                xml.metadata AS tei_metadata,
                md.path AS markdown_path,
                md.sha256 AS markdown_sha256,
                md.metadata AS markdown_metadata,
                detection.status AS detection_status,
                detection.detector_version,
                detection.candidate_count,
                detection.metrics AS detection_metrics,
                detection.error AS detection_error
            FROM discovery_candidates dc
            JOIN pipeline_runs pr ON pr.id = dc.pipeline_run_id
            JOIN publications p ON p.id = dc.publication_id
            LEFT JOIN LATERAL (
                SELECT
                    pi.status,
                    jsonb_object_agg(pis.stage, pis.status ORDER BY pis.stage) AS stage_statuses
                FROM pipeline_items pi
                LEFT JOIN pipeline_item_stages pis ON pis.pipeline_item_id = pi.id
                WHERE pi.pipeline_run_id = dc.pipeline_run_id
                  AND pi.publication_id = dc.publication_id
                GROUP BY pi.id
                LIMIT 1
            ) item ON TRUE
            LEFT JOIN LATERAL (
                SELECT path, sha256, metadata FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'pdf'
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) pdf ON TRUE
            LEFT JOIN LATERAL (
                SELECT path, sha256, metadata FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'tei_xml'
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) xml ON TRUE
            LEFT JOIN LATERAL (
                SELECT id, path, sha256, metadata FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'markdown'
                  AND (pr.config->>'render_profile' IS NULL OR metadata->>'profile' = pr.config->>'render_profile')
                  AND (pr.config->>'renderer_version' IS NULL OR metadata->>'producer_version' = pr.config->>'renderer_version')
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) md ON TRUE
            LEFT JOIN LATERAL (
                SELECT status, detector_version, candidate_count, metrics, error
                FROM mention_detection_runs
                WHERE publication_id = p.id
                  AND markdown_artifact_id = md.id
                  AND render_sha256 = md.sha256
                  AND detector_version IN (COALESCE(pr.config->>'detector_version', %s), %s)
                ORDER BY (detector_version = COALESCE(pr.config->>'detector_version', %s)) DESC,
                         created_at DESC, id DESC LIMIT 1
            ) detection ON TRUE
            WHERE dc.pipeline_run_id = %s
              AND dc.included
            ORDER BY dc.id
            """,
            (DETECTOR_VERSION, SCREENING_VERSION, DETECTOR_VERSION, pipeline_run_id),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def evaluation_candidate_rows(self, pipeline_run_id: int) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                dc.pipeline_run_id AS run_id,
                p.paper_id,
                p.title,
                p.doi,
                dc.candidate_strength AS discovery_score,
                dc.evidence_tier AS discovery_evidence_tier,
                md.id AS markdown_artifact_id,
                md.path AS markdown_path,
                md.sha256 AS render_sha256,
                md.metadata->>'producer_version' AS renderer_version,
                dr.id AS detection_run_id,
                dr.detector_version,
                dr.completed_at AS detected_at,
                c.id AS candidate_id,
                c.dataset_name,
                c.evidence_tier,
                c.trigger_type,
                c.trigger_text,
                c.triggers,
                c.evidence_text,
                c.section_heading,
                c.standardized_section,
                c.char_start,
                c.char_end,
                c.score AS legacy_tier_score,
                c.source
            FROM discovery_candidates dc
            JOIN pipeline_runs pr ON pr.id = dc.pipeline_run_id
            JOIN publications p ON p.id = dc.publication_id
            JOIN LATERAL (
                SELECT id, path, sha256, metadata FROM artifacts
                WHERE publication_id = p.id AND artifact_type = 'markdown'
                  AND (pr.config->>'render_profile' IS NULL OR metadata->>'profile' = pr.config->>'render_profile')
                  AND (pr.config->>'renderer_version' IS NULL OR metadata->>'producer_version' = pr.config->>'renderer_version')
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) md ON TRUE
            JOIN mention_detection_runs dr
              ON dr.publication_id = p.id
             AND dr.markdown_artifact_id = md.id
             AND dr.render_sha256 = md.sha256
             AND dr.detector_version = COALESCE(pr.config->>'detector_version', %s)
             AND dr.status = 'successful'
            JOIN mention_candidates c ON c.detection_run_id = dr.id
            WHERE dc.pipeline_run_id = %s
              AND dc.included
            ORDER BY dc.id, c.char_start, c.id
            """,
            (DETECTOR_VERSION, pipeline_run_id),
        )
        return [dict(row) for row in self.cursor.fetchall()]
