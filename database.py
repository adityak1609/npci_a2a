"""
database.py — SQLAlchemy models and database engine for Mindgate Policy Bot.

Tables:
    users           — Registered user accounts (email + hashed password)
    chat_sessions   — Logical chat sessions tied to a user
    chat_messages   — Individual messages within a session (role + content)
    document_chunks — PDF text chunks with pgvector embeddings

The pgvector extension must be enabled in your Postgres database:
    CREATE EXTENSION IF NOT EXISTS vector;
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from core.config import env_bool, env_int
from core.secrets_loader import load_secret_files
from rag.embedding_config import embedding_dim

load_dotenv()
load_secret_files()

# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mindgate_db")

# Per-PROCESS pool sizing.  The effective ceiling against Postgres is
# (DB_POOL_SIZE + DB_MAX_OVERFLOW) × number of processes — every Gunicorn API
# worker plus the ingestion worker holds its own pool.  The defaults
# (5 + 10 = 15 per process; 5 processes ≈ 75 connections) stay inside a stock
# Postgres max_connections=100.  Raise these only together with the server's
# max_connections, or put PgBouncer in front.
DB_POOL_SIZE = env_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = env_int("DB_MAX_OVERFLOW", 10)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=30,       # Wait up to 30s for a free connection
    pool_recycle=1800,     # Recycle connections every 30 min (avoid stale)
    # Kill runaway queries (e.g. pgvector ANN search under load) before they
    # exhaust the connection pool.  30 s is generous; tunable via env var.
    connect_args={
        "options": f"-c statement_timeout={int(os.getenv('DB_STATEMENT_TIMEOUT_MS', '30000'))}"
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# ---------------------------------------------------------------------------
# Cross-process coordination
# ---------------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402  (kept near its only use)


@contextmanager
def pg_try_advisory_lock(key: int):
    """Yield True while holding a Postgres session advisory lock, False if
    another process already holds it.

    Used to elect a single process for singleton work (startup recovery,
    periodic maintenance, retention) across Gunicorn workers.  Fails OPEN:
    if the database is unreachable the caller proceeds as if it won the
    election — the guarded work is idempotent/SKIP LOCKED-safe and will
    surface its own connectivity errors.
    """
    from sqlalchemy import text

    try:
        conn = engine.connect()
    except Exception:
        yield True
        return
    acquired = False
    try:
        acquired = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    """Registered user account."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(150), unique=True, nullable=True, index=True)  # portal display login; NULL for legacy email-only accounts
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False, server_default="false")
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    mfa_enabled = Column(Boolean, default=False, nullable=False, server_default="false")
    totp_secret = Column(String(64), nullable=True)  # Base32-encoded TOTP secret; NULL = MFA not configured
    visibility_tier = Column(String(50), nullable=False, default="admin", server_default="admin")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    feedback = relationship("MessageFeedback", back_populates="user", cascade="all, delete-orphan")


class LoginFailure(Base):
    """One failed login attempt, keyed by (normalized) email.

    Backs the account-lockout policy: N failures inside the window lock the
    account for the remainder of the window.  Stored in Postgres — not Redis
    or process memory — so the lockout is shared across workers and survives
    Redis outages instead of failing open.  Rows are short-lived; the daily
    retention job purges anything older than a day.
    """
    __tablename__ = "login_failures"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_login_failures_email_created", "email", "created_at"),
    )


class ChatSession(Base):
    """
    A chat session groups multiple back-and-forth messages.
    Session ID is generated client-side as a UUID.
    """
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="sessions")
    messages = relationship(
        "ChatMessage",
        back_populates="session",
        order_by="ChatMessage.created_at",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_chat_sessions_user_created", "user_id", "created_at"),
    )


