"""
a2a_core/models.py — Pydantic models derived from the NPCI Unified AgentNxt A2A
Protocol Specification v1.0.

Covers:
    - Common envelope (spec §4)
    - Part A (Phase C) 14 message types + echo, fully modelled (spec §6, Appendix B)
    - Certification (Part B) task_type names retained but out of scope
    - Fixed doc_type enum for change_communication.product_kit[] (spec §6 Part A)
    - Query and clarification_response payloads (spec §Appendix B)
    - Change and Certification lifecycle state enums (spec §6)
    - Spec error codes (spec §4.1)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, Field

# ---------------------------------------------------------------------------
# §6 — task_type enum (Part A fully in scope; Part B retained, out of scope)
# ---------------------------------------------------------------------------

class A2ATaskType(StrEnum):
    """task_type values from the NPCI A2A spec §6.

    Part A (Phase C change lifecycle) uses the exact v1.0 names below. The
    certification (Part B) members are retained for forward compatibility but
    are out of scope for the current build.
    """

    # ── Change lifecycle (Phase C) — 14 messages, spec v1.0 ────────────────
    CHANGE_COMMUNICATION = "change_communication"
    PROPOSAL_ACKNOWLEDGED = "proposal_acknowledged"
    CHANGE_ACKNOWLEDGEMENT = "change_acknowledgement"
    QUERY = "query"
    CLARIFICATION_RESPONSE = "clarification_response"
    COUNTER_PROPOSAL = "counter_proposal"
    COUNTER_DECISION = "counter_decision"
    MILESTONE_UPDATE = "milestone_update"
    MILESTONE_STATUS_REQUEST = "milestone_status_request"
    MILESTONE_STATUS_REPORT = "milestone_status_report"
    READINESS_DECLARATION = "readiness_declaration"
    BLOCKER = "blocker"
    BLOCKER_STATUS_UPDATE = "blocker_status_update"
    BLOCKER_RESOLUTION = "blocker_resolution"

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

    # Protocol / schema (spec §9)
    UNKNOWN_TASK_TYPE = "unknown_task_type"
    PAYLOAD_VALIDATION_ERROR = "payload_validation_error"


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

    ``query_text`` also accepts the spec field name ``question``; ``subject``
    and ``evidence_refs`` are accepted for full Appendix B fidelity.
    """

    model_config = {"populate_by_name": True}

    change_id: str
    query_id: Optional[str] = None  # Assigned by the sender; used as business idempotency key
    clause_ref: Optional[str] = None  # e.g., "TSD §4.2.3"
    phase: Optional[str] = None  # Current lifecycle phase of the change
    subject: Optional[str] = None  # One-line topic (spec)
    bank_interpretation: str  # Bank's reading of the clause in question
    priority: str = "normal"  # "normal" | "high" | "critical"
    evidence_refs: Optional[list[str]] = None
    query_text: str = Field(validation_alias=AliasChoices("query_text", "question"))


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

    model_config = {"populate_by_name": True}

    change_id: str
    query_id: Optional[str] = None
    ticket_id: Optional[str] = None
    # Accept the spec field name ``response`` as well as legacy ``response_text``.
    response_text: str = Field(validation_alias=AliasChoices("response_text", "response"))

    interpretation_confirmed: bool = False
    # Spec sends objects (e.g. {"doc": "...", "section": "..."}); legacy callers
    # send plain strings. Accept either.
    grounded_in: Optional[list[Any]] = None
    amends_change: bool = False
    amendment_ref: Optional[str] = None  # Reference to the amendment if amends_change=True
    approver: Optional[Any] = None  # str (legacy) or object {role,name,approved_at} (spec)
    generation_mode: Optional[str] = None  # "manual" | "ai_assisted" | "auto" | "ai_verbatim"


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


# ---------------------------------------------------------------------------
# §Appendix B — remaining Part A (Phase C) payloads
# Field names track the spec exactly; long free-text values are plain strings.
# ---------------------------------------------------------------------------

class ChangeAcknowledgementPayload(BaseModel):
    """``change_acknowledgement`` (Bank → NPCI) — formal acceptance of a change."""

    decision: str = "ACCEPT"  # ACCEPT only; counter/waiver are separate messages
    accepted_at: Optional[str] = None
    version_accepted: int
    supersedes_acceptance_of: Optional[int] = None
    accepted_by: Optional[dict[str, Any]] = None
    planned_waiver_case_ids: Optional[list[str]] = None
    remarks: Optional[str] = None


class CounterItem(BaseModel):
    """A single per-clause counter within a counter_proposal."""

    clause_ref: str
    clause_text: Optional[str] = None
    classification: Optional[str] = None  # mandatory | optional
    proposed_text: Optional[str] = None
    rationale: Optional[str] = None
    evidence_refs: Optional[list[str]] = None
    alternative_offered: Optional[str] = None


