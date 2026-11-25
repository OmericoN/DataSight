"""Persistence for discovery previews and run-scoped candidate evidence."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import psycopg2.extras

from datasight.domain.candidate_detection import SCREENING_VERSION

def scope_to_preview_candidates(
    query: str,
    params: list[Any],
    pipeline_run_id: int | None,
    *,
    artifact_type: str | None = None,
    artifact_sha_sql: str | None = None,
) -> tuple[str, list[Any]]:
    """Restrict preview-backed runs to the candidates selected for that run."""
    if pipeline_run_id is None:
        return query, params
    if artifact_type and artifact_sha_sql:
        query += f"""
            AND NOT EXISTS (
                SELECT 1 FROM discovery_candidates dc JOIN pipeline_runs pr ON pr.id=dc.pipeline_run_id
                WHERE pr.id=%s AND dc.publication_id=p.id AND pr.config->>'validated_pdf_target'='true'
                  AND dc.raw->>%s IS DISTINCT FROM {artifact_sha_sql}
            )
        """
        params = [*params, pipeline_run_id, artifact_type + "_sha256"]
    query += """
        AND (
            NOT EXISTS (
                SELECT 1 FROM pipeline_runs pr
                WHERE pr.id = %s
                  AND (NULLIF(pr.config->>'preview_id', '') IS NOT NULL OR EXISTS (
                      SELECT 1 FROM discovery_candidates selected WHERE selected.pipeline_run_id=pr.id
                  ) OR EXISTS (
                      SELECT 1 FROM stage_runs sr
                      WHERE sr.pipeline_run_id=pr.id AND sr.publication_id IS NULL
                        AND sr.stage='discover' AND sr.metrics->'payload' ? 'paper_ids'
                  ))
            )
            OR EXISTS (
                SELECT 1 FROM discovery_candidates dc
                WHERE dc.pipeline_run_id = %s
                  AND dc.publication_id = p.id
                  AND dc.included
                  AND dc.pipeline_ready
            )
            OR EXISTS (
                SELECT 1 FROM stage_runs sr
                JOIN pipeline_runs pr ON pr.id=sr.pipeline_run_id
                WHERE sr.pipeline_run_id=%s AND sr.publication_id IS NULL AND sr.stage='discover'
                  AND NULLIF(pr.config->>'preview_id', '') IS NULL
                  AND sr.metrics->'payload'->'paper_ids' ? p.paper_id
            )
        )
    """
    return query, [*params, pipeline_run_id, pipeline_run_id, pipeline_run_id]


class DiscoveryRepositoryMixin:
    conn: Any
    cursor: Any

    def lock_pool_selection(self, *, session: bool = False) -> None:
        # Materialization spans commits. Its session lock is released when the repository closes.
        lock = "pg_advisory_lock" if session else "pg_advisory_xact_lock"
        self.cursor.execute(f"SELECT {lock}(73015, 0)")

    def active_pool_claims(self, paper_ids: Iterable[str], run_id: int) -> set[str]:
        self.cursor.execute("""
            SELECT p.paper_id FROM discovery_candidates dc
            JOIN publications p ON p.id=dc.publication_id
            JOIN pipeline_runs pr ON pr.id=dc.pipeline_run_id
            WHERE p.paper_id=ANY(%s) AND dc.included AND pr.id<>%s
              AND pr.status IN ('queued','running','started','interrupted')
        """, (list(paper_ids), run_id))
        return {row["paper_id"] for row in self.cursor.fetchall()}

    def next_pool_download(self, run_id: int, target: int) -> dict[str, Any] | None:
        self.lock_pool_selection()
        self.cursor.execute("""
            UPDATE discovery_candidates SET included=false, exclusion_reason='PDF unavailable'
            WHERE pipeline_run_id=%s AND included AND download_status='failed'
        """, (run_id,))
        self.cursor.execute("""
            SELECT count(*) AS n FROM discovery_candidates
            WHERE pipeline_run_id=%s AND included AND download_status='downloaded'
        """, (run_id,))
        if self.cursor.fetchone()["n"] >= target:
            self.conn.commit()
            return None
        self.cursor.execute("""
            SELECT dc.id, p.paper_id AS "paperId"
            FROM discovery_candidates dc JOIN publications p ON p.id=dc.publication_id
            WHERE dc.pipeline_run_id=%s AND dc.pipeline_ready AND dc.download_status='not_attempted'
              AND (dc.included OR dc.exclusion_reason='Processing limit reached')
            ORDER BY dc.included DESC, dc.id
        """, (run_id,))
        rows = self.cursor.fetchall()
        ids = [r["paperId"] for r in rows]
        unavailable = self.processed_discovery_paper_ids(ids, run_id) | self.active_pool_claims(ids, run_id)
        if unavailable:
            self.cursor.execute("""
                UPDATE discovery_candidates dc SET included=false, exclusion_reason='Processed or claimed by another run'
                FROM publications p WHERE dc.pipeline_run_id=%s AND dc.publication_id=p.id AND p.paper_id=ANY(%s)
                  AND (dc.included OR dc.exclusion_reason='Processing limit reached') AND dc.download_status='not_attempted'
            """, (run_id, list(unavailable)))
        candidate = next((r for r in rows if r["paperId"] not in unavailable), None)
        selected = None
        if candidate:
            self.cursor.execute("""
                UPDATE discovery_candidates SET included=true,
                    raw=raw || jsonb_build_object('replacement', COALESCE(raw->'replacement',to_jsonb(NOT included))),
                    exclusion_reason=NULL WHERE id=%s
            """, (candidate["id"],))
            self.cursor.execute("""
                SELECT p.paper_id AS "paperId", dc.raw->>'title' AS title,
                       dc.raw->>'open_access_url' AS url, COALESCE(dc.raw->'pdf_locations',p.raw) AS raw
                FROM discovery_candidates dc JOIN publications p ON p.id=dc.publication_id
                WHERE dc.id=%s
            """, (candidate["id"],))
            selected = self.cursor.fetchone()
        self.conn.commit()
        return dict(selected) if selected else None

    def pool_download_counts(self, run_id: int) -> dict[str, int]:
        self.cursor.execute("""
            SELECT count(*) FILTER (WHERE download_status<>'not_attempted') AS attempted,
                   count(*) FILTER (WHERE included AND download_status='downloaded') AS validated,
                   count(*) FILTER (WHERE download_status='failed') AS failed,
                   count(*) FILTER (WHERE download_status<>'not_attempted' AND raw->>'replacement'='true') AS replacements
            FROM discovery_candidates WHERE pipeline_run_id=%s
        """, (run_id,))
        return dict(self.cursor.fetchone())

    def processed_discovery_paper_ids(
        self, paper_ids: Iterable[str], pipeline_run_id: int | None = None,
    ) -> set[str]:
        """Find completed extractions and terminal screening decisions for discovery hits."""
        ids = sorted(set(paper_ids) - {""})
        if not ids:
            return set()
        self.cursor.execute(
            """
            SELECT p.paper_id
            FROM publications p
            WHERE p.paper_id = ANY(%s)
              AND (
                  EXISTS (
                      SELECT 1 FROM publication_extractions e
                      WHERE e.publication_id=p.id AND e.status='successful'
                        AND e.pipeline_run_id IS DISTINCT FROM %s
                  )
                  OR (
                      SELECT dr.metrics->>'decision'
                      FROM mention_detection_runs dr
                      JOIN artifacts a ON a.id=dr.markdown_artifact_id AND a.sha256=dr.render_sha256
                      WHERE dr.publication_id=p.id AND dr.status='successful'
                        AND dr.detector_version=%s
                      ORDER BY dr.completed_at DESC NULLS LAST, dr.id DESC
                      LIMIT 1
                  ) = 'no_use'
              )
            """,
            (ids, pipeline_run_id, SCREENING_VERSION),
        )
        return {row["paper_id"] for row in self.cursor.fetchall()}

    def save_discovery_preview(
        self,
        preview_id: str,
        catalog_fingerprint: str,
        request: dict[str, Any],
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> None:
        self.cursor.execute(
            """
            INSERT INTO discovery_previews (
                preview_id, catalog_fingerprint, request, payload, expires_at
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (preview_id) DO UPDATE SET
                catalog_fingerprint = EXCLUDED.catalog_fingerprint,
                request = EXCLUDED.request,
                payload = EXCLUDED.payload,
                expires_at = EXCLUDED.expires_at
            """,
            (
                preview_id,
                catalog_fingerprint,
                psycopg2.extras.Json(request),
                psycopg2.extras.Json(payload),
                expires_at,
            ),
        )
        self.cursor.execute("DELETE FROM discovery_previews WHERE expires_at <= now()")
        self.conn.commit()

    def get_discovery_preview(self, preview_id: str) -> dict[str, Any] | None:
        self.cursor.execute(
            """
            SELECT preview_id, catalog_fingerprint, request, payload, expires_at, created_at
            FROM discovery_previews
            WHERE preview_id = %s AND expires_at > now()
            """,
            (preview_id,),
        )
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def list_topic_resolution_cache(self, catalog_fingerprint: str) -> dict[str, str | None]:
        self.cursor.execute(
            """
            SELECT topic_name, topic_id
            FROM discovery_topic_resolutions
            WHERE catalog_fingerprint = %s
            """,
            (catalog_fingerprint,),
        )
        return {
            str(row["topic_name"]): str(row["topic_id"]) if row.get("topic_id") else None
            for row in self.cursor.fetchall()
        }

    def save_topic_resolutions(
        self,
        catalog_fingerprint: str,
        requested_names: Iterable[str],
        resolved: dict[str, str],
    ) -> None:
        for topic_name in requested_names:
            topic_id = resolved.get(topic_name)
            self.cursor.execute(
                """
                INSERT INTO discovery_topic_resolutions (
                    catalog_fingerprint, topic_name, topic_id, status, resolved_at
                )
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (catalog_fingerprint, topic_name) DO UPDATE SET
                    topic_id = EXCLUDED.topic_id,
                    status = EXCLUDED.status,
                    resolved_at = now()
                """,
                (
                    catalog_fingerprint,
                    topic_name,
                    topic_id,
                    "resolved" if topic_id else "unresolved",
                ),
            )
        self.conn.commit()

    def persist_discovery_candidates(
        self,
        pipeline_run_id: int,
        candidates: Iterable[dict[str, Any]],
    ) -> int:
        count = 0
        for candidate in candidates:
            paper_id = candidate.get("paper_id") or candidate.get("paperId")
            if not paper_id:
                continue
            self.cursor.execute("SELECT id FROM publications WHERE paper_id = %s", (paper_id,))
            publication = self.cursor.fetchone()
            if not publication:
                continue
            raw = {
                "title": candidate.get("title"),
                "year": candidate.get("year"),
                "doi": candidate.get("doi"),
                "source_url": candidate.get("source_url") or candidate.get("url"),
                "open_access_url": candidate.get("open_access_url"),
                "oa_status": candidate.get("oa_status"),
                "pdf_locations": {
                    key: candidate["raw"][key]
                    for key in ("locations", "best_oa_location", "primary_location")
                    if isinstance(candidate.get("raw"), dict) and key in candidate["raw"]
                },
            }
            self.cursor.execute(
                """
                INSERT INTO discovery_candidates (
                    pipeline_run_id, publication_id, candidate_strength, evidence_tier,
                    evidence_reasons, matched_um_dataset_ids, pipeline_ready,
                    included, exclusion_reason, raw, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (pipeline_run_id, publication_id) DO UPDATE SET
                    candidate_strength = EXCLUDED.candidate_strength,
                    evidence_tier = EXCLUDED.evidence_tier,
                    evidence_reasons = EXCLUDED.evidence_reasons,
                    matched_um_dataset_ids = EXCLUDED.matched_um_dataset_ids,
                    pipeline_ready = EXCLUDED.pipeline_ready,
                    included = EXCLUDED.included,
                    exclusion_reason = EXCLUDED.exclusion_reason,
                    raw = EXCLUDED.raw,
                    updated_at = now()
                """,
                (
                    pipeline_run_id,
                    publication["id"],
                    candidate.get("candidate_strength") or 0,
                    candidate.get("evidence_tier") or "expanded",
                    candidate.get("evidence_reasons") or [],
                    candidate.get("matched_um_dataset_ids") or [],
                    bool(candidate.get("pipeline_ready")),
                    bool(candidate.get("included")),
                    candidate.get("exclusion_reason"),
                    psycopg2.extras.Json(raw),
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def list_discovery_candidates(
        self,
        pipeline_run_id: int,
        offset: int = 0,
        limit: int = 100,
        selected_only: bool = False,
    ) -> dict[str, Any]:
        selection_filter = " AND dc.included AND dc.pipeline_ready" if selected_only else ""
        self.cursor.execute(
            f"SELECT COUNT(*) AS count FROM discovery_candidates dc WHERE dc.pipeline_run_id = %s{selection_filter}",
            (pipeline_run_id,),
        )
        total = int((self.cursor.fetchone() or {}).get("count") or 0)
        self.cursor.execute(
            f"""
            SELECT
                p.paper_id,
                p.title,
                p.doi,
                p.year,
                p.source_url,
                p.open_access_url,
                p.oa_status,
                p.cited_by_count,
                p.primary_source_name,
                dc.candidate_strength,
                dc.evidence_tier,
                dc.evidence_reasons,
                dc.matched_um_dataset_ids,
                dc.pipeline_ready,
                dc.included,
                dc.exclusion_reason,
                dc.download_status,
                dc.download_failure_category
            FROM discovery_candidates dc
            JOIN publications p ON p.id = dc.publication_id
            WHERE dc.pipeline_run_id = %s
              {selection_filter}
            ORDER BY
                CASE dc.evidence_tier WHEN 'direct' THEN 3 WHEN 'exact' THEN 2 ELSE 1 END DESC,
                dc.candidate_strength DESC,
                p.cited_by_count DESC NULLS LAST,
                p.paper_id
            LIMIT %s OFFSET %s
            """,
            (pipeline_run_id, limit, offset),
        )
        return {
            "items": [dict(row) for row in self.cursor.fetchall()],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def included_discovery_paper_ids(self, pipeline_run_id: int) -> list[str]:
        self.cursor.execute(
            """
            SELECT p.paper_id
            FROM discovery_candidates dc
            JOIN publications p ON p.id = dc.publication_id
            WHERE dc.pipeline_run_id = %s AND dc.included AND dc.pipeline_ready
            ORDER BY
                CASE dc.evidence_tier WHEN 'direct' THEN 3 WHEN 'exact' THEN 2 ELSE 1 END DESC,
                dc.candidate_strength DESC,
                dc.id
            """,
            (pipeline_run_id,),
        )
        return [str(row["paper_id"]) for row in self.cursor.fetchall()]
