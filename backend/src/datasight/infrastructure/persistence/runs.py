"""Focused persistence methods."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg2.extras

from datasight.domain.stages import WORKFLOW_STAGE_ORDER
from datasight.infrastructure.persistence.events import event_level, event_message


class RunRepositoryMixin:
    conn: Any
    cursor: Any

    def create_pipeline_run(self, query: str, config: dict[str, Any] | None = None) -> int:
        run_key = f"run-{uuid4()}"
        run_config = config or {}
        self.cursor.execute(
            """
            INSERT INTO pipeline_runs (run_key, query, status, config)
            VALUES (%s, %s, 'running', %s)
            RETURNING id
            """,
            (run_key, query, psycopg2.extras.Json(run_config)),
        )
        row = self.cursor.fetchone()
        if not row:
            raise RuntimeError("Failed to create pipeline run")
        pipeline_run_id = int(row["id"])
        self._insert_pipeline_run_event(
            pipeline_run_id=pipeline_run_id,
            stage=None,
            level="info",
            message=f'Pipeline run created for "{query}".',
            payload={"query": query, "config": run_config, "run_key": run_key},
        )
        self.conn.commit()
        return pipeline_run_id

    def update_pipeline_run_task_id(self, pipeline_run_id: int, task_id: str | None) -> None:
        self.cursor.execute(
            "UPDATE pipeline_runs SET celery_task_id = %s, updated_at = now() WHERE id = %s",
            (task_id, pipeline_run_id),
        )
        self.conn.commit()

    def record_stage_result(
        self,
        stage: str,
        status: str,
        metrics: dict[str, Any] | None = None,
        pipeline_run_id: int | None = None,
        error: str | None = None,
    ) -> None:
        if pipeline_run_id is None:
            return
        self.cursor.execute(
            """
            SELECT id, attempt_count
            FROM stage_runs
            WHERE pipeline_run_id = %s AND publication_id IS NULL AND stage = %s
            ORDER BY id
            LIMIT 1
            """,
            (pipeline_run_id, str(stage)),
        )
        existing = self.cursor.fetchone()
        if existing:
            self.cursor.execute(
                """
                UPDATE stage_runs
                SET status = %s,
                    attempt_count = GREATEST(attempt_count, 1),
                    error = %s,
                    metrics = %s,
                    started_at = COALESCE(started_at, now()),
                    finished_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    status,
                    error,
                    psycopg2.extras.Json(metrics or {}),
                    existing["id"],
                ),
            )
        else:
            self.cursor.execute(
                """
                INSERT INTO stage_runs (
                    pipeline_run_id, stage, status, attempt_count, error, metrics,
                    started_at, finished_at, updated_at
                )
                VALUES (%s, %s, %s, 1, %s, %s, now(), now(), now())
                """,
                (
                    pipeline_run_id,
                    stage,
                    status,
                    error,
                    psycopg2.extras.Json(metrics or {}),
                ),
            )
        self._insert_pipeline_run_event(
            pipeline_run_id=pipeline_run_id,
            stage=str(stage),
            level=event_level(status),
            message=event_message(str(stage), status, metrics, error),
            payload=metrics or {},
        )
        self.conn.commit()

    def finish_pipeline_run(self, pipeline_run_id: int | None, status: str) -> None:
        if pipeline_run_id is None:
            return
        self.cursor.execute(
            "UPDATE pipeline_runs SET status = CASE WHEN status='interrupted' THEN status ELSE %s END, finished_at = now(), updated_at = now() WHERE id = %s RETURNING status",
            (status, pipeline_run_id),
        )
        status = str((self.cursor.fetchone() or {}).get("status") or status)
        self._insert_pipeline_run_event(
            pipeline_run_id=pipeline_run_id,
            stage=None,
            level=event_level(status),
            message=f"Pipeline run finished with status: {status}.",
            payload={"status": status},
        )
        self.conn.commit()

    def standard_run_outcome(self, pipeline_run_id: int) -> str:
        self.cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status IN ('failed','completed_with_errors')) AS count,
                   COUNT(*) FILTER (WHERE status='interrupted') AS interrupted
            FROM stage_runs
            WHERE pipeline_run_id = %s
              AND stage = ANY(%s)
              AND status IN ('failed', 'completed_with_errors', 'interrupted')
            """,
            (pipeline_run_id, [stage.value for stage in WORKFLOW_STAGE_ORDER]),
        )
        row = self.cursor.fetchone() or {}
        if int(row.get("interrupted") or 0):
            return "interrupted"
        return "completed_with_errors" if int(row.get("count") or 0) else "successful"

    def fail_pipeline_run(self, pipeline_run_id: int | None, error: str) -> None:
        if pipeline_run_id is None:
            return
        self.cursor.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', error = %s, finished_at = now(), updated_at = now()
            WHERE id = %s AND status <> 'interrupted'
            """,
            (error, pipeline_run_id),
        )
        self._insert_pipeline_run_event(
            pipeline_run_id=pipeline_run_id,
            stage=None,
            level="error",
            message="Pipeline run failed.",
            payload={"error": error},
        )
        self.conn.commit()

    def list_pipeline_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                pr.id,
                pr.run_key,
                pr.query,
                pr.status,
                pr.config,
                pr.celery_task_id,
                pr.error,
                pr.created_at,
                pr.updated_at,
                pr.finished_at
            FROM pipeline_runs pr
            ORDER BY pr.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        runs = [dict(row) for row in self.cursor.fetchall()]
        for run in runs:
            run["stages"] = self.list_stage_runs(int(run["id"]))
        return runs

    def get_pipeline_run(self, pipeline_run_id: int) -> dict[str, Any] | None:
        self.cursor.execute(
            """
            SELECT
                id,
                run_key,
                query,
                status,
                config,
                celery_task_id,
                error,
                created_at,
                updated_at,
                finished_at
            FROM pipeline_runs
            WHERE id = %s
            """,
            (pipeline_run_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        run = dict(row)
        run["stages"] = self.list_stage_runs(pipeline_run_id)
        return run

    def list_stage_runs(self, pipeline_run_id: int) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                id,
                stage,
                status,
                attempt_count,
                task_id,
                error,
                metrics,
                started_at,
                finished_at,
                created_at,
                updated_at
            FROM stage_runs
            WHERE pipeline_run_id = %s
            ORDER BY id
            """,
            (pipeline_run_id,),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def list_pipeline_run_events(self, pipeline_run_id: int, limit: int = 200) -> list[dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT
                id,
                pipeline_run_id,
                stage,
                level,
                message,
                payload,
                created_at
            FROM pipeline_run_events
            WHERE pipeline_run_id = %s
            ORDER BY created_at, id
            LIMIT %s
            """,
            (pipeline_run_id, limit),
        )
        return [dict(row) for row in self.cursor.fetchall()]

    def active_run_count(self) -> int:
        self.cursor.execute(
            "SELECT COUNT(*) AS count FROM pipeline_runs WHERE status IN ('queued', 'running', 'started', 'interrupted')"
        )
        row = self.cursor.fetchone() or {}
        return int(row.get("count") or 0)

    def reset_database(self) -> list[str]:
        self.cursor.execute(
            """
            SELECT tablename, quote_ident(tablename) AS quoted_table_name
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename <> 'alembic_version'
              AND tablename <> 'um_datasets'
            ORDER BY tablename
            """
        )
        rows = self.cursor.fetchall()
        tables = [str(row["tablename"]) for row in rows]
        if not tables:
            return []

        quoted_tables = [str(row["quoted_table_name"]) for row in rows]
        self.cursor.execute(
            f"TRUNCATE {', '.join(quoted_tables)} RESTART IDENTITY CASCADE"
        )
        self.conn.commit()
        return tables

    def _insert_pipeline_run_event(
        self,
        pipeline_run_id: int,
        stage: str | None,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.cursor.execute(
            """
            INSERT INTO pipeline_run_events (pipeline_run_id, stage, level, message, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (pipeline_run_id, stage, level, message, psycopg2.extras.Json(payload or {})),
        )