class CounterProposalPayload(BaseModel):
    """``counter_proposal`` (Bank → NPCI)."""

    change_id: str
    counter_proposal_id: str
    version_targeted: Optional[int] = None
    proposed_at: Optional[str] = None
    counters: list[CounterItem]
    summary: Optional[str] = None
    bank_lead: Optional[dict[str, Any]] = None
    references: Optional[list[dict[str, Any]]] = None


class PerCounterDecision(BaseModel):
    """NPCI's decision on a single clause within a counter_decision."""

    clause_ref: str
    decision: str  # accepted | rejected | accepted_with_modification | addressed_in_revision
    modified_text: Optional[str] = None
    reasoning: Optional[str] = None


class CounterDecisionPayload(BaseModel):
    """``counter_decision`` (NPCI → Bank)."""

    change_id: str
    in_response_to: str  # the counter_proposal_id
    decided_at: Optional[str] = None
    per_counter: list[PerCounterDecision]
    references: Optional[list[dict[str, Any]]] = None


class Risk(BaseModel):
    """An informational risk entry inside a milestone_update."""

    description: str
    severity: Optional[str] = None  # info | low | medium | high
    impact: Optional[str] = None
    mitigation: Optional[str] = None


class MilestoneUpdatePayload(BaseModel):
    """``milestone_update`` (Bank → NPCI)."""

    change_id: str
    milestone: str  # design | coding | testing
    state: str  # in_progress | completed | at_risk | delayed | reopened
    version_implementing: Optional[int] = None
    completed_at: Optional[str] = None
    notes: Optional[str] = None
    evidence_refs: Optional[list[str]] = None
    next_milestone: Optional[str] = None
    next_milestone_eta: Optional[str] = None
    risks: Optional[list[Risk]] = None
    amends_prior_update: Optional[str] = None
    bank_lead: Optional[dict[str, Any]] = None


class MilestoneStatusRequestPayload(BaseModel):
    """``milestone_status_request`` (Either) — poll for milestone state."""

    change_id: str
    scope: str = "all"  # all | current_only | with_risks


class MilestoneStatusReportPayload(BaseModel):
    """``milestone_status_report`` (Either) — snapshot reply."""

    change_id: str
    in_reply_to: Optional[str] = None
    snapshot_at: Optional[str] = None
    version_implementing: Optional[int] = None
    milestones: list[dict[str, Any]]
    current_milestone: Optional[str] = None
    ready_for_cert_eta: Optional[str] = None
    active_risks: Optional[list[dict[str, Any]]] = None
    bank_lead: Optional[dict[str, Any]] = None


class ReadinessDeclarationPayload(BaseModel):
    """``readiness_declaration`` (Bank → NPCI) — formal handoff to certification."""

    change_id: str
    declared_at: Optional[str] = None
    version_implementing: int
    implementation_summary: Optional[str] = None
    evidence_refs: Optional[list[str]] = None
    bank_lead: Optional[dict[str, Any]] = None


class BlockerPayload(BaseModel):
    """``blocker`` (Bank → NPCI) — an impediment requiring NPCI action."""

    change_id: str
    blocker_id: str
    raised_at: Optional[str] = None
    type: str  # simulator_issue | environment_failure | credential_missing | ...
    severity: str  # critical | high | medium | low
    subject: Optional[str] = None
    description: Optional[str] = None
    impact: Optional[str] = None
    investigation_done: Optional[str] = None
    options_considered: Optional[str] = None
    requested_action_from_npci: Optional[str] = None
    blocks: Optional[list[str]] = None
    bank_internal_ticket: Optional[dict[str, Any]] = None
    references: Optional[list[dict[str, Any]]] = None
    evidence_refs: Optional[list[str]] = None
    bank_lead: Optional[dict[str, Any]] = None


class BlockerStatusUpdatePayload(BaseModel):
    """``blocker_status_update`` (NPCI → Bank) — intermediate investigation status."""

    change_id: str
    blocker_id: str
    updated_at: Optional[str] = None
    status: str  # received | triaged | assigned | in_investigation | ...
    assigned_team: Optional[str] = None
    crm: Optional[dict[str, Any]] = None
    estimated_resolution_by: Optional[str] = None
    notes: Optional[str] = None
    references: Optional[list[dict[str, Any]]] = None


class BlockerResolutionPayload(BaseModel):
    """``blocker_resolution`` (NPCI → Bank) — terminal state for a blocker."""

    change_id: str
    blocker_id: str
    resolved_at: Optional[str] = None
    resolution: str  # resolved | wontfix | deferred
    resolution_text: Optional[str] = None
    action_taken: Optional[str] = None
    patched_artefacts: Optional[list[str]] = None
    crm: Optional[dict[str, Any]] = None
    assigned_team: Optional[str] = None
    references: Optional[list[dict[str, Any]]] = None
