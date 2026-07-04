"""Part A (Phase C) tables + Partner Portal schema.

Adds counter-proposal / blocker / milestone tracking for the Part A change
lifecycle, the app_settings runtime config store, and user portal columns
(username, is_active).

Revision ID: 20260702_0006_part_a_portal
Revises: 20260702_0005_documents
Create Date: 2026-07-02
"""
from alembic import op

revision = "20260702_0006_part_a_portal"
down_revision = "20260702_0005_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── a2a_changes: dashboard title + blocked flag ────────────────────────
    op.execute("ALTER TABLE a2a_changes ADD COLUMN IF NOT EXISTS title VARCHAR(512)")
    op.execute("ALTER TABLE a2a_changes ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP")
    op.execute("ALTER TABLE a2a_changes ADD COLUMN IF NOT EXISTS blocked_reason TEXT")

    # ── users: portal login + activation ───────────────────────────────────
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(150)")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)"
    )

    # ── a2a_counter_proposals ──────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS a2a_counter_proposals (
            id SERIAL PRIMARY KEY,
            counter_proposal_id VARCHAR(100) NOT NULL UNIQUE,
            change_id VARCHAR(100) NOT NULL,
            version_targeted INTEGER,
            status VARCHAR(50) NOT NULL DEFAULT 'open',
            counters_json TEXT,
            decision_json TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_a2a_counter_proposals_change "
        "ON a2a_counter_proposals (change_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_a2a_counter_proposals_status "
        "ON a2a_counter_proposals (status)"
    )

    # ── a2a_blockers ───────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS a2a_blockers (
            id SERIAL PRIMARY KEY,
            blocker_id VARCHAR(100) NOT NULL UNIQUE,
            change_id VARCHAR(100) NOT NULL,
            type VARCHAR(100),
            severity VARCHAR(50),
            subject VARCHAR(512),
            status VARCHAR(50) NOT NULL DEFAULT 'open',
            resolution VARCHAR(50),
            resolution_text TEXT,
            crm_json TEXT,
            payload_json TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_a2a_blockers_change ON a2a_blockers (change_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_a2a_blockers_status ON a2a_blockers (status)")

    # ── a2a_milestones ─────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS a2a_milestones (
            id SERIAL PRIMARY KEY,
            change_id VARCHAR(100) NOT NULL,
            milestone VARCHAR(50) NOT NULL,
            state VARCHAR(50) NOT NULL,
            version_implementing INTEGER,
            notes TEXT,
            eta VARCHAR(50),
            updated_at TIMESTAMP DEFAULT NOW(),
            CONSTRAINT uq_a2a_milestone_change_ms UNIQUE (change_id, milestone)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_a2a_milestones_change ON a2a_milestones (change_id)")

    # ── app_settings ───────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id SERIAL PRIMARY KEY,
            key VARCHAR(100) NOT NULL UNIQUE,
            value TEXT,
            category VARCHAR(50) NOT NULL DEFAULT 'general',
            is_secret BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_app_settings_key ON app_settings (key)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_app_settings_category ON app_settings (category)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
    op.execute("DROP TABLE IF EXISTS a2a_milestones")
    op.execute("DROP TABLE IF EXISTS a2a_blockers")
    op.execute("DROP TABLE IF EXISTS a2a_counter_proposals")
    op.execute("DROP INDEX IF EXISTS ix_users_username")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS username")
    op.execute("ALTER TABLE a2a_changes DROP COLUMN IF EXISTS blocked_reason")
    op.execute("ALTER TABLE a2a_changes DROP COLUMN IF EXISTS blocked_at")
    op.execute("ALTER TABLE a2a_changes DROP COLUMN IF EXISTS title")
