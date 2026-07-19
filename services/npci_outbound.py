"""Outbound Bank→NPCI senders for Part A (Phase C) lifecycle messages.

Each public ``send_*`` / ``submit_*`` function:
    1. persists a local record (counter-proposal / blocker / milestone),
    2. builds a spec-conformant A2A envelope,
    3. POSTs it to NPCI via ``post_npci_rpc`` (HMAC+JWT+mTLS handled there),
    4. writes an outbound ``a2a_messages`` audit row, and
    5. advances the change lifecycle state where the spec says so.

These are invoked from the admin ``/a2a/internal`` endpoint (see
``agent/dispatcher.py``) so a human/agent on the bank side can drive the change.
The auto-fired proposal_acknowledged / change_acknowledgement live in
``services/a2a_ingestor.py`` and are unchanged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from a2a_core.audit import log_a2a_message
from a2a_core.models import (
    A2AEnvelope,
    A2ATaskType,
    BlockerPayload,
    CounterProposalPayload,
    MilestoneStatusReportPayload,
    MilestoneUpdatePayload,
    ReadinessDeclarationPayload,
)
from a2a_core.state import advance_change_state
from database import A2ABlocker, A2AChange, A2ACounterProposal, A2AMilestone, SessionLocal
from services.npci_transport import post_npci_rpc

_AGENT_ID = "mindgate_a2a_agent"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_outbound_envelope(
    task_type: str,
    payload: dict,
    *,
    change_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Construct the wire envelope (JSON-RPC params) for an outbound message."""
    envelope = A2AEnvelope(
        task_type=task_type,
        change_id=change_id,
        from_=_AGENT_ID,  # populate_by_name → serialises to "from"
        agent_id=_AGENT_ID,
        agent_run_id=str(uuid.uuid4()),
        correlation_id=correlation_id or str(uuid.uuid4()),
        timestamp=_now_iso(),
        payload=payload,
        message_id=str(uuid.uuid4()),
    )
    return envelope.to_wire()


async def _send_and_audit(
    task_type: str,
    payload: dict,
    *,
    change_id: str | None = None,
    advance_state: bool = False,
) -> dict:
    """Send an outbound message to NPCI and record an audit row.

    Returns the NPCI ``result`` (or an empty dict) merged with local metadata.
    """
    envelope = build_outbound_envelope(task_type, payload, change_id=change_id)
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": envelope["message_id"],
        "method": task_type,
        "params": envelope,
    }

    error_code = None
    http_status = 200
    result: dict = {}
    try:
        data = await post_npci_rpc(rpc_payload)
        result = (data.get("result") if isinstance(data, dict) else {}) or {}
    except Exception as exc:  # noqa: BLE001 — audited + re-raised
        error_code = "outbound_send_failed"
        http_status = 502
        _audit_outbound(envelope, task_type, change_id, error_code, http_status)
        raise

    _audit_outbound(envelope, task_type, change_id, error_code, http_status, result=result)

    if advance_state and change_id:
        db = SessionLocal()
        try:
            advance_change_state(change_id, task_type, db, payload=payload)
        finally:
            db.close()

    return {"sent": True, "message_id": envelope["message_id"], "result": result}


def _audit_outbound(
    envelope: dict,
    task_type: str,
    change_id: str | None,
    error_code: str | None,
    http_status: int,
    result: dict | None = None,
) -> None:
    db = SessionLocal()
    try:
        log_a2a_message(
            db,
            message_id=envelope["message_id"],
            task_type=task_type,
            direction="outbound",
            change_id=change_id,
            agent_id=envelope.get("agent_id"),
            agent_run_id=envelope.get("agent_run_id"),
            correlation_id=envelope.get("correlation_id"),
            error_code=error_code,
            payload_ref=json.dumps(envelope.get("payload"), default=str)[:4000],
            result_payload=json.dumps(result, default=str) if result else None,
            http_status=http_status,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# counter_proposal (Bank → NPCI)
# ---------------------------------------------------------------------------

async def submit_counter_proposal(params: dict) -> dict:
    """Persist and send a counter_proposal. params: change_id, counters[], summary?"""
    change_id = (params.get("change_id") or "").strip()
    counters = params.get("counters") or []
    if not change_id:
        raise ValueError("change_id is required.")
    if not counters:
        raise ValueError("At least one counter is required.")

    counter_proposal_id = params.get("counter_proposal_id") or f"c-{uuid.uuid4().hex[:12]}"
    payload = CounterProposalPayload(
        change_id=change_id,
        counter_proposal_id=counter_proposal_id,
        version_targeted=params.get("version_targeted"),
        proposed_at=_now_iso(),
        counters=counters,
        summary=params.get("summary"),
    ).model_dump(exclude_none=True)

    db = SessionLocal()
    try:
        db.add(A2ACounterProposal(
            counter_proposal_id=counter_proposal_id,
            change_id=change_id,
            version_targeted=params.get("version_targeted"),
            status="open",
            counters_json=json.dumps(counters, default=str),
        ))
        db.commit()
    finally:
        db.close()

    sent = await _send_and_audit(
        A2ATaskType.COUNTER_PROPOSAL, payload, change_id=change_id, advance_state=True
    )
    return {"counter_proposal_id": counter_proposal_id, **sent}


# ---------------------------------------------------------------------------
# milestone_update (Bank → NPCI)
# ---------------------------------------------------------------------------

async def report_milestone(params: dict) -> dict:
    """Upsert a milestone snapshot and send milestone_update. params: change_id, milestone, state, ..."""
    change_id = (params.get("change_id") or "").strip()
    milestone = (params.get("milestone") or "").strip()
    state = (params.get("state") or "").strip()
    if not (change_id and milestone and state):
        raise ValueError("change_id, milestone and state are required.")

    payload = MilestoneUpdatePayload(
        change_id=change_id,
        milestone=milestone,
        state=state,
        version_implementing=params.get("version_implementing"),
        completed_at=_now_iso() if state == "completed" else None,
        notes=params.get("notes"),
        next_milestone=params.get("next_milestone"),
        next_milestone_eta=params.get("next_milestone_eta"),
    ).model_dump(exclude_none=True)

    _upsert_milestone(change_id, milestone, state, params)

    sent = await _send_and_audit(
        A2ATaskType.MILESTONE_UPDATE, payload, change_id=change_id, advance_state=True
    )
    return {"change_id": change_id, "milestone": milestone, "state": state, **sent}


def _upsert_milestone(change_id: str, milestone: str, state: str, params: dict) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(A2AMilestone)
            .filter(A2AMilestone.change_id == change_id, A2AMilestone.milestone == milestone)
            .first()
        )
        if row:
            row.state = state
            row.version_implementing = params.get("version_implementing")
            row.notes = params.get("notes")
            row.eta = params.get("next_milestone_eta")
        else:
            db.add(A2AMilestone(
                change_id=change_id,
                milestone=milestone,
                state=state,
                version_implementing=params.get("version_implementing"),
                notes=params.get("notes"),
                eta=params.get("next_milestone_eta"),
            ))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# readiness_declaration (Bank → NPCI)
