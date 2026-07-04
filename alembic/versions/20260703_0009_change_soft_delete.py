"""Change soft delete: let admins remove a change from the board/dashboards/assistant.

Revision ID: 20260703_0009_change_soft_delete
Revises: 20260703_0008_hidden_updates
Create Date: 2026-07-03
"""
from alembic import op

revision = "20260703_0009_change_soft_delete"
down_revision = "20260703_0008_hidden_updates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE a2a_changes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE a2a_changes DROP COLUMN IF EXISTS deleted_at")
