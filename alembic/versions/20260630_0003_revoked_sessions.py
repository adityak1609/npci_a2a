"""add revoked jwt sessions

Revision ID: 20260630_0003
Revises: 20260630_0002
Create Date: 2026-06-30 00:00:00+00:00
"""

from alembic import op
from sqlalchemy import text


revision = "20260630_0003"
down_revision = "20260630_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("""
        CREATE TABLE IF NOT EXISTS revoked_sessions (
            id SERIAL PRIMARY KEY,
            jti VARCHAR(64) NOT NULL UNIQUE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reason VARCHAR(100),
            expires_at TIMESTAMP NOT NULL,
            revoked_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    bind.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_revoked_sessions_jti
        ON revoked_sessions (jti)
    """))
    bind.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_revoked_sessions_expires
        ON revoked_sessions (expires_at)
    """))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP INDEX IF EXISTS ix_revoked_sessions_expires"))
    bind.execute(text("DROP INDEX IF EXISTS ix_revoked_sessions_jti"))
    bind.execute(text("DROP TABLE IF EXISTS revoked_sessions"))
