"""Hidden updates: let admins hide portal dashboard update items from users.

Revision ID: 20260703_0008_hidden_updates
Revises: 20260703_0007_change_owner
Create Date: 2026-07-03
"""
from alembic import op

revision = "20260703_0008_hidden_updates"
down_revision = "20260703_0007_change_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hidden_updates (
            id SERIAL PRIMARY KEY,
            kind VARCHAR(50) NOT NULL,
            ref VARCHAR(200) NOT NULL,
            hidden_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            CONSTRAINT uq_hidden_update_kind_ref UNIQUE (kind, ref)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hidden_updates")
