"""Run-scoped extraction state, request checkpoints and atomic publication."""

from __future__ import annotations

from typing import Any

import psycopg2.extras

from datasight.domain.extraction import PublicationExtraction


class ExtractionRepositoryMixin:
    conn: Any
    cursor: Any

    def extraction_publications(self, run_id: int, limit: int | None = None, publication_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT p.id AS publication_id, p.paper_id, p.title,
                   xml.path AS xml_path, md.id AS markdown_artifact_id,
                   md.path AS markdown_path, md.sha256 AS markdown_sha256, md.metadata AS markdown_metadata
            FROM publications p
            LEFT JOIN LATERAL (
                SELECT path FROM artifacts WHERE publication_id=p.id AND artifact_type='tei_xml'
                ORDER BY created_at DESC,id DESC LIMIT 1
            ) xml ON TRUE
            LEFT JOIN LATERAL (
                SELECT * FROM artifacts WHERE publication_id=p.id AND artifact_type='markdown'
                ORDER BY created_at DESC,id DESC LIMIT 1
            ) md ON TRUE
            WHERE EXISTS (
                SELECT 1 FROM discovery_candidates dc
                WHERE dc.pipeline_run_id=%s AND dc.publication_id=p.id AND dc.included AND dc.pipeline_ready
            )
        """
        params: list[Any] = [run_id]
        if publication_id is not None:
            query += " AND p.id=%s"
            params.append(publication_id)
        query += " ORDER BY p.id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def extraction_lock(self, run_id: int, publication_id: int | None = None) -> bool:
        # Shared run locks permit distinct papers; an exclusive lock reserves full-run resume.
        if publication_id is not None:
            self.cursor.execute("SELECT pg_advisory_lock_shared(73013, %s)", (run_id,))
            self.cursor.execute("SELECT pg_advisory_lock(73014, %s)", (publication_id,))
            return True
        self.cursor.execute("SELECT pg_try_advisory_lock(73013, %s) AS locked", (run_id,))
        return bool((self.cursor.fetchone() or {}).get("locked"))

    def latest_publication_extraction(self, run_id: int, publication_id: int) -> dict[str, Any] | None:
        self.cursor.execute("SELECT * FROM publication_extractions WHERE pipeline_run_id=%s AND publication_id=%s ORDER BY id DESC LIMIT 1", (run_id, publication_id))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def evaluation_extraction_rows(self, run_id: int) -> list[dict[str, Any]]:
        self.cursor.execute("""
            SELECT p.paper_id,p.title,e.id AS extraction_id,COALESCE(e.status,'pending') AS status,
                   e.source_sha256,e.source_path,e.fingerprint,e.config,e.coverage,e.metrics,e.error,e.result
            FROM discovery_candidates dc JOIN publications p ON p.id=dc.publication_id
            LEFT JOIN LATERAL (
                SELECT * FROM publication_extractions WHERE pipeline_run_id=dc.pipeline_run_id AND publication_id=p.id
                ORDER BY id DESC LIMIT 1
            ) e ON TRUE WHERE dc.pipeline_run_id=%s AND dc.included ORDER BY dc.id
        """, (run_id,))
        return [dict(row) for row in self.cursor.fetchall()]

    def evaluation_request_attempt_rows(self, run_id: int) -> list[dict[str, Any]]:
        self.cursor.execute("""
            SELECT p.paper_id, e.id AS extraction_id, r.work_key, r.status AS request_status,
                   attempt.ordinality AS attempt_number,
                   (attempt.value->>'http_status')::integer AS http_status,
                   (attempt.value->>'elapsed_seconds')::double precision AS elapsed_seconds,
                   (attempt.value->'usage'->>'prompt_tokens')::integer AS prompt_tokens,
                   (attempt.value->'usage'->>'completion_tokens')::integer AS completion_tokens,
                   (attempt.value->'usage'->>'total_tokens')::integer AS total_tokens,
                   CASE WHEN attempt.value->>'error' IS NULL THEN NULL
                        WHEN attempt.value->>'http_status'='503' THEN 'unavailable'
                        WHEN attempt.value->>'http_status'='429' THEN 'rate_limited'
                        WHEN attempt.value->>'http_status'='413' THEN 'request_too_large'
                        WHEN (attempt.value->>'http_status')::integer >= 500 THEN 'server_error'
                        WHEN attempt.value->>'error' LIKE 'Completion reached its output limit%%' THEN 'output_truncated'
                        WHEN attempt.value->>'error' LIKE 'Gateway connection failed or timed out%%' THEN 'connection_or_timeout'
                        WHEN attempt.value->>'error' LIKE 'Invalid completion:%%' THEN 'invalid_completion'
                        WHEN attempt.value->>'error' LIKE 'extraction_budget_exceeded:%%' THEN 'extraction_budget_exceeded'
                        ELSE 'request_error' END AS error_category
            FROM discovery_candidates dc
            JOIN publications p ON p.id=dc.publication_id
            JOIN LATERAL (
                SELECT id FROM publication_extractions
                WHERE pipeline_run_id=dc.pipeline_run_id AND publication_id=p.id
                ORDER BY id DESC LIMIT 1
            ) e ON TRUE
            JOIN extraction_requests r ON r.extraction_id=e.id
            CROSS JOIN LATERAL jsonb_array_elements(r.attempts) WITH ORDINALITY AS attempt(value, ordinality)
            WHERE dc.pipeline_run_id=%s AND dc.included
            ORDER BY p.paper_id, r.id, attempt.ordinality
        """, (run_id,))
        return [dict(row) for row in self.cursor.fetchall()]

    def begin_publication_extraction(self, run_id: int, publication_id: int, fingerprint: str, configuration: dict[str, Any]) -> dict[str, Any]:
        self.cursor.execute("""
            INSERT INTO publication_extractions(pipeline_run_id,publication_id,fingerprint,config)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(pipeline_run_id,publication_id,fingerprint) DO UPDATE SET updated_at=now()
            RETURNING *
        """, (run_id, publication_id, fingerprint, psycopg2.extras.Json(configuration)))
        result = dict(self.cursor.fetchone())
        self.conn.commit()
        return result

    def attach_extraction_source(self, extraction_id: int, path: str, digest: str, artifact_id: int | None) -> None:
        self.cursor.execute("""
            UPDATE publication_extractions SET source_path=%s,source_sha256=%s,source_artifact_id=%s,
                status='running',error=NULL,updated_at=now() WHERE id=%s AND status<>'successful'
        """, (path, digest, artifact_id, extraction_id))
        self.conn.commit()

    def get_extraction_request(self, extraction_id: int, key: str) -> dict[str, Any] | None:
        self.cursor.execute("SELECT * FROM extraction_requests WHERE extraction_id=%s AND work_key=%s", (extraction_id, key))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def begin_extraction_request(self, extraction_id: int, key: str, payload: dict[str, Any]) -> None:
        self.cursor.execute("""
            INSERT INTO extraction_requests(extraction_id,work_key,status,request)
            VALUES (%s,%s,'running',%s)
            ON CONFLICT(extraction_id,work_key) DO UPDATE SET status='running',error=NULL,updated_at=now()
        """, (extraction_id, key, psycopg2.extras.Json(payload)))
        self.conn.commit()

    def record_extraction_attempt(self, extraction_id: int, key: str, attempt: dict[str, Any]) -> None:
        response = attempt.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        attempt = {**attempt, "usage": {key: value for key, value in usage.items() if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int and value >= 0}}
        self.cursor.execute("""
            UPDATE extraction_requests SET attempts=attempts || %s::jsonb,updated_at=now()
            WHERE extraction_id=%s AND work_key=%s
        """, (psycopg2.extras.Json([attempt]), extraction_id, key))
        self.conn.commit()

    def finish_extraction_request(self, extraction_id: int, key: str, result: dict[str, Any] | None, error: str | None) -> None:
        self.cursor.execute("""
            UPDATE extraction_requests SET status=%s,result=%s,error=%s,updated_at=now()
            WHERE extraction_id=%s AND work_key=%s
        """, ("failed" if error else "successful", psycopg2.extras.Json(result), error, extraction_id, key))
        self.conn.commit()

    def fail_publication_extraction(self, extraction_id: int, status: str, error: str) -> None:
        self.conn.rollback()
        metrics = self.extraction_metrics(extraction_id)
        self.cursor.execute("UPDATE publication_extractions SET status=%s,error=%s,metrics=%s,updated_at=now() WHERE id=%s AND status<>'successful'", (status, error, psycopg2.extras.Json(metrics), extraction_id))
        self.conn.commit()

    def record_extraction_progress(self, extraction_id: int, progress: dict[str, Any]) -> None:
        self.cursor.execute("""UPDATE publication_extractions SET metrics=metrics || jsonb_build_object('progress',%s::jsonb),
            updated_at=now() WHERE id=%s""", (psycopg2.extras.Json(progress), extraction_id))
        self.conn.commit()

    def extraction_metrics(self, extraction_id: int) -> dict[str, Any]:
        self.cursor.execute("""SELECT count(*) AS attempts,
            COALESCE(sum((a->>'elapsed_seconds')::numeric),0) AS elapsed_seconds,
            COALESCE(sum((a->'usage'->>'prompt_tokens')::bigint),0)::bigint AS prompt_tokens,
            COALESCE(sum((a->'usage'->>'completion_tokens')::bigint),0)::bigint AS completion_tokens,
            COALESCE(sum((a->'usage'->>'total_tokens')::bigint),0)::bigint AS total_tokens
            FROM extraction_requests r CROSS JOIN LATERAL jsonb_array_elements(r.attempts) a
            WHERE r.extraction_id=%s""", (extraction_id,))
        result = dict(self.cursor.fetchone())
        result["elapsed_seconds"] = float(result["elapsed_seconds"])
        self.cursor.execute("SELECT count(*) AS count FROM extraction_requests WHERE extraction_id=%s", (extraction_id,))
        result["requests"] = self.cursor.fetchone()["count"]
        self.cursor.execute('SELECT metrics FROM publication_extractions WHERE id=%s', (extraction_id,))
        existing = (self.cursor.fetchone() or {}).get('metrics') or {}
        if 'progress' in existing:
            result['progress'] = existing['progress']
        return result

    def publish_extraction(self, extraction_id: int, result: PublicationExtraction) -> None:
        try:
            self.cursor.execute("SELECT publication_id,status,config FROM publication_extractions WHERE id=%s FOR UPDATE", (extraction_id,))
            row = self.cursor.fetchone()
            if row["status"] == "successful":
                self.conn.commit()
                return
            result = result.model_copy(update={'prompt_version': row['config'].get('prompt_version', result.prompt_version)})
            evidence = {e.evidence_id: e for e in result.evidence}
            for dataset in result.datasets:
                first = evidence[dataset.occurrences[0].evidence_ids[0]]
                roles = {item.value for item in dataset.use.roles.values} if dataset.use.roles.state == "reported" else set()
                subjects = {item.value for item in dataset.use.claim_subject.values}
                role = next(iter(roles)) if len(roles) == 1 and subjects == {"current_publication"} else "unclear"
                # Compatibility fields are projections; the typed features remain authoritative.
                dataset_ids = [item.value.literal for item in dataset.identity.identifiers.values if item.value.target == "dataset"] if dataset.identity.identifiers.state == "reported" else []
                dataset_urls = [item.value.url for item in dataset.identity.urls.values if item.value.target in {"landing_page", "download"}] if dataset.identity.urls.state == "reported" else []
                feature_dict = dataset.model_dump(mode="json")
                metadata = {
                    "persistent_identifier": dataset_ids[0] if len(dataset_ids) == 1 else None,
                    "dataset_url": dataset_urls[0] if len(dataset_urls) == 1 else None,
                    "dataset_authors": [item.value for item in dataset.identity.creators.values] if dataset.identity.creators.state == "reported" else [],
                    "dataset_year": dataset.identity.year.values[0].value if dataset.identity.year.state == "reported" and len(dataset.identity.year.values) == 1 else None,
                    "features": feature_dict,
                }
                provenance = {**row["config"], "extraction_id": extraction_id, "dataset_local_id": dataset.dataset_local_id,
                              "source_sha256": result.source_sha256, "char_start": first.char_start, "char_end": first.char_end}
                self.cursor.execute("""
                    INSERT INTO dataset_mentions(publication_id,dataset_name,aliases,dataset_role,
                        reference_directness,evidence,metadata,provenance,confidence,extraction_id,dataset_local_id)
                    VALUES (%s,%s,%s,%s,'unclear',%s,%s,%s,NULL,%s,%s)
                    ON CONFLICT(extraction_id,dataset_local_id) WHERE extraction_id IS NOT NULL DO NOTHING
                """, (row["publication_id"], dataset.display_name, [item.value for item in dataset.identity.aliases.values] if dataset.identity.aliases.state == "reported" else [], role,
                      psycopg2.extras.Json({"body_quote": first.quote, "section_heading": first.section_heading,
                          "standardized_section": first.standardized_section, "occurrences": feature_dict["occurrences"],
                          "passages": [e.model_dump(mode="json") for e in result.evidence]}),
                      psycopg2.extras.Json(metadata), psycopg2.extras.Json(provenance), extraction_id, dataset.dataset_local_id))
            metrics = self.extraction_metrics(extraction_id)
            self.cursor.execute("""
                UPDATE publication_extractions SET status='successful',result=%s,coverage=%s,error=NULL,
                    metrics=%s,updated_at=now() WHERE id=%s
            """, (psycopg2.extras.Json(result.model_dump(mode="json")), psycopg2.extras.Json(result.coverage), psycopg2.extras.Json(metrics), extraction_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def list_extractions(self, run_id: int, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        # Include not-yet-started papers and preparation/ingestion failures.
        base = """
            FROM discovery_candidates dc JOIN publications p ON p.id=dc.publication_id
            LEFT JOIN LATERAL (
                SELECT * FROM publication_extractions WHERE pipeline_run_id=dc.pipeline_run_id AND publication_id=p.id
                ORDER BY id DESC LIMIT 1
            ) e ON TRUE
            LEFT JOIN LATERAL (
                SELECT pis.id,pis.error FROM pipeline_items pi
                JOIN pipeline_item_stages pis ON pis.pipeline_item_id=pi.id
                WHERE pi.pipeline_run_id=dc.pipeline_run_id AND pi.publication_id=p.id
                  AND pis.stage<>'extract_features' AND pis.status='failed'
                ORDER BY pis.id DESC LIMIT 1
            ) upstream ON TRUE
            WHERE dc.pipeline_run_id=%s AND dc.included AND dc.pipeline_ready
        """
        self.cursor.execute("SELECT count(*) AS total " + base, (run_id,))
        total = int(self.cursor.fetchone()["total"])
        self.cursor.execute("""SELECT p.paper_id,p.title,e.id,
            COALESCE(e.status,CASE WHEN upstream.id IS NOT NULL THEN 'failed' ELSE 'pending' END) AS status,
            COALESCE(e.error,upstream.error,CASE WHEN upstream.id IS NOT NULL THEN 'Upstream processing failed; full text was not extracted.' END) AS error,
            e.source_sha256,e.config,e.coverage,e.metrics,e.updated_at,
            jsonb_array_length(COALESCE(e.result->'datasets','[]'::jsonb)) AS dataset_count """ + base + " ORDER BY p.id LIMIT %s OFFSET %s", (run_id, limit, offset))
        return [dict(row) for row in self.cursor.fetchall()], total

    def extraction_detail(self, run_id: int, extraction_id: int) -> dict[str, Any] | None:
        self.cursor.execute("SELECT * FROM publication_extractions WHERE pipeline_run_id=%s AND id=%s", (run_id, extraction_id))
        row = self.cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        self.cursor.execute("""SELECT work_key,status,jsonb_array_length(attempts) AS attempt_count,error,updated_at,
            (SELECT COALESCE(sum((a->'usage'->>'total_tokens')::bigint),0)::bigint FROM jsonb_array_elements(attempts) a) AS total_tokens
            FROM extraction_requests WHERE extraction_id=%s ORDER BY id""", (extraction_id,))
        result["requests"] = [dict(request) for request in self.cursor.fetchall()]
        return result

    def interrupt_extraction_run(self, run_id: int, error: str) -> None:
        self.cursor.execute("UPDATE pipeline_runs SET status='interrupted',error=%s,updated_at=now() WHERE id=%s", (error, run_id))
        self.cursor.execute("UPDATE pipeline_item_stages pis SET status='interrupted',error=%s,updated_at=now() FROM pipeline_items pi WHERE pi.id=pis.pipeline_item_id AND pi.pipeline_run_id=%s AND pis.stage='extract_features' AND pis.status<>'successful'", (error, run_id))
        self.cursor.execute("UPDATE pipeline_items SET status='interrupted',updated_at=now() WHERE pipeline_run_id=%s AND status<>'successful'", (run_id,))
        self.conn.commit()

    def claim_extraction_resume(self, run_id: int) -> None:
        self.cursor.execute("SELECT status FROM pipeline_runs WHERE id=%s FOR UPDATE", (run_id,))
        row = self.cursor.fetchone()
        if not row:
            raise LookupError("Pipeline run not found")
        if row["status"] in {"running", "started", "queued"}:
            raise ValueError("This run already has active execution")
        self.cursor.execute("""SELECT count(*) AS active FROM pipeline_item_stages pis
            JOIN pipeline_items pi ON pi.id=pis.pipeline_item_id
            WHERE pi.pipeline_run_id=%s AND pis.stage<>'extract_features'
              AND pis.status IN ('pending','queued','running','started')""", (run_id,))
        if int(self.cursor.fetchone()["active"]):
            raise ValueError("Upstream processing is still active; resume when it finishes")
        self.cursor.execute("UPDATE pipeline_runs SET status='queued',error=NULL,finished_at=NULL,updated_at=now() WHERE id=%s", (run_id,))
        self.cursor.execute("UPDATE stage_runs SET status='queued',error=NULL,finished_at=NULL WHERE pipeline_run_id=%s AND stage='extract_features'", (run_id,))
        self.conn.commit()

    def finish_resumed_extraction_items(self, run_id: int) -> None:
        self.cursor.execute("""
            UPDATE pipeline_item_stages pis SET status=e.status, error=e.error,finished_at=now(),updated_at=now()
            FROM pipeline_items pi, LATERAL (
                SELECT status,error FROM publication_extractions
                WHERE pipeline_run_id=pi.pipeline_run_id AND publication_id=pi.publication_id ORDER BY id DESC LIMIT 1
            ) e WHERE pi.id=pis.pipeline_item_id AND pi.pipeline_run_id=%s AND pis.stage='extract_features'
        """, (run_id,))
        self.cursor.execute("""UPDATE pipeline_items pi SET status=pis.status,updated_at=now()
            FROM pipeline_item_stages pis WHERE pis.pipeline_item_id=pi.id AND pis.stage='extract_features' AND pi.pipeline_run_id=%s""", (run_id,))
        self.conn.commit()
