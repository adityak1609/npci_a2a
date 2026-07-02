"""Add durable A2A received-change queue.

Revision ID: 20260702_0005_documents
Revises: 20260630_0004_add_mfa_fields
Create Date: 2026-07-02
"""
from alembic import op

revision = "20260702_0005_documents"
down_revision = "20260630_0004_add_mfa_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            change_id TEXT PRIMARY KEY,
            path TEXT,
            status TEXT NOT NULL,
            received_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            error TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_status_received "
        "ON documents (status, received_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_status_received")
    op.execute("DROP TABLE IF EXISTS documents")