class ChatMessage(Base):
    """A single message (user or assistant) within a chat session."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)      # "user" | "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    session = relationship("ChatSession", back_populates="messages")
    feedback = relationship(
        "MessageFeedback",
        back_populates="message",
        cascade="all, delete-orphan",
        uselist=False,
    )

    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
        Index("ix_chat_messages_role_created", "role", "created_at"),
    )


class MessageFeedback(Base):
    """A user's rating and optional explanation for an assistant response."""
    __tablename__ = "message_feedback"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating = Column(String(20), nullable=False)
    reason = Column(String(50), nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    message = relationship("ChatMessage", back_populates="feedback")
    user = relationship("User", back_populates="feedback")

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_feedback_message"),
        Index("ix_message_feedback_rating_created", "rating", "created_at"),
        Index("ix_message_feedback_user_created", "user_id", "created_at"),
    )


class VoiceNote(Base):
    """
    A voice note (transcript + title) saved to the knowledge base by an admin.
    """
    __tablename__ = "voice_notes"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    admin = relationship("User", foreign_keys=[admin_id])

    __table_args__ = (
        Index("ix_voice_notes_admin_created", "admin_id", "created_at"),
    )


class AuditLog(Base):
    """
    An audit log entry recording admin actions (upload, delete, voice note save, etc.).
    
    Hash-chained and append-only through ORM hooks plus database triggers.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(100), nullable=False)   # e.g. "upload_pdf", "delete_pdf", "save_voice_note"
    target = Column(String(512), nullable=True)    # e.g. filename or note title
    detail = Column(Text, nullable=True)           # JSON or human-readable detail
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    admin = relationship("User", foreign_keys=[admin_id])

    __table_args__ = (
        Index("ix_audit_logs_admin_created", "admin_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )


class DocumentChunk(Base):
    """
    A text chunk from an ingested PDF with its pgvector embedding.

    The `embedding` column uses pgvector's VECTOR type.
    Dimension defaults to 768 and is configurable via A2A_EMBEDDING_DIM.
    """
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("received_documents.id", ondelete="SET NULL"), nullable=True)
    source_file = Column(String(512), nullable=False)   # e.g. "policy.pdf"
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)

    # Configurable dimension for Gemini's Matryoshka embedding outputs.
    _EMBEDDING_DIM = embedding_dim()
    embedding = Column(Vector(_EMBEDDING_DIM), nullable=False)

    # Parent-child chunking: children (these rows) are embedded for precise
    # retrieval; parent_text carries the surrounding passage handed to the LLM.
    # NULL on legacy/single chunks — retrieval falls back to `text`.
    # (The BM25 `text_search` tsvector companion column is Postgres-generated
    # and deliberately unmapped so SQLite-based tests can still create_all.)
    parent_key = Column(String(150), nullable=True)
    parent_text = Column(Text, nullable=True)

    source = Column(String(100), nullable=False, default="admin_upload", server_default="admin_upload")
    visibility_tier = Column(String(50), nullable=False, default="admin", server_default="admin")
    page_number = Column(Integer, nullable=True)
    content_type = Column(String(100), nullable=True)
    source_doc = Column(String(512), nullable=True)
    ticket_id = Column(String(100), nullable=True)
    metadata_json = Column(Text, nullable=True)
    change_id = Column(String(100), nullable=True)
    citations_json = Column(Text, nullable=True)  # grounded_in[] citation metadata from NPCI
    provisional = Column(Boolean, default=False, server_default="false")  # True = amends_change response, not yet authoritative
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    document = relationship("ReceivedDocument", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("source_file", "chunk_index", name="uq_chunk_source_idx"),
        Index("ix_document_chunks_source_file", "source_file"),
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_ticket_id", "ticket_id"),
        Index("ix_document_chunks_change_id", "change_id"),
        # IVFFlat index for fast cosine similarity — created separately below
    )


class DocumentSummary(Base):
    """Stored summary for a document received or indexed by the RAG system."""
    __tablename__ = "document_summaries"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("received_documents.id", ondelete="SET NULL"), nullable=True)
    filename = Column(String(512), nullable=False)
    source = Column(String(100), nullable=False)
    visibility_tier = Column(String(50), nullable=False, default="admin", server_default="admin")
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    document = relationship("ReceivedDocument", back_populates="summary")


class ReceivedDocument(Base):
    """Original A2A document plus idempotency and ingestion lifecycle metadata."""
    __tablename__ = "received_documents"

    id = Column(Integer, primary_key=True)
    filename = Column(String(512), nullable=False)
    stored_path = Column(String(1024), nullable=False)
    content_hash = Column(String(64), unique=True, nullable=False)
    source = Column(String(100), nullable=False, default="npci_agent", server_default="npci_agent")
    file_type = Column(String(50), nullable=False)
    visibility_tier = Column(String(50), nullable=False, default="admin", server_default="admin")
    status = Column(String(50), nullable=False, default="queued", server_default="queued")
    metadata_json = Column(Text, nullable=True)
    change_id = Column(String(100), nullable=True)  # Links doc to an NPCI change
    doc_type = Column(String(50), nullable=True)     # DocTypeEnum value from product_kit[]
    version = Column(Integer, nullable=True)         # change_communication version
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    chunks = relationship("DocumentChunk", back_populates="document")
    summary = relationship("DocumentSummary", back_populates="document", uselist=False)
    jobs = relationship("A2AIngestionJob", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_received_documents_created", "created_at"),
        Index("ix_received_documents_status_created", "status", "created_at"),
        Index("ix_received_documents_hash", "content_hash"),
        Index("ix_received_documents_change_id", "change_id"),
    )


class A2AIngestionJob(Base):
    """Async ingestion job for a received A2A document."""
    __tablename__ = "a2a_ingestion_jobs"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("received_documents.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), nullable=False, default="queued", server_default="queued")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    document = relationship("ReceivedDocument", back_populates="jobs")

    __table_args__ = (
        Index("ix_a2a_ingestion_jobs_document_created", "document_id", "created_at"),
        Index("ix_a2a_ingestion_jobs_status_created", "status", "created_at"),
    )


class NpciTicket(Base):
    """NPCI escalation ticket tracked by the Mindgate A2A workflow.

    Extended with spec-required fields: change_id (scoping query to a change),
    query_id, correlation_id, agent_run_id, clarification_response fields
    (interpretation_confirmed, grounded_in, amends_change, etc.), and
    provisional flag.
    """
    __tablename__ = "npci_tickets"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(String(100), unique=True, nullable=False)
    query = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="pending", server_default="pending")
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Portal user who raised the escalation — scopes dashboard visibility so
    # one user's escalations (and NPCI's answers) never appear on another
    # user's feed. Null for system/legacy tickets (falls back to change owner).
    raised_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ── Spec §Appendix B — query/clarification_response fields ────────────
    change_id = Column(String(100), nullable=True)
    query_id = Column(String(100), nullable=True)
    correlation_id = Column(String(100), nullable=True)
    agent_run_id = Column(String(100), nullable=True)
    clause_ref = Column(String(512), nullable=True)
    phase = Column(String(100), nullable=True)
    bank_interpretation = Column(Text, nullable=True)
    priority = Column(String(50), nullable=True)

    # ── clarification_response fields (NPCI → Bank) ──────────────────────
    interpretation_confirmed = Column(Boolean, nullable=True)
    grounded_in = Column(Text, nullable=True)          # JSON array of citation refs
    amends_change = Column(Boolean, nullable=True)
    amendment_ref = Column(String(100), nullable=True)
    approver = Column(String(255), nullable=True)
    generation_mode = Column(String(50), nullable=True)
    provisional = Column(Boolean, default=False, server_default="false")

    __table_args__ = (
        Index("ix_npci_tickets_change_id", "change_id"),
        Index("ix_npci_tickets_status_created", "status", "created_at"),
    )


class A2AMessage(Base):
    """Audit log for every A2A message — accepted AND rejected (spec §10).

    One row per inbound or outbound message. Rejections are logged with
    their error_code so compliance/SOC can pattern-match on failures.
    
    Hash-chained and append-only through ORM hooks plus database triggers.
    """
    __tablename__ = "a2a_messages"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(100), unique=True, nullable=False)  # Idempotency key
    protocol_version = Column(String(10), nullable=True, default="1.0")
    task_type = Column(String(100), nullable=False)
    direction = Column(String(10), nullable=False)  # "inbound" | "outbound"
    change_id = Column(String(100), nullable=True)
    cflow_id = Column(String(100), nullable=True)
    agent_id = Column(String(100), nullable=True)
    agent_run_id = Column(String(100), nullable=True)
    correlation_id = Column(String(100), nullable=True)
    caller_ip = Column(String(45), nullable=True)
    jwt_sub = Column(String(255), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error_code = Column(String(100), nullable=True)  # NULL = success; else A2AErrorCode value
    payload_ref = Column(Text, nullable=True)  # JSON of payload (or reference)
    result_payload = Column(Text, nullable=True)  # Cached result for idempotency
    http_status = Column(Integer, nullable=True)
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_a2a_messages_message_id", "message_id"),
        Index("ix_a2a_messages_change_id_created", "change_id", "created_at"),
        Index("ix_a2a_messages_cflow_id_created", "cflow_id", "created_at"),
        Index("ix_a2a_messages_task_type_created", "task_type", "created_at"),
        Index("ix_a2a_messages_error_code", "error_code"),
    )


class A2AIdempotencyKey(Base):
    """Transactional idempotency claim for inbound A2A processing.

    The append-only A2A audit table records evidence after processing. This
    mutable table is the pre-processing concurrency gate that prevents two
    identical transport or business-key requests from executing side effects.
    """
    __tablename__ = "a2a_idempotency_keys"

    id = Column(Integer, primary_key=True)
    key_type = Column(String(50), nullable=False)
    key_value = Column(String(512), nullable=False)
    message_id = Column(String(100), nullable=True)
    task_type = Column(String(100), nullable=True)
    status = Column(String(50), nullable=False, default="processing", server_default="processing")
    result_payload = Column(Text, nullable=True)
    error_code = Column(String(100), nullable=True)
    http_status = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("key_type", "key_value", name="uq_a2a_idempotency_key"),
        Index("ix_a2a_idempotency_status_updated", "status", "updated_at"),
        Index("ix_a2a_idempotency_message_id", "message_id"),
    )


class A2AChange(Base):
    """Tracks the lifecycle state of an NPCI change (spec §6 change lifecycle)."""
    __tablename__ = "a2a_changes"

    id = Column(Integer, primary_key=True)
    change_id = Column(String(100), unique=True, nullable=False)
    current_state = Column(String(50), nullable=False, default="ASSIGNED")
    version = Column(Integer, nullable=True)
    partner_id = Column(String(100), nullable=True)
    title = Column(String(512), nullable=True)  # from change_communication.title, for the dashboard
    metadata_json = Column(Text, nullable=True)
    # Concerned bank product manager for this change. NPCI updates (counter
    # proposal decisions, blocker resolutions, milestone acks, ticket answers)
    # for this change are surfaced ONLY to this user's portal dashboard. Set by
    # admin assignment or auto-claimed when a portal user first acts on the
    # change via /a2a/internal.
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    blocked_at = Column(DateTime, nullable=True)       # spec: assignment.blocked_at
    blocked_reason = Column(Text, nullable=True)       # spec: assignment.blocked_reason
    # Soft delete: an admin can remove a change from the board, dashboards, and
    # the Assistant change picker without destroying its audit trail.
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", foreign_keys=[owner_user_id])

    __table_args__ = (
        Index("ix_a2a_changes_change_id", "change_id"),
        Index("ix_a2a_changes_state", "current_state"),
        Index("ix_a2a_changes_owner_user_id", "owner_user_id"),
    )


class HiddenUpdate(Base):
    """An update item an admin has hidden from users' portal dashboards.

    Identified by (kind, ref) matching the update feed items produced by the
    portal dashboard — e.g. kind='document' ref='<doc id>', kind='counter_proposal'
    ref='<counter_proposal_id>'. Hidden items are filtered out of what non-admin
    users see; admins still see them (flagged) so they can restore.
    """
    __tablename__ = "hidden_updates"

    id = Column(Integer, primary_key=True)
    kind = Column(String(50), nullable=False)
    ref = Column(String(200), nullable=False)
    hidden_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("kind", "ref", name="uq_hidden_update_kind_ref"),
    )


class A2ACounterProposal(Base):
    """A Bank→NPCI counter_proposal and NPCI's counter_decision (spec §Appendix B)."""
    __tablename__ = "a2a_counter_proposals"

    id = Column(Integer, primary_key=True)
    counter_proposal_id = Column(String(100), unique=True, nullable=False)
    change_id = Column(String(100), nullable=False)
    version_targeted = Column(Integer, nullable=True)
    status = Column(String(50), nullable=False, default="open", server_default="open")  # open | decided
    counters_json = Column(Text, nullable=True)   # the proposed counters[]
    decision_json = Column(Text, nullable=True)    # NPCI per_counter[] decision
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_a2a_counter_proposals_change", "change_id"),
        Index("ix_a2a_counter_proposals_status", "status"),
    )


