"""baseline schema

Revision ID: 20260629_0001
Revises:
Create Date: 2026-06-29 00:00:00+00:00
"""

import os

from alembic import op
from sqlalchemy import text

from database import Base


revision = "20260629_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=bind)

    bind.execute(text("""
        CREATE OR REPLACE FUNCTION prevent_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit table is append-only';
        END;
        $$ LANGUAGE plpgsql
    """))
    for table_name in ("audit_logs", "a2a_messages"):
        bind.execute(text(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}"
        ))
        bind.execute(text(
            f"CREATE TRIGGER trg_{table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()"
        ))

    bind.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """))


def downgrade() -> None:
    raise RuntimeError("Baseline production schema downgrade is not supported.")
