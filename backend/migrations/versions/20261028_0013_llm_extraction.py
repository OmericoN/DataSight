"""Versioned publication extraction, resumable requests and dataset linkage."""

from alembic import op

revision = "20261028_0013"
down_revision = "20260824_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE publication_extractions (
            id BIGSERIAL PRIMARY KEY,
            pipeline_run_id BIGINT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            publication_id BIGINT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
            source_artifact_id BIGINT REFERENCES artifacts(id) ON DELETE SET NULL,
            source_sha256 TEXT,
            source_path TEXT,
            fingerprint TEXT NOT NULL,
            config JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','running','interrupted','failed','successful')),
            coverage JSONB NOT NULL DEFAULT '{}',
            metrics JSONB NOT NULL DEFAULT '{}',
            result JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (pipeline_run_id, publication_id, fingerprint)
        )
    """)
    op.execute("""
        CREATE TABLE extraction_requests (
            id BIGSERIAL PRIMARY KEY,
            extraction_id BIGINT NOT NULL REFERENCES publication_extractions(id) ON DELETE CASCADE,
            work_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running','successful','failed')),
            request JSONB NOT NULL,
            attempts JSONB NOT NULL DEFAULT '[]',
            result JSONB,
            error TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (extraction_id, work_key)
        )
    """)
    op.execute("ALTER TABLE dataset_mentions ALTER COLUMN confidence DROP NOT NULL")
    op.execute("ALTER TABLE dataset_mentions ADD COLUMN extraction_id BIGINT REFERENCES publication_extractions(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE dataset_mentions ADD COLUMN dataset_local_id TEXT")
    op.execute("CREATE UNIQUE INDEX uq_dataset_extraction ON dataset_mentions(extraction_id, dataset_local_id) WHERE extraction_id IS NOT NULL")
    op.execute("CREATE INDEX idx_extraction_run ON publication_extractions(pipeline_run_id, publication_id, id DESC)")


def downgrade() -> None:
    op.execute("DELETE FROM dataset_mentions WHERE extraction_id IS NOT NULL")
    op.execute("DROP INDEX uq_dataset_extraction")
    op.execute("ALTER TABLE dataset_mentions DROP COLUMN dataset_local_id, DROP COLUMN extraction_id")
    op.execute("UPDATE dataset_mentions SET confidence = 0 WHERE confidence IS NULL")
    op.execute("ALTER TABLE dataset_mentions ALTER COLUMN confidence SET NOT NULL")
    op.execute("DROP TABLE extraction_requests")
    op.execute("DROP TABLE publication_extractions")
