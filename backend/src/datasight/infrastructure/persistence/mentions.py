"""Focused persistence methods."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg2.extras

from datasight.domain.schemas import DatasetMention, MentionCandidate
from datasight.domain.candidate_detection import DETECTOR_VERSION
from datasight.infrastructure.persistence.discovery import scope_to_preview_candidates


class MentionRepositoryMixin:
    conn: Any
    cursor: Any

    def get_markdown_artifacts_needing_detection(
        self,
        detector_version: str = DETECTOR_VERSION,
        limit: int | None = None,
        pipeline_run_id: int | None = None,
        *,
        render_profile: str | None = None,
        renderer_version: str | None = None,
        include_cached: bool = False,
    ) -> list[dict[str, Any]]:
        profile_filter = "AND a.metadata->>'profile' = %s" if render_profile else ""
        version_filter = "AND a.metadata->>'producer_version' = %s" if renderer_version else ""
        pending_filter = "" if include_cached else """
            AND NOT EXISTS (
                SELECT 1
                FROM mention_detection_runs dr
                WHERE dr.publication_id = p.id
                  AND dr.markdown_artifact_id = md.id
                  AND dr.render_sha256 = md.sha256
                  AND dr.detector_version = %s
                  AND dr.status = 'successful'
            )
        """
        query = """
            SELECT
                p.id AS publication_row_id,
                p.paper_id,
                md.id AS markdown_artifact_id,
                md.path,
                md.sha256 AS render_sha256,
                md.metadata AS markdown_metadata,
                xml.path AS xml_path
            FROM publications p
            JOIN LATERAL (
                SELECT a.* FROM artifacts a
                WHERE a.publication_id = p.id AND a.artifact_type = 'markdown'
                  {profile_filter}
                  {version_filter}
                ORDER BY a.created_at DESC, a.id DESC LIMIT 1
            ) md ON TRUE
            LEFT JOIN LATERAL (
                SELECT a.path FROM artifacts a
                WHERE a.publication_id = p.id AND a.artifact_type = 'tei_xml'
                  AND a.sha256 = md.metadata->>'source_sha256'
                ORDER BY a.created_at DESC, a.id DESC LIMIT 1
            ) xml ON TRUE
            WHERE md.sha256 IS NOT NULL
              {pending_filter}
        """.format(profile_filter=profile_filter, version_filter=version_filter, pending_filter=pending_filter)
        params: list[Any] = [value for value in (render_profile, renderer_version) if value]
        if not include_cached:
            params.append(detector_version)
        query, params = scope_to_preview_candidates(query, params, pipeline_run_id)
        query += " ORDER BY p.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def get_markdown_artifacts_without_candidates(
        self, limit: int | None = None, pipeline_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Compatibility alias now backed by versioned completion records."""
        return self.get_markdown_artifacts_needing_detection(
            limit=limit, pipeline_run_id=pipeline_run_id
        )

    def begin_detection_run(
        self,
        publication_row_id: int,
        markdown_artifact_id: int,
        render_sha256: str,
        detector_version: str = DETECTOR_VERSION,
    ) -> int:
        self.cursor.execute(
            """
            INSERT INTO mention_detection_runs (
                publication_id, markdown_artifact_id, render_sha256, detector_version,
                status, candidate_count, metrics, error, completed_at
            )
            VALUES (%s, %s, %s, %s, 'running', 0, '{}'::jsonb, NULL, NULL)
            ON CONFLICT (publication_id, markdown_artifact_id, render_sha256, detector_version)
            DO UPDATE SET
                status = 'running', candidate_count = 0, metrics = '{}'::jsonb,
                error = NULL, completed_at = NULL
            RETURNING id
            """,
            (publication_row_id, markdown_artifact_id, render_sha256, detector_version),
        )
        row = self.cursor.fetchone()
        if not row:
            raise RuntimeError("Failed to create mention detection status record")
        detection_run_id = int(row["id"])
        self.cursor.execute(
            "DELETE FROM mention_candidates WHERE detection_run_id = %s",
            (detection_run_id,),
        )
        self.conn.commit()
        return detection_run_id

    def finish_detection_run(
        self,
        detection_run_id: int,
        candidate_count: int,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        self.cursor.execute(
            """
            UPDATE mention_detection_runs
            SET status = 'successful', candidate_count = %s, metrics = %s,
                error = NULL, completed_at = now()
            WHERE id = %s
            """,
            (candidate_count, psycopg2.extras.Json(metrics or {}), detection_run_id),
        )
        self.conn.commit()

    def fail_detection_run(self, detection_run_id: int, error: str) -> None:
        # A failed candidate insert leaves PostgreSQL's transaction aborted.
        # Clear it before persisting the detection-run failure itself.
        self.conn.rollback()
        self.cursor.execute(
            """
            UPDATE mention_detection_runs
            SET status = 'failed', error = %s, completed_at = now()
            WHERE id = %s
            """,
            (error, detection_run_id),
        )
        self.conn.commit()

    def get_mention_screening(
        self, publication_id: int, artifact_id: int, render_sha256: str, version: str,
    ) -> dict[str, Any] | None:
        self.cursor.execute(
            """
            SELECT metrics FROM mention_detection_runs
            WHERE publication_id = %s AND markdown_artifact_id = %s AND render_sha256 = %s
              AND detector_version = %s AND status = 'successful'
            """,
            (publication_id, artifact_id, render_sha256, version),
        )
        row = self.cursor.fetchone()
        return dict(row["metrics"]) if row else None

    def record_mention_screening(
        self, publication_id: int, artifact_id: int, render_sha256: str, version: str,
        metrics: dict[str, Any], error: str | None = None,
    ) -> dict[str, Any]:
        self.cursor.execute(
            """
            INSERT INTO mention_detection_runs AS dr (
                publication_id, markdown_artifact_id, render_sha256, detector_version,
                status, candidate_count, metrics, error, completed_at
            )
            VALUES (%s, %s, %s, %s, %s, 0, %s, %s, now())
            ON CONFLICT (publication_id, markdown_artifact_id, render_sha256, detector_version)
            DO UPDATE SET status = EXCLUDED.status, metrics = EXCLUDED.metrics,
                          error = EXCLUDED.error, completed_at = EXCLUDED.completed_at
            WHERE dr.status <> 'successful'
            RETURNING metrics
            """,
            (publication_id, artifact_id, render_sha256, version,
             "failed" if error is not None else "successful", psycopg2.extras.Json(metrics), error),
        )
        row = self.cursor.fetchone()
        self.conn.commit()
        if row:
            return dict(row["metrics"])
        return self.get_mention_screening(publication_id, artifact_id, render_sha256, version) or metrics

    def upsert_mention_candidates(
        self,
        candidates: Iterable[MentionCandidate],
        detection_run_id: int | None = None,
    ) -> int:
        count = 0
        for candidate in candidates:
            self.cursor.execute(
                "SELECT id FROM publications WHERE paper_id = %s",
                (candidate.publication_id,),
            )
            row = self.cursor.fetchone()
            if not row:
                continue
            active_detection_run_id = detection_run_id or candidate.detection_run_id
            query = """
                INSERT INTO mention_candidates (
                    publication_id, dataset_name, evidence_text, section_heading,
                    standardized_section, char_start, char_end, score, source,
                    detection_run_id, trigger_type, trigger_text, triggers,
                    evidence_tier, detector_version, render_sha256, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """
            if active_detection_run_id is not None:
                query += """
                ON CONFLICT (detection_run_id, dataset_name, char_start, char_end)
                WHERE detection_run_id IS NOT NULL
                DO UPDATE SET
                    evidence_text = EXCLUDED.evidence_text,
                    section_heading = EXCLUDED.section_heading,
                    standardized_section = EXCLUDED.standardized_section,
                    score = EXCLUDED.score,
                    source = EXCLUDED.source,
                    trigger_type = EXCLUDED.trigger_type,
                    trigger_text = EXCLUDED.trigger_text,
                    triggers = EXCLUDED.triggers,
                    evidence_tier = EXCLUDED.evidence_tier,
                    detector_version = EXCLUDED.detector_version,
                    render_sha256 = EXCLUDED.render_sha256,
                    updated_at = now()
                """
            else:
                query += """
                ON CONFLICT (publication_id, dataset_name, char_start, char_end)
                WHERE detection_run_id IS NULL
                DO UPDATE SET evidence_text = EXCLUDED.evidence_text, updated_at = now()
                """
            self.cursor.execute(
                query,
                (
                    row["id"],
                    candidate.dataset_name,
                    candidate.evidence_text,
                    candidate.section_heading,
                    candidate.standardized_section,
                    candidate.char_start,
                    candidate.char_end,
                    candidate.score,
                    candidate.source,
                    active_detection_run_id,
                    candidate.trigger_type,
                    candidate.trigger_text,
                    psycopg2.extras.Json(candidate.triggers),
                    candidate.evidence_tier,
                    candidate.detector_version,
                    candidate.render_sha256,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def get_unprocessed_candidates(
        self, limit: int | None = None, pipeline_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                c.id AS candidate_id,
                p.paper_id AS publication_id,
                c.dataset_name,
                c.evidence_text,
                c.section_heading,
                c.standardized_section,
                c.char_start,
                c.char_end,
                c.score,
                c.source
            FROM mention_candidates c
            JOIN publications p ON p.id = c.publication_id
            JOIN mention_detection_runs dr ON dr.id = c.detection_run_id
            JOIN artifacts md
              ON md.id = dr.markdown_artifact_id
             AND md.publication_id = p.id
             AND md.artifact_type = 'markdown'
             AND md.sha256 = dr.render_sha256
            WHERE c.promoted_mention_id IS NULL
              AND dr.status = 'successful'
              AND dr.detector_version = %s
        """
        params: list[Any] = [DETECTOR_VERSION]
        query, params = scope_to_preview_candidates(query, params, pipeline_run_id)
        query += " ORDER BY c.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def get_unprocessed_candidates_for_publication(self, publication_row_id: int) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                c.id AS candidate_id,
                p.paper_id AS publication_id,
                c.dataset_name,
                c.evidence_text,
                c.section_heading,
                c.standardized_section,
                c.char_start,
                c.char_end,
                c.score,
                c.source
            FROM mention_candidates c
            JOIN publications p ON p.id = c.publication_id
            JOIN mention_detection_runs dr ON dr.id = c.detection_run_id
            JOIN artifacts md
              ON md.id = dr.markdown_artifact_id
             AND md.publication_id = p.id
             AND md.artifact_type = 'markdown'
             AND md.sha256 = dr.render_sha256
            WHERE c.promoted_mention_id IS NULL
              AND dr.status = 'successful'
              AND dr.detector_version = %s
              AND c.publication_id = %s
            ORDER BY c.id
            """,
            (DETECTOR_VERSION, publication_row_id),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def persist_dataset_mentions(self, mentions: Iterable[DatasetMention], candidate_ids: Iterable[int]) -> int:
        count = 0
        for mention, candidate_id in zip(mentions, candidate_ids, strict=False):
            self.cursor.execute("SELECT id FROM publications WHERE paper_id = %s", (mention.publication_id,))
            publication = self.cursor.fetchone()
            if not publication:
                continue
            publication_id = publication["id"]
            char_start = mention.provenance.char_start

            self.cursor.execute(
                """
                SELECT id
                FROM dataset_mentions
                WHERE publication_id = %s
                  AND dataset_name = %s
                  AND provenance->>'char_start' IS NOT DISTINCT FROM %s
                """,
                (
                    publication_id,
                    mention.dataset_name,
                    str(char_start) if char_start is not None else None,
                ),
            )
            existing = self.cursor.fetchone()
            if existing:
                mention_id = existing["id"]
                self.cursor.execute(
                    """
                    UPDATE dataset_mentions
                    SET aliases = %s,
                        dataset_role = %s,
                        reference_directness = %s,
                        evidence = %s,
                        metadata = %s,
                        provenance = %s,
                        confidence = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        mention.aliases,
                        mention.dataset_role,
                        mention.reference_directness,
                        psycopg2.extras.Json(mention.evidence.model_dump(mode="json")),
                        psycopg2.extras.Json(mention.metadata.model_dump(mode="json")),
                        psycopg2.extras.Json(mention.provenance.model_dump(mode="json")),
                        mention.provenance.confidence,
                        mention_id,
                    ),
                )
            else:
                self.cursor.execute(
                    """
                    INSERT INTO dataset_mentions (
                        publication_id, dataset_name, aliases, dataset_role,
                        reference_directness, evidence, metadata, provenance, confidence
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        publication_id,
                        mention.dataset_name,
                        mention.aliases,
                        mention.dataset_role,
                        mention.reference_directness,
                        psycopg2.extras.Json(mention.evidence.model_dump(mode="json")),
                        psycopg2.extras.Json(mention.metadata.model_dump(mode="json")),
                        psycopg2.extras.Json(mention.provenance.model_dump(mode="json")),
                        mention.provenance.confidence,
                    ),
                )
                inserted = self.cursor.fetchone()
                if not inserted:
                    raise RuntimeError("Failed to persist dataset mention")
                mention_id = inserted["id"]

            self.cursor.execute(
                "UPDATE mention_candidates SET promoted_mention_id = %s, updated_at = now() WHERE id = %s",
                (mention_id, candidate_id),
            )
            count += 1
        self.conn.commit()
        return count