# ---------------------------------------------------------------------------

async def declare_readiness(params: dict) -> dict:
    """Send readiness_declaration → change moves to READY_FOR_CERTIFICATION."""
    change_id = (params.get("change_id") or "").strip()
    if not change_id:
        raise ValueError("change_id is required.")

    payload = ReadinessDeclarationPayload(
        change_id=change_id,
        declared_at=_now_iso(),
        version_implementing=int(params.get("version_implementing") or 1),
        implementation_summary=params.get("implementation_summary"),
        evidence_refs=params.get("evidence_refs"),
    ).model_dump(exclude_none=True)

    sent = await _send_and_audit(
        A2ATaskType.READINESS_DECLARATION, payload, change_id=change_id, advance_state=True
    )
    return {"change_id": change_id, **sent}


# ---------------------------------------------------------------------------
# blocker (Bank → NPCI)
# ---------------------------------------------------------------------------

async def raise_blocker(params: dict) -> dict:
    """Persist and send a blocker; marks the change blocked. params: change_id, type, severity, subject, ..."""
    change_id = (params.get("change_id") or "").strip()
    if not change_id:
        raise ValueError("change_id is required.")
    blocker_type = params.get("type") or "other"
    severity = params.get("severity") or "medium"

    blocker_id = params.get("blocker_id") or f"blk-{uuid.uuid4().hex[:12]}"
    payload = BlockerPayload(
        change_id=change_id,
        blocker_id=blocker_id,
        raised_at=_now_iso(),
        type=blocker_type,
        severity=severity,
        subject=params.get("subject"),
        description=params.get("description"),
        impact=params.get("impact"),
        requested_action_from_npci=params.get("requested_action_from_npci"),
    ).model_dump(exclude_none=True)

    db = SessionLocal()
    try:
        db.add(A2ABlocker(
            blocker_id=blocker_id,
            change_id=change_id,
            type=blocker_type,
            severity=severity,
            subject=params.get("subject"),
            status="open",
            payload_json=json.dumps(payload, default=str),
        ))
        change = db.query(A2AChange).filter(A2AChange.change_id == change_id).first()
        if change:
            change.blocked_at = datetime.now(timezone.utc)
            change.blocked_reason = params.get("subject") or blocker_type
        db.commit()
    finally:
        db.close()

    sent = await _send_and_audit(A2ATaskType.BLOCKER, payload, change_id=change_id)
    return {"blocker_id": blocker_id, **sent}


# ---------------------------------------------------------------------------
# milestone_status_report (Either) — used as a reply to milestone_status_request
# ---------------------------------------------------------------------------

async def send_milestone_status_report(change_id: str, in_reply_to: str | None = None) -> dict:
    """Build and send a milestone_status_report snapshot for a change."""
    payload = build_milestone_status_report(change_id, in_reply_to=in_reply_to)
    return await _send_and_audit(
        A2ATaskType.MILESTONE_STATUS_REPORT, payload, change_id=change_id
    )


def build_milestone_status_report(change_id: str, in_reply_to: str | None = None) -> dict:
    """Assemble a milestone_status_report payload from stored milestone rows."""
    db = SessionLocal()
    try:
        rows = db.query(A2AMilestone).filter(A2AMilestone.change_id == change_id).all()
        milestones = [
            {"milestone": r.milestone, "state": r.state, "eta": r.eta}
            for r in rows
        ]
        current = next((r.milestone for r in rows if r.state == "in_progress"), None)
        version = next((r.version_implementing for r in rows if r.version_implementing), None)
    finally:
        db.close()

    return MilestoneStatusReportPayload(
        change_id=change_id,
        in_reply_to=in_reply_to,
        snapshot_at=_now_iso(),
        version_implementing=version,
        milestones=milestones,
        current_milestone=current,
    ).model_dump(exclude_none=True)
