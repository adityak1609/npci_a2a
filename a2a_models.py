"""
a2a_models.py — Pydantic models derived from the NPCI Unified AgentNxt A2A
Protocol Specification v1.0.

Covers:
    - Common envelope (spec §4)
    - All 28 message types + echo (spec §6)
    - Fixed doc_type enum for change_communication.product_kit[] (spec §6 Part A)
    - Query and clarification_response payloads (spec §Appendix B)
    - Change and Certification lifecycle state enums (spec §6)
    - Spec error codes (spec §4.1)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# §6 — 28 defined message types + echo
# ---------------------------------------------------------------------------

class A2ATaskType(StrEnum):
    """All task_type values defined in the NPCI A2A spec §6."""

    # ── Change lifecycle (Phase C) ─────────────────────────────────────────
    CHANGE_COMMUNICATION = "change_communication"
    PROPOSAL_ACKNOWLEDGED = "proposal_acknowledged"
    COUNTER_PROPOSAL = "counter_proposal"
    COUNTER_PROPOSAL_RESPONSE = "counter_proposal_response"
    DESIGN_SUBMISSION = "design_submission"
    DESIGN_REVIEW = "design_review"
    CODING_COMPLETE = "coding_complete"
    TEST_RESULTS = "test_results"
    QUERY = "query"
    CLARIFICATION_RESPONSE = "clarification_response"
    CHANGE_ACCEPTED = "change_accepted"
    CHANGE_REJECTED = "change_rejected"
    CHANGE_ABORTED = "change_aborted"

    # ── Certification lifecycle ────────────────────────────────────────────
    CERT_CONFIG_REQUEST = "cert_config_request"
    CERT_CONFIG_RESPONSE = "cert_config_response"
    CERT_SETUP_COMPLETE = "cert_setup_complete"
    CERT_CASE_RESULT = "cert_case_result"
    CERT_CASE_TRIAGE = "cert_case_triage"
    CERT_FIX_SUBMITTED = "cert_fix_submitted"
    CERT_WAIVER_REQUEST = "cert_waiver_request"
    CERT_WAIVER_RESPONSE = "cert_waiver_response"
    CERT_DISPUTE_RAISED = "cert_dispute_raised"
    CERT_DISPUTE_RESOLUTION = "cert_dispute_resolution"
    CERT_COMPLETED = "cert_completed"
    CERT_ABORTED = "cert_aborted"

    # ── Cross-cutting ─────────────────────────────────────────────────────
    STATUS_REQUEST = "status_request"
    STATUS_RESPONSE = "status_response"
    NOTIFICATION = "notification"

    # ── Connectivity test ─────────────────────────────────────────────────
    ECHO = "echo"


# ---------------------------------------------------------------------------
# §6 Part A — doc_type enum for change_communication.product_kit[]
# ---------------------------------------------------------------------------

class DocTypeEnum(StrEnum):
    """Fixed document type values for product_kit[] entries (spec §6 Part A)."""

    BRD = "brd"
    TECH_SPEC = "tech_spec"
    CERT_TEST_CASES = "cert_test_cases"
    CIRCULAR = "circular"
    FAQ = "faq"
    MANIFEST = "manifest"
    PRODUCT_DECK = "product_deck"
    EXPLAINER_VIDEO = "explainer_video"
    PRODUCT_NOTE = "product_note"
    PROMO_VIDEO = "promo_video"


# ---------------------------------------------------------------------------
# §6 — Change lifecycle states
# ---------------------------------------------------------------------------

class ChangeLifecycleState(StrEnum):
    """States in the change lifecycle state machine (spec §6)."""

    ASSIGNED = "ASSIGNED"
    COMMUNICATED = "COMMUNICATED"
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    DESIGN_COMPLETED = "DESIGN_COMPLETED"
    CODING_COMPLETED = "CODING_COMPLETED"
    TESTED = "TESTED"
    READY_FOR_CERTIFICATION = "READY_FOR_CERTIFICATION"
    CERTIFYING = "CERTIFYING"
    CERTIFIED = "CERTIFIED"
    COUNTER_PROPOSAL = "COUNTER_PROPOSAL"
    ABORTED = "ABORTED"


# ---------------------------------------------------------------------------
# §6 — Certification lifecycle states
# ---------------------------------------------------------------------------

class CertLifecycleState(StrEnum):
    """States in the certification lifecycle state machine (spec §6)."""

    NOT_STARTED = "NOT_STARTED"
    CONFIG_REQUESTED = "CONFIG_REQUESTED"
    CONFIG_RECEIVED = "CONFIG_RECEIVED"
    SETUP = "SETUP"
    RUNNING = "RUNNING"
    TRIAGE_PENDING = "TRIAGE_PENDING"
    FIX_PENDING = "FIX_PENDING"
    WAIVER_PENDING = "WAIVER_PENDING"
    DISPUTE_PENDING = "DISPUTE_PENDING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


# ---------------------------------------------------------------------------
# §4.1 — Spec error codes
# ---------------------------------------------------------------------------

class A2AErrorCode(StrEnum):
    """Error code strings returned in response bodies per spec §4.1.

    Pattern-matchable by NPCI SOC tooling and our own monitoring.
    """

    # Layer 1: mTLS
    MTLS_REQUIRED = "mtls_required"
    MTLS_FINGERPRINT_MISMATCH = "mtls_fingerprint_mismatch"

    # Layer 2: CIDR
    IP_NOT_ALLOWED = "ip_not_allowed"

    # Layer 3: HMAC
    SIGNATURE_MISMATCH = "signature_mismatch"
    TIMESTAMP_SKEW = "timestamp_skew"
    REPLAY_DETECTED = "replay_detected"
    MISSING_ENVELOPE_HEADERS = "missing_envelope_headers"

    # Layer 4: JWT / session
    INVALID_TOKEN = "invalid_token"
    SESSION_REVOKED = "session_revoked"
    SESSION_EXPIRED = "session_expired"

    # Layer 5: Partner status
    PARTNER_INACTIVE = "partner_inactive"

    # Layer 6: Ownership
    PARTNER_MISMATCH = "partner_mismatch"
    UNKNOWN_ID = "unknown_id"

    # Layer 7: State machine
    INVALID_STATE_TRANSITION = "invalid_state_transition"

    # Layer 8: Agent identity
    UNKNOWN_AGENT = "unknown_agent"
    AGENT_NOT_AUTHORIZED_FOR_TASK = "agent_not_authorized_for_task"


# ---------------------------------------------------------------------------
# §4 — Common A2A Envelope
# ---------------------------------------------------------------------------

class A2AEnvelope(BaseModel):
    """Common envelope wrapping every inbound/outbound A2A message (spec §4).

    The ``from_`` field maps to the wire-level ``"from"`` key.
    ``message_id`` is the transport-level idempotency key — dedup on this.
    ``protocol_version`` is always ``"1.0"`` for the current spec.
    """

    task_type: str
    change_id: Optional[str] = None
    cflow_id: Optional[str] = None
    cert_attempt: Optional[int] = None
    from_: str = Field(..., alias="from")
    agent_id: str
    agent_run_id: str
    correlation_id: str
    timestamp: str  # ISO 8601 UTC
    payload: dict[str, Any]
    protocol_version: str = "1.0"
    message_id: str  # Transport-level idempotency key

    model_config = {"populate_by_name": True}

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the wire format (``from`` instead of ``from_``)."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        return data


# ---------------------------------------------------------------------------
# §6 Part A — product_kit[] entry within change_communication
# ---------------------------------------------------------------------------

class ProductKitEntry(BaseModel):
    """A single document entry inside change_communication.product_kit[]."""

    doc_type: DocTypeEnum
    filename: str
    content_b64: str  # Base64-encoded document content
    version: Optional[int] = None
    description: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ChangeCommunicationPayload(BaseModel):
    """Payload schema for the ``change_communication`` task_type (spec §6 Part A)."""

    change_id: str
    version: int
    title: Optional[str] = None
    description: Optional[str] = None
    product_kit: list[ProductKitEntry]
    notify_list: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# §Appendix B — query payload (Bank → NPCI)
# ---------------------------------------------------------------------------

class QueryPayload(BaseModel):
    """Payload for the ``query`` task_type (spec §Appendix B).

    A query is a clarification on a *specific change* under negotiation —
    NOT a generic "chatbot didn't know this" hook.
    """

    change_id: str
    query_id: Optional[str] = None  # Assigned by the sender; used as business idempotency key
    clause_ref: Optional[str] = None  # e.g., "TSD §4.2.3"
    phase: Optional[str] = None  # Current lifecycle phase of the change
    bank_interpretation: str  # Bank's reading of the clause in question
    priority: str = "normal"  # "normal" | "high" | "critical"
    query_text: str


# ---------------------------------------------------------------------------
# §Appendix B — clarification_response payload (NPCI → Bank)
# ---------------------------------------------------------------------------

class ClarificationResponsePayload(BaseModel):
    """Payload for the ``clarification_response`` task_type (spec §Appendix B).

    Contains critical fields that must NOT be dropped:
    - interpretation_confirmed: whether NPCI agrees with the bank's reading
    - grounded_in[]: citation references for the response
    - amends_change: if True, the response is provisional and the authoritative
      update will arrive later as change_communication v+1
    """

    change_id: str
    query_id: Optional[str] = None
    ticket_id: Optional[str] = None
    response_text: str

    interpretation_confirmed: bool = False
    grounded_in: Optional[list[str]] = None  # Citation references, e.g. ["TSD §4.2.3", "BRD §2.1"]
    amends_change: bool = False
    amendment_ref: Optional[str] = None  # Reference to the amendment if amends_change=True
    approver: Optional[str] = None  # Who approved this clarification
    generation_mode: Optional[str] = None  # "manual" | "ai_assisted" | "auto"


# ---------------------------------------------------------------------------
# proposal_acknowledged payload (Bank → NPCI, auto-fired)
# ---------------------------------------------------------------------------

class ProposalAcknowledgedPayload(BaseModel):
    """Payload for ``proposal_acknowledged`` (auto-fired on change_communication receipt).

    Idempotent on (change_id, version_received).
    """

    change_id: str
    version_received: int
    acknowledged_by: str = "mindgate_a2a_agent"


# ---------------------------------------------------------------------------
# echo payload (connectivity test)
# ---------------------------------------------------------------------------

class EchoPayload(BaseModel):
    """Payload for the ``echo`` task_type (connectivity test)."""

    ping: Optional[str] = "ping"


class EchoResponse(BaseModel):
    """Response for the ``echo`` task_type."""

    pong: str = "pong"
    protocol_version: str = "1.0"
    agent_id: Optional[str] = None
