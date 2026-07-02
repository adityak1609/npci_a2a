"""Add MFA fields to users table.

Revision ID: 20260630_0004_add_mfa_fields
Revises: 20260630_0003
Create Date: 2026-06-30
"""
from alembic import op

revision = "20260630_0004_add_mfa_fields"
down_revision = "20260630_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use IF NOT EXISTS to be idempotent — baseline migration may have already
    # created these columns via Base.metadata.create_all().
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(64)"
    )


def downgrade() -> None:
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "mfa_enabled")
