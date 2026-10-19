"""Focused persistence methods."""

from __future__ import annotations

from typing import Any


class InsightRepositoryMixin:
    cursor: Any

    def export_run_result_rows(self, pipeline_run_id: int) -> list[dict[str, Any]]:
        """Return every selected candidate, with zero or more canonical insight rows."""
        self.cursor.execute(
            """
            SELECT
                p.paper_id,
                p.title AS publication_title,
                p.year AS publication_year,
                COALESCE(pr.config->>'discovery_mode', 'unrecorded') AS discovery_mode,
                COALESCE(dc.evidence_reasons, ARRAY[]::TEXT[]) AS discovery_methods,
                COALESCE(
                    item_progress.pdf_downloaded,
                    EXISTS (
                        SELECT 1 FROM artifacts artifact
                        WHERE artifact.publication_id = p.id AND artifact.artifact_type = 'pdf'
                    )
                ) AS pdf_downloaded,
                COALESCE(
                    item_progress.grobid_converted,
                    EXISTS (
                        SELECT 1 FROM artifacts artifact
                        WHERE artifact.publication_id = p.id AND artifact.artifact_type = 'tei_xml'
                    )
                ) AS grobid_converted,
                COALESCE(
                    item_progress.document_rendered,
                    EXISTS (
                        SELECT 1 FROM artifacts artifact
                        WHERE artifact.publication_id = p.id AND artifact.artifact_type = 'markdown'
                    )
                ) AS document_rendered,
                dm.dataset_name,
                dm.dataset_role,
                dm.reference_directness,
                dm.confidence,
                dm.evidence,
                dm.metadata,
                md.status AS match_status,
                md.match_method,
                md.match_score,
                md.matched_fields,
                md.candidate_um_dataset_ids,
                md.review_required,
                ud.um_dataset_id,
                ud.title AS um_dataset_title,
                ud.repository AS um_repository
            FROM discovery_candidates dc
            JOIN publications p ON p.id = dc.publication_id
            JOIN pipeline_runs pr ON pr.id = dc.pipeline_run_id
            LEFT JOIN LATERAL (
                SELECT
                    BOOL_OR(pis.status = 'successful') FILTER (
                        WHERE pis.stage = 'download_pdf'
                    ) AS pdf_downloaded,
                    BOOL_OR(pis.status = 'successful') FILTER (
                        WHERE pis.stage = 'grobid_convert'
                    ) AS grobid_converted,
                    BOOL_OR(pis.status = 'successful') FILTER (
                        WHERE pis.stage = 'render_document'
                    ) AS document_rendered
                FROM pipeline_items pi
                JOIN pipeline_item_stages pis ON pis.pipeline_item_id = pi.id
                WHERE pi.pipeline_run_id = dc.pipeline_run_id
                  AND pi.publication_id = p.id
            ) item_progress ON TRUE
            LEFT JOIN dataset_mentions dm ON dm.publication_id = p.id
            LEFT JOIN um_match_decisions md ON md.dataset_mention_id = dm.id
            LEFT JOIN um_datasets ud ON ud.id = md.um_dataset_row_id
            WHERE dc.pipeline_run_id = %s
              AND dc.included
              AND dc.pipeline_ready
            ORDER BY p.paper_id, dm.dataset_name NULLS FIRST
            """,
            (pipeline_run_id,),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def export_insight_rows(self, pipeline_run_id: int | None = None) -> list[dict[str, Any]]:
        if pipeline_run_id is not None:
            self.cursor.execute(
                """
                SELECT
                    p.paper_id,
                    p.title AS publication_title,
                    p.year AS publication_year,
                    COALESCE(pr.config->>'discovery_mode', 'unrecorded') AS discovery_mode,
                    COALESCE(dc.evidence_reasons, ARRAY[]::TEXT[]) AS discovery_methods,
                    dm.dataset_name,
                    dm.dataset_role,
                    dm.reference_directness,
                    dm.confidence,
                    dm.evidence,
                    dm.metadata,
                    md.status AS match_status,
                    md.match_method,
                    md.match_score,
                    md.matched_fields,
                    md.candidate_um_dataset_ids,
                    md.review_required,
                    ud.um_dataset_id,
                    ud.title AS um_dataset_title,
                    ud.repository AS um_repository
                FROM dataset_mentions dm
                JOIN publications p ON p.id = dm.publication_id
                JOIN discovery_candidates dc
                  ON dc.publication_id = p.id
                 AND dc.pipeline_run_id = %s
                 AND dc.included
                 AND dc.pipeline_ready
                JOIN pipeline_runs pr ON pr.id = dc.pipeline_run_id
                LEFT JOIN um_match_decisions md ON md.dataset_mention_id = dm.id
                LEFT JOIN um_datasets ud ON ud.id = md.um_dataset_row_id
                ORDER BY p.paper_id, dm.dataset_name
                """,
                (pipeline_run_id,),
            )
            return [dict(row) for row in self.cursor.fetchall()]

        self.cursor.execute(
            """
            SELECT
                p.paper_id,
                p.title AS publication_title,
                p.year AS publication_year,
                COALESCE(discovery.discovery_mode, 'unrecorded') AS discovery_mode,
                COALESCE(discovery.discovery_methods, ARRAY[]::TEXT[]) AS discovery_methods,
                dm.dataset_name,
                dm.dataset_role,
                dm.reference_directness,
                dm.confidence,
                dm.evidence,
                dm.metadata,
                md.status AS match_status,
                md.match_method,
                md.match_score,
                md.matched_fields,
                md.candidate_um_dataset_ids,
                md.review_required,
                ud.um_dataset_id,
                ud.title AS um_dataset_title,
                ud.repository AS um_repository
            FROM dataset_mentions dm
            JOIN publications p ON p.id = dm.publication_id
            LEFT JOIN LATERAL (
                SELECT
                    COALESCE(pr.config->>'discovery_mode', 'unrecorded') AS discovery_mode,
                    dc.evidence_reasons AS discovery_methods
                FROM discovery_candidates dc
                JOIN pipeline_runs pr ON pr.id = dc.pipeline_run_id
                WHERE dc.publication_id = p.id
                  AND dc.included
                  AND dc.pipeline_ready
                ORDER BY
                    CASE
                        WHEN %s::BIGINT IS NOT NULL AND dc.pipeline_run_id = %s THEN 0
                        ELSE 1
                    END,
                    dc.updated_at DESC,
                    dc.id DESC
                LIMIT 1
            ) discovery ON TRUE
            LEFT JOIN um_match_decisions md ON md.dataset_mention_id = dm.id
            LEFT JOIN um_datasets ud ON ud.id = md.um_dataset_row_id
            ORDER BY p.paper_id, dm.dataset_name
            """,
            (None, None),
        )
        return [dict(row) for row in self.cursor.fetchall()]
