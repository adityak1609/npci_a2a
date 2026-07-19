"""Hybrid retrieval schema: parent-child chunks + BM25 full-text search.

Adds to document_chunks:
  * parent_key / parent_text — children are embedded for precise matching,
    while the LLM receives the full parent passage (parent-child chunking).
  * text_search — a STORED generated tsvector column so every ingestion path
    (PDF upload, A2A ingest, KB paste, NPCI replies) gets lexical/BM25-style
    search for free, without each writer having to maintain the column.
  * GIN index on text_search for fast @@ queries.

Revision ID: 20260710_0012_hybrid_retrieval
Revises: 20260705_0011_login_failures
Create Date: 2026-07-10
"""
from alembic import op

revision = "20260710_0012_hybrid_retrieval"
down_revision = "20260705_0011_login_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN IF NOT EXISTS parent_key VARCHAR(150)"
    )
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN IF NOT EXISTS parent_text TEXT"
    )
    # Generated column keeps the lexical index consistent no matter which
    # code path inserts or updates a chunk. Requires PostgreSQL 12+.
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN IF NOT EXISTS text_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_text_search "
        "ON document_chunks USING gin (text_search)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_text_search")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS text_search")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS parent_text")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS parent_key")
