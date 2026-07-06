"""Postgres-backed login failure tracking for account lockout.

The lockout previously lived in the Redis/in-memory rate limiter, which fails
open per-process when Redis is down (attempts × workers). Failures are now
durable rows shared by all workers; the daily retention job purges old ones.

Revision ID: 20260705_0011_login_failures
Revises: 20260704_0010_ticket_raised_by
Create Date: 2026-07-05
"""
from alembic import op

revision = "20260705_0011_login_failures"
down_revision = "20260704_0010_ticket_raised_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS login_failures (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_login_failures_email_created "
        "ON login_failures (email, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_login_failures_email_created")
    op.execute("DROP TABLE IF EXISTS login_failures")
