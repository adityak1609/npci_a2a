"""Per-user escalation scoping: record which portal user raised an NPCI ticket.

Escalations (and NPCI's answers to them) are personal to the user who raised
them — the dashboard scopes them by raised_by_user_id instead of leaking them
to whoever owns the change.

Revision ID: 20260704_0010_ticket_raised_by
Revises: 20260703_0009_change_soft_delete
Create Date: 2026-07-04
"""
from alembic import op

revision = "20260704_0010_ticket_raised_by"
down_revision = "20260703_0009_change_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE npci_tickets ADD COLUMN IF NOT EXISTS "
        "raised_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_npci_tickets_raised_by "
        "ON npci_tickets (raised_by_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_npci_tickets_raised_by")
    op.execute("ALTER TABLE npci_tickets DROP COLUMN IF EXISTS raised_by_user_id")