class A2ABlocker(Base):
    """A Bank→NPCI blocker plus NPCI status updates / resolution (spec §Appendix B)."""
    __tablename__ = "a2a_blockers"

    id = Column(Integer, primary_key=True)
    blocker_id = Column(String(100), unique=True, nullable=False)
    change_id = Column(String(100), nullable=False)
    type = Column(String(100), nullable=True)
    severity = Column(String(50), nullable=True)
    subject = Column(String(512), nullable=True)
    status = Column(String(50), nullable=False, default="open", server_default="open")  # open | <npci status> | resolved | wontfix | deferred
    resolution = Column(String(50), nullable=True)  # resolved | wontfix | deferred
    resolution_text = Column(Text, nullable=True)
    crm_json = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)  # original blocker payload
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_a2a_blockers_change", "change_id"),
        Index("ix_a2a_blockers_status", "status"),
    )


class A2AMilestone(Base):
    """Latest per-milestone state for a change — the milestone_status_report source."""
    __tablename__ = "a2a_milestones"

    id = Column(Integer, primary_key=True)
    change_id = Column(String(100), nullable=False)
    milestone = Column(String(50), nullable=False)  # design | coding | testing
    state = Column(String(50), nullable=False)      # in_progress | completed | at_risk | ...
    version_implementing = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    eta = Column(String(50), nullable=True)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("change_id", "milestone", name="uq_a2a_milestone_change_ms"),
        Index("ix_a2a_milestones_change", "change_id"),
    )


