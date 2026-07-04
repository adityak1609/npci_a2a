"""Change ownership: link an A2A change to the concerned bank product manager.

Adds a2a_changes.owner_user_id so NPCI updates (counter-proposal decisions,
blocker resolutions, milestone acks, ticket answers) for a change can be scoped
to a single portal user's dashboard.

Revision ID: 20260703_0007_change_owner
Revises: 20260702_0006_part_a_portal
Create Date: 2026-07-03
"""
from alembic import op

revision = "20260703_0007_change_owner"
down_revision = "20260702_0006_part_a_portal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE a2a_changes ADD COLUMN IF NOT EXISTS owner_user_id INTEGER"
    )
    # FK is added separately and defensively — the users table always exists by
    # this revision, but IF NOT EXISTS keeps re-runs idempotent.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_a2a_changes_owner_user'
            ) THEN
                ALTER TABLE a2a_changes
                    ADD CONSTRAINT fk_a2a_changes_owner_user
                    FOREIGN KEY (owner_user_id) REFERENCES users (id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_a2a_changes_owner_user_id "
        "ON a2a_changes (owner_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_a2a_changes_owner_user_id")
    op.execute("ALTER TABLE a2a_changes DROP CONSTRAINT IF EXISTS fk_a2a_changes_owner_user")
    op.execute("ALTER TABLE a2a_changes DROP COLUMN IF EXISTS owner_user_id")
