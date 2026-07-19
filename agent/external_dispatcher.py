"""
agent/external_dispatcher.py — Task-type-based dispatch for /a2a/external.

This dispatcher speaks the spec's vocabulary: it routes on the A2A envelope's
``task_type`` field. All 14 Part A (Phase C) message types are handled (inbound
NPCI→Bank messages do real work; Bank-sent messages are validate-and-acked on
loopback); certification (Part B) types return a not_implemented error.

Each handler is wrapped with:
    1. Idempotency check (message_id + business key)
    2. Handler execution
    3. State advancement on success
    4. Audit logging (success or failure)

NOTE: /a2a/external is a spec-conformant endpoint for dev/cert interoperability
with NPCI's actual platform.  Auth (HMAC + layers 1-8) is enforced in the route
via ``a2a_external_auth`` before this dispatcher is reached.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from a2a_core.audit import (
    IdempotencyClaim,
    claim_idempotency_key,
    complete_idempotency_claims,
    compute_business_key_marker,
    log_a2a_message,
)
from a2a_core.auth import A2AAuthContext
from a2a_core.models import (
    A2AEnvelope,
    A2ATaskType,
    BlockerResolutionPayload,
    BlockerStatusUpdatePayload,
    CounterDecisionPayload,
    EchoResponse,
)
from a2a_core.state import (
    CERT_TASK_TARGET,
    advance_cert_state,
    advance_change_state,
    change_target_state,
)

# ---------------------------------------------------------------------------
# Handler type
# ---------------------------------------------------------------------------

HandlerFn = Callable[[A2AEnvelope, Session, A2AAuthContext], Any]


class NotImplementedTaskError(ValueError):
    """Known NPCI task type that is intentionally rejected until implemented."""


class PayloadValidationError(ValueError):
    """Inbound payload failed schema validation (spec §9 payload_validation_error)."""


class UnknownTaskTypeError(ValueError):
    """task_type is not in the enum / has no handler (spec §9 unknown_task_type)."""


def _validate(model_cls, payload: dict):
    """Validate an inbound payload, raising PayloadValidationError on failure."""
    from pydantic import ValidationError

    try:
        return model_cls.model_validate(payload or {})
    except ValidationError as exc:
        raise PayloadValidationError(str(exc))


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def handle_change_communication(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound change_communication (NPCI → Bank).

    Unpacks product_kit[] entries doc-by-doc, queues ingestion for each,
    and auto-fires proposal_acknowledged.
    """
    from services.a2a_ingestor import handle_change_communication_ingest
    return await handle_change_communication_ingest(envelope, db)