class AppSetting(Base):
    """Runtime-editable configuration surfaced in the portal Settings page.

    Values here override environment defaults via the settings resolver so an
    admin can reconfigure the NPCI connection etc. without a redeploy. Secrets
    are marked is_secret and never echoed back to the UI in plaintext.
    """
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    category = Column(String(50), nullable=False, default="general", server_default="general")
    is_secret = Column(Boolean, default=False, nullable=False, server_default="false")
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_app_settings_category", "category"),
    )


class A2ACflow(Base):
    """Tracks the lifecycle state of a certification flow (spec §6 cert lifecycle)."""
    __tablename__ = "a2a_cflows"

    id = Column(Integer, primary_key=True)
    cflow_id = Column(String(100), unique=True, nullable=False)
    change_id = Column(String(100), nullable=True)  # FK conceptually to a2a_changes
    current_state = Column(String(50), nullable=False, default="NOT_STARTED")
    cert_attempt = Column(Integer, nullable=True)
    partner_id = Column(String(100), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_a2a_cflows_cflow_id", "cflow_id"),
        Index("ix_a2a_cflows_change_id", "change_id"),
        Index("ix_a2a_cflows_state", "current_state"),
    )


class A2ANonce(Base):
    """HMAC nonce store for replay detection (spec §4.1).

    PostgreSQL-backed fallback when Redis is not available.
    Nonces expire after A2A_NONCE_TTL_SECONDS (default: 600s).
    """
    __tablename__ = "a2a_nonces"

    nonce = Column(String(64), primary_key=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WebSocketTicket(Base):
    """Short-lived, single-use ticket for browser WebSocket authentication."""
    __tablename__ = "ws_tickets"

    token_hash = Column(String(64), primary_key=True)
    ticket_id = Column(String(100), nullable=False, index=True)
    admin_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    admin = relationship("User", foreign_keys=[admin_id])

    __table_args__ = (
        Index("ix_ws_tickets_ticket_expires", "ticket_id", "expires_at"),
    )


class RevokedSession(Base):
    """JWT session IDs (jti values) that must no longer authenticate."""
    __tablename__ = "revoked_sessions"

    id = Column(Integer, primary_key=True)
    jti = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason = Column(String(100), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_revoked_sessions_expires", "expires_at"),
    )


def _audit_chain_payload(target, fields: list[str]) -> str:
    payload = {}
    for field in fields:
        value = getattr(target, field, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        payload[field] = value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _apply_hash_chain(connection, target, table_name: str, fields: list[str]) -> None:
    from sqlalchemy import text

    if getattr(target, "created_at", None) is None:
        target.created_at = datetime.now(timezone.utc)

    try:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
            {"lock_name": f"{table_name}_hash_chain"},
        )
    except Exception:
        pass

    prev_hash = connection.execute(
        text(f"SELECT entry_hash FROM {table_name} WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1")
    ).scalar()
    target.prev_hash = prev_hash or "GENESIS"
    canonical = _audit_chain_payload(target, fields)
    target.entry_hash = hashlib.sha256(
        f"{target.prev_hash}.{canonical}".encode()
    ).hexdigest()


def _reject_audit_mutation(*_args, **_kwargs) -> None:
    raise ValueError("Audit records are append-only and cannot be updated or deleted.")


@event.listens_for(AuditLog, "before_insert")
def _hash_audit_log(_mapper, connection, target) -> None:
    _apply_hash_chain(
        connection,
        target,
        "audit_logs",
        ["admin_id", "action", "target", "detail", "created_at"],
    )


@event.listens_for(A2AMessage, "before_insert")
def _hash_a2a_message(_mapper, connection, target) -> None:
    _apply_hash_chain(
        connection,
        target,
        "a2a_messages",
        [
            "message_id",
            "protocol_version",
            "task_type",
            "direction",
            "change_id",
            "cflow_id",
            "agent_id",
            "agent_run_id",
            "correlation_id",
            "caller_ip",
            "jwt_sub",
            "latency_ms",
            "error_code",
            "payload_ref",
            "result_payload",
            "http_status",
            "created_at",
        ],
    )


event.listen(AuditLog, "before_update", _reject_audit_mutation)
event.listen(AuditLog, "before_delete", _reject_audit_mutation)
event.listen(A2AMessage, "before_update", _reject_audit_mutation)
event.listen(A2AMessage, "before_delete", _reject_audit_mutation)


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Create all tables and the pgvector extension if not already present.
    Safe to call multiple times — uses IF NOT EXISTS semantics.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    Base.metadata.create_all(bind=engine)

    # Keep non-production fresh databases performant without running legacy
    # ALTER-based migrations. Production still uses Alembic for this.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))
        # Hybrid retrieval (mirrors Alembic 20260710_0012): parent-child
        # columns come from create_all; the generated tsvector column cannot
        # be declared in the ORM (SQLite tests), so add it here for dev DBs.
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS text_search tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_text_search "
            "ON document_chunks USING gin (text_search)"
        ))
        # Pre-existing dev databases created before parent-child chunking.
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS parent_key VARCHAR(150)"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS parent_text TEXT"
        ))

    if env_bool("ALLOW_LEGACY_INLINE_MIGRATIONS", default=False):
        _apply_legacy_inline_migrations()


