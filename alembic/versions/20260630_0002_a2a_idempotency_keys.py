"""add a2a idempotency claims

Revision ID: 20260630_0002
Revises: 20260629_0001
Create Date: 2026-06-30 00:00:00+00:00
"""

from alembic import op
from sqlalchemy import text


revision = "20260630_0002"
down_revision = "20260629_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("""
        CREATE TABLE IF NOT EXISTS a2a_idempotency_keys (
            id SERIAL PRIMARY KEY,
            key_type VARCHAR(50) NOT NULL,
            key_value VARCHAR(512) NOT NULL,
            message_id VARCHAR(100),
            task_type VARCHAR(100),
            status VARCHAR(50) NOT NULL DEFAULT 'processing',
            result_payload TEXT,
            error_code VARCHAR(100),
            http_status INTEGER,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_a2a_idempotency_key UNIQUE (key_type, key_value)
        )
    """))
    bind.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_a2a_idempotency_status_updated
        ON a2a_idempotency_keys (status, updated_at)
    """))
    bind.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_a2a_idempotency_message_id
        ON a2a_idempotency_keys (message_id)
    """))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP INDEX IF EXISTS ix_a2a_idempotency_message_id"))
    bind.execute(text("DROP INDEX IF EXISTS ix_a2a_idempotency_status_updated"))
    bind.execute(text("DROP TABLE IF EXISTS a2a_idempotency_keys"))