async def handle_proposal_acknowledged(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Acknowledge receipt of a proposal_acknowledged (Bank → NPCI auto-fire confirmation).

    This is mostly a no-op on our side (we are the bank sending it), but
    if NPCI echoes it back or we receive it as a confirmation, we just ack.
    """
    return {
        "status": "acknowledged",
        "change_id": envelope.change_id,
        "task_type": "proposal_acknowledged",
    }


async def handle_query(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound query (Bank → NPCI).

    On our side (as the bank), this is outbound — but if we receive it
    as a loopback/test, just acknowledge.
    """
    return {
        "status": "acknowledged",
        "change_id": envelope.change_id,
        "task_type": "query",
    }


async def handle_clarification_response(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound clarification_response (NPCI → Bank).

    Delegates to the refactored reply handler with full field parsing.
    """
    from services.npci_reply import handle_clarification_response_ingest
    return await handle_clarification_response_ingest(envelope, db)


async def handle_counter_decision(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound counter_decision (NPCI → Bank): record NPCI's decision."""
    from database import A2ACounterProposal

    payload = _validate(CounterDecisionPayload, envelope.payload)
    cp = (
        db.query(A2ACounterProposal)
        .filter(A2ACounterProposal.counter_proposal_id == payload.in_response_to)
        .first()
    )
    if cp:
        cp.status = "decided"
        cp.decision_json = json.dumps(
            [d.model_dump(exclude_none=True) for d in payload.per_counter], default=str
        )
        db.commit()
    return {
        "status": "recorded",
        "counter_proposal_id": payload.in_response_to,
        "decisions": len(payload.per_counter),
    }


async def handle_blocker_status_update(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound blocker_status_update (NPCI → Bank)."""
    from database import A2ABlocker

    payload = _validate(BlockerStatusUpdatePayload, envelope.payload)
    blk = db.query(A2ABlocker).filter(A2ABlocker.blocker_id == payload.blocker_id).first()
    if blk:
        blk.status = payload.status
        if payload.crm:
            blk.crm_json = json.dumps(payload.crm, default=str)
        db.commit()
    return {"status": "recorded", "blocker_id": payload.blocker_id, "blocker_status": payload.status}


async def handle_blocker_resolution(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound blocker_resolution (NPCI → Bank): terminal state + unblock."""
    from database import A2ABlocker, A2AChange

    payload = _validate(BlockerResolutionPayload, envelope.payload)
    blk = db.query(A2ABlocker).filter(A2ABlocker.blocker_id == payload.blocker_id).first()
    if blk:
        blk.status = payload.resolution
        blk.resolution = payload.resolution
        blk.resolution_text = payload.resolution_text
        if payload.crm:
            blk.crm_json = json.dumps(payload.crm, default=str)
        db.commit()

    # Spec: assignment.blocked_at cleared on resolved/wontfix; deferred preserves it.
    if payload.resolution in ("resolved", "wontfix") and envelope.change_id:
        change = db.query(A2AChange).filter(A2AChange.change_id == envelope.change_id).first()
        if change:
            change.blocked_at = None
            change.blocked_reason = None
            db.commit()
    return {"status": "recorded", "blocker_id": payload.blocker_id, "resolution": payload.resolution}


async def handle_milestone_status_request(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Process an inbound milestone_status_request → return a milestone_status_report."""
    from services.npci_outbound import build_milestone_status_report

    report = build_milestone_status_report(
        envelope.change_id or "", in_reply_to=envelope.correlation_id
    )
    return {"task_type": "milestone_status_report", "payload": report}


async def handle_ack_only(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Validate-and-ack for messages the Bank normally *sends* (loopback receipt).

    State advancement (if applicable) is handled centrally after the handler.
    """
    return {
        "status": "acknowledged",
        "task_type": envelope.task_type,
        "change_id": envelope.change_id,
    }


async def handle_echo(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Echo connectivity test — returns immediately."""
    agent_id = envelope.agent_id or "mindgate_a2a_agent"
    return EchoResponse(
        pong="pong",
        protocol_version="1.0",
        agent_id=agent_id,
    ).model_dump()


async def _not_implemented_handler(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Handler for task_types not yet fully implemented.

    Raises ValueError so the caller returns a spec-compliant JSON-RPC error
    (-32601) instead of a silent ACK.  Silent success on unimplemented logic
    causes state divergence: NPCI thinks we processed the message, but we
    didn't act on it.

    These are logged to audit with error_code='not_implemented' by the
    dispatch_external error path, making them queryable and alertable.
    """
    raise NotImplementedTaskError(
        f"Task type '{envelope.task_type}' is not yet implemented. "
        f"change_id={envelope.change_id}, cflow_id={envelope.cflow_id}. "
        f"Message was received but NOT processed — no state was changed."
    )


# ---------------------------------------------------------------------------
# Handler registry — maps each A2ATaskType to its handler function
# ---------------------------------------------------------------------------

EXTERNAL_HANDLERS: dict[str, HandlerFn] = {
    # ── Part A (Phase C) change lifecycle — all 14 handled ─────────────────
    # Inbound NPCI→Bank messages do real work; messages the Bank normally
    # sends are validate-and-acked on loopback receipt.
    A2ATaskType.CHANGE_COMMUNICATION: handle_change_communication,   # in
    A2ATaskType.CLARIFICATION_RESPONSE: handle_clarification_response,  # in
    A2ATaskType.COUNTER_DECISION: handle_counter_decision,           # in
    A2ATaskType.BLOCKER_STATUS_UPDATE: handle_blocker_status_update, # in
    A2ATaskType.BLOCKER_RESOLUTION: handle_blocker_resolution,       # in
    A2ATaskType.MILESTONE_STATUS_REQUEST: handle_milestone_status_request,  # in → report
    A2ATaskType.PROPOSAL_ACKNOWLEDGED: handle_proposal_acknowledged,  # out (ack on loopback)
    A2ATaskType.QUERY: handle_query,                                 # out (ack on loopback)
    A2ATaskType.CHANGE_ACKNOWLEDGEMENT: handle_ack_only,             # out
    A2ATaskType.COUNTER_PROPOSAL: handle_ack_only,                   # out
    A2ATaskType.MILESTONE_UPDATE: handle_ack_only,                   # out
    A2ATaskType.MILESTONE_STATUS_REPORT: handle_ack_only,           # out
    A2ATaskType.READINESS_DECLARATION: handle_ack_only,             # out
    A2ATaskType.BLOCKER: handle_ack_only,                            # out

    # ── Connectivity ───────────────────────────────────────────────────────
    A2ATaskType.ECHO: handle_echo,

    # ── Out of scope: certification (Part B) + generic cross-cutting ───────
    # Return a spec-compliant JSON-RPC error rather than silently ACKing.
    A2ATaskType.CERT_CONFIG_REQUEST: _not_implemented_handler,
    A2ATaskType.CERT_CONFIG_RESPONSE: _not_implemented_handler,
    A2ATaskType.CERT_SETUP_COMPLETE: _not_implemented_handler,
    A2ATaskType.CERT_CASE_RESULT: _not_implemented_handler,
    A2ATaskType.CERT_CASE_TRIAGE: _not_implemented_handler,
    A2ATaskType.CERT_FIX_SUBMITTED: _not_implemented_handler,
    A2ATaskType.CERT_WAIVER_REQUEST: _not_implemented_handler,
    A2ATaskType.CERT_WAIVER_RESPONSE: _not_implemented_handler,
    A2ATaskType.CERT_DISPUTE_RAISED: _not_implemented_handler,
    A2ATaskType.CERT_DISPUTE_RESOLUTION: _not_implemented_handler,
    A2ATaskType.CERT_COMPLETED: _not_implemented_handler,
    A2ATaskType.CERT_ABORTED: _not_implemented_handler,
    A2ATaskType.STATUS_REQUEST: _not_implemented_handler,
    A2ATaskType.STATUS_RESPONSE: _not_implemented_handler,
    A2ATaskType.NOTIFICATION: _not_implemented_handler,
}


# ---------------------------------------------------------------------------
# Main dispatch function
# ---------------------------------------------------------------------------

async def dispatch_external(
    envelope: A2AEnvelope,
    db: Session,
    ctx: A2AAuthContext,
) -> dict:
    """Dispatch an externally-received A2A message.

    Steps:
        1. Transport-level idempotency (message_id)
        2. Business-level idempotency (per-type key)
        3. Look up handler by task_type
        4. Execute handler
        5. Advance lifecycle state
        6. Log success to audit table
        7. Return result

    On error, logs the rejection and re-raises for the route to handle.
    """
    start_time = time.time()

    # 1. Look up handler before claiming idempotency rows.
    handler = EXTERNAL_HANDLERS.get(envelope.task_type)
    if not handler:
        raise UnknownTaskTypeError(
            f"Unknown task_type '{envelope.task_type}'."
        )

    claims: list[IdempotencyClaim] = []

    # 2. Atomically claim transport-level idempotency.
    claim, cached = claim_idempotency_key(
        db,
        key_type="message_id",
        key_value=envelope.message_id,
        message_id=envelope.message_id,
        task_type=envelope.task_type,
    )
    if cached:
        return cached
    if claim:
        claims.append(claim)

    # 3. Atomically claim business-level idempotency when the task has a key.
    biz_marker = compute_business_key_marker(envelope.task_type, envelope.payload)
    if biz_marker:
        claim, cached = claim_idempotency_key(
            db,
            key_type="business",
            key_value=biz_marker,
            message_id=envelope.message_id,
            task_type=envelope.task_type,
        )
        if cached:
            if envelope.task_type == A2ATaskType.CHANGE_COMMUNICATION:
                change_id = envelope.payload.get("change_id")
                version = envelope.payload.get("version")
                if change_id and version is not None:
                    from services.a2a_ingestor import resend_change_acknowledgements
                    await resend_change_acknowledgements(change_id, int(version))
            cached_failed = cached.get("status") == "failed"
            complete_idempotency_claims(
                db,
                claims,
                status="failed" if cached_failed else "succeeded",
                result=None if cached_failed else cached,
                error_code=cached.get("original_error_code") if cached_failed else None,
                http_status=500 if cached_failed else 200,
            )
            return cached
        if claim:
            claims.append(claim)

    # 4. Execute handler.
    try:
        result = await handler(envelope, db, ctx)
    except NotImplementedTaskError:
        latency = int((time.time() - start_time) * 1000)
        log_a2a_message(
            db,
            envelope=envelope,
            message_id=envelope.message_id,
            task_type=envelope.task_type,
            direction="inbound",
            caller_ip=ctx.caller_ip,
            jwt_sub=ctx.jwt_sub,
            latency_ms=latency,
            error_code="not_implemented",
            payload_ref=json.dumps(envelope.payload, default=str)[:4000],
            http_status=501,
        )
        complete_idempotency_claims(
            db,
            claims,
            status="failed",
            error_code="not_implemented",
            http_status=501,
        )
        raise
    except PayloadValidationError:
        latency = int((time.time() - start_time) * 1000)
        log_a2a_message(
            db,
            envelope=envelope,
            message_id=envelope.message_id,
            task_type=envelope.task_type,
            direction="inbound",
            caller_ip=ctx.caller_ip,
            jwt_sub=ctx.jwt_sub,
            latency_ms=latency,
            error_code="payload_validation_error",
            payload_ref=json.dumps(envelope.payload, default=str)[:4000],
            http_status=400,
        )
        complete_idempotency_claims(
            db,
            claims,
            status="failed",
            error_code="payload_validation_error",
            http_status=400,
        )
        raise
    except Exception as exc:
        # Log the failure to audit
        latency = int((time.time() - start_time) * 1000)
        log_a2a_message(
            db,
            envelope=envelope,
            message_id=envelope.message_id,
            task_type=envelope.task_type,
            direction="inbound",
            caller_ip=ctx.caller_ip,
            jwt_sub=ctx.jwt_sub,
            latency_ms=latency,
            error_code="handler_error",
            payload_ref=json.dumps(envelope.payload, default=str)[:4000],
            http_status=500,
        )
        complete_idempotency_claims(
            db,
            claims,
            status="failed",
            error_code="handler_error",
            http_status=500,
        )
        raise

    # 5. Advance lifecycle state
    if envelope.change_id and change_target_state(envelope.task_type, envelope.payload) is not None:
        advance_change_state(
            envelope.change_id, envelope.task_type, db, payload=envelope.payload
        )

    if envelope.cflow_id and envelope.task_type in CERT_TASK_TARGET:
        advance_cert_state(
            envelope.cflow_id, envelope.task_type, db,
            change_id=envelope.change_id,
        )

    # 6. Log success to audit table
    latency = int((time.time() - start_time) * 1000)
    payload_ref_parts = []
    if biz_marker:
        payload_ref_parts.append(biz_marker)
    payload_ref_parts.append(json.dumps(envelope.payload, default=str)[:3000])
    payload_ref_str = "\n".join(payload_ref_parts)

    result_json = json.dumps(result, default=str) if result else None

    log_a2a_message(
        db,
        envelope=envelope,
        message_id=envelope.message_id,
        task_type=envelope.task_type,
        direction="inbound",
        caller_ip=ctx.caller_ip,
        jwt_sub=ctx.jwt_sub,
        latency_ms=latency,
        error_code=None,  # Success
        payload_ref=payload_ref_str,
        result_payload=result_json,
        http_status=200,
    )
    complete_idempotency_claims(
        db,
        claims,
        status="succeeded",
        result=result,
        http_status=200,
    )

    return result