def _apply_legacy_inline_migrations() -> None:
    """Legacy in-place ALTER/CREATE migrations for pre-Alembic dev databases.

    Dead by default — gated behind ALLOW_LEGACY_INLINE_MIGRATIONS and retained
    only as an escape hatch for upgrading an old local database in place.
    Production schema state is owned exclusively by Alembic (see
    _assert_database_migrated in main.py), so this path never runs there.
    """
    from sqlalchemy import text

    # Migrate existing tables: add is_admin column if it doesn't exist yet.
    # This is safe to run repeatedly (IF NOT EXISTS semantics).
    with engine.begin() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        except Exception:
            pass  # Postgres < 9.6 fallback — column may already exist
        conn.execute(text(
            "ALTER TABLE users "
            "ADD COLUMN IF NOT EXISTS visibility_tier VARCHAR(50) NOT NULL DEFAULT 'admin'"
        ))

        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS source VARCHAR(100) NOT NULL DEFAULT 'admin_upload'"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS visibility_tier VARCHAR(50) NOT NULL DEFAULT 'admin'"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS document_id INTEGER REFERENCES received_documents(id) ON DELETE SET NULL"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS page_number INTEGER"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS content_type VARCHAR(100)"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS source_doc VARCHAR(512)"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS ticket_id VARCHAR(100)"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS metadata_json TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE document_summaries "
            "ADD COLUMN IF NOT EXISTS document_id INTEGER REFERENCES received_documents(id) ON DELETE SET NULL"
        ))
        conn.execute(text(
            "ALTER TABLE document_summaries "
            "ADD COLUMN IF NOT EXISTS visibility_tier VARCHAR(50) NOT NULL DEFAULT 'admin'"
        ))
        conn.execute(text(
            "ALTER TABLE document_summaries "
            "DROP CONSTRAINT IF EXISTS document_summaries_filename_key"
        ))
        conn.execute(text(
            "ALTER TABLE npci_tickets "
            "ALTER COLUMN status SET DEFAULT 'pending'"
        ))

        # ── npci_tickets: add spec-required columns ───────────────────────
        for col_def in [
            "change_id VARCHAR(100)",
            "query_id VARCHAR(100)",
            "correlation_id VARCHAR(100)",
            "agent_run_id VARCHAR(100)",
            "clause_ref VARCHAR(512)",
            "phase VARCHAR(100)",
            "bank_interpretation TEXT",
            "priority VARCHAR(50)",
            "interpretation_confirmed BOOLEAN",
            "grounded_in TEXT",
            "amends_change BOOLEAN",
            "amendment_ref VARCHAR(100)",
            "approver VARCHAR(255)",
            "generation_mode VARCHAR(50)",
            "provisional BOOLEAN DEFAULT FALSE",
            "raised_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        ]:
            conn.execute(text(
                f"ALTER TABLE npci_tickets ADD COLUMN IF NOT EXISTS {col_def}"
            ))

        # ── document_chunks: add change_id, citations, provisional ────────
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS change_id VARCHAR(100)"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS citations_json TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS provisional BOOLEAN DEFAULT FALSE"
        ))

        # ── received_documents: add change_id, doc_type, version ──────────
        conn.execute(text(
            "ALTER TABLE received_documents "
            "ADD COLUMN IF NOT EXISTS change_id VARCHAR(100)"
        ))
        conn.execute(text(
            "ALTER TABLE received_documents "
            "ADD COLUMN IF NOT EXISTS visibility_tier VARCHAR(50) NOT NULL DEFAULT 'admin'"
        ))
        conn.execute(text(
            "ALTER TABLE received_documents "
            "ADD COLUMN IF NOT EXISTS doc_type VARCHAR(50)"
        ))
        conn.execute(text(
            "ALTER TABLE received_documents "
            "ADD COLUMN IF NOT EXISTS version INTEGER"
        ))

        # create_all() does not add newly declared indexes to existing tables.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_created "
            "ON chat_sessions (user_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_created "
            "ON chat_messages (session_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_role_created "
            "ON chat_messages (role, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_source_file "
            "ON document_chunks (source_file)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_document_id "
            "ON document_chunks (document_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_ticket_id "
            "ON document_chunks (ticket_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_change_id "
            "ON document_chunks (change_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_received_documents_status_created "
            "ON received_documents (status, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_received_documents_hash "
            "ON received_documents (content_hash)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_received_documents_change_id "
            "ON received_documents (change_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_a2a_ingestion_jobs_status_created "
            "ON a2a_ingestion_jobs (status, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_message_feedback_rating_created "
            "ON message_feedback (rating, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_message_feedback_user_created "
            "ON message_feedback (user_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_npci_tickets_change_id "
            "ON npci_tickets (change_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_npci_tickets_status_created "
            "ON npci_tickets (status, created_at)"
        ))
        # Voice notes & audit log migrations
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS voice_notes ("
            "  id SERIAL PRIMARY KEY,"
            "  admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,"
            "  title VARCHAR(512) NOT NULL,"
            "  content TEXT NOT NULL,"
            "  created_at TIMESTAMP DEFAULT NOW()"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS audit_logs ("
            "  id SERIAL PRIMARY KEY,"
            "  admin_id INTEGER REFERENCES users(id) ON DELETE SET NULL,"
            "  action VARCHAR(100) NOT NULL,"
            "  target VARCHAR(512),"
            "  detail TEXT,"
            "  created_at TIMESTAMP DEFAULT NOW()"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_voice_notes_admin_created "
            "ON voice_notes (admin_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created "
            "ON audit_logs (action, created_at)"
        ))
        conn.execute(text(
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS prev_hash VARCHAR(64)"
        ))
        conn.execute(text(
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS entry_hash VARCHAR(64)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_entry_hash "
            "ON audit_logs (entry_hash)"
        ))

        # ── A2A protocol tables (spec §10, §6) ────────────────────────────
        # Nonce expiry cleanup has been moved to a periodic background task in main.py.
        conn.execute(text(
            "ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS prev_hash VARCHAR(64)"
        ))
        conn.execute(text(
            "ALTER TABLE a2a_messages ADD COLUMN IF NOT EXISTS entry_hash VARCHAR(64)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_a2a_messages_entry_hash "
            "ON a2a_messages (entry_hash)"
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ws_tickets (
              token_hash VARCHAR(64) PRIMARY KEY,
              ticket_id VARCHAR(100) NOT NULL,
              admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at TIMESTAMP NOT NULL,
              used_at TIMESTAMP,
              created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ws_tickets_ticket_expires "
            "ON ws_tickets (ticket_id, expires_at)"
        ))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION prevent_audit_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'audit table is append-only';
            END;
            $$ LANGUAGE plpgsql
        """))
        for table_name in ("audit_logs", "a2a_messages"):
            conn.execute(text(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}"
            ))
            conn.execute(text(
                f"CREATE TRIGGER trg_{table_name}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table_name} "
                f"FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()"
            ))

    # Create an HNSW index for fast approximate cosine similarity search.
    # HNSW doesn't require pre-training on existing rows like IVFFlat.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))


# ---------------------------------------------------------------------------
# Dependency — FastAPI DB session
# ---------------------------------------------------------------------------

def get_db():
    """FastAPI dependency that yields a SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
