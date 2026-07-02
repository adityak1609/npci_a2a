"""
agent/external_dispatcher.py — Task-type-based dispatch for /a2a/external.

This dispatcher speaks the spec's vocabulary: it routes on the A2A envelope's
``task_type`` field against the 28 defined message types (+ echo).

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
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from a2a_audit import (
    claim_idempotency_key,
    complete_idempotency_claims,
    compute_business_key_marker,
    IdempotencyClaim,
    log_a2a_message,
)
from a2a_auth import A2AAuthContext
from a2a_models import (
    A2AEnvelope,
    A2ATaskType,
    ClarificationResponsePayload,
    ChangeCommunicationPayload,
    EchoPayload,
    EchoResponse,
    QueryPayload,
)
from a2a_state import (
    CERT_TASK_TARGET,
    CHANGE_TASK_TARGET,
    advance_cert_state,
    advance_change_state,
)


# ---------------------------------------------------------------------------
# Handler type
# ---------------------------------------------------------------------------

HandlerFn = Callable[[A2AEnvelope, Session, A2AAuthContext], Any]


class NotImplementedTaskError(ValueError):
    """Known NPCI task type that is intentionally rejected until implemented."""


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


async def handle_cert_config_request(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Reject cert_config_request until a real handler is implemented."""
    raise NotImplementedTaskError(
        "cert_config_request is not implemented; message was not processed."
    )


async def handle_cert_case_result(
    envelope: A2AEnvelope, db: Session, ctx: A2AAuthContext
) -> dict:
    """Reject cert_case_result until a real handler is implemented."""
    raise NotImplementedTaskError(
        "cert_case_result is not implemented; message was not processed."
    )


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
    # ── Change lifecycle (full implementation) ─────────────────────────────
    A2ATaskType.CHANGE_COMMUNICATION: handle_change_communication,
    A2ATaskType.PROPOSAL_ACKNOWLEDGED: handle_proposal_acknowledged,
    A2ATaskType.QUERY: handle_query,
    A2ATaskType.CLARIFICATION_RESPONSE: handle_clarification_response,

    # ── Cert lifecycle (full implementation) ────────────────────────────────
    A2ATaskType.CERT_CONFIG_REQUEST: handle_cert_config_request,
    A2ATaskType.CERT_CASE_RESULT: handle_cert_case_result,

    # ── Connectivity ───────────────────────────────────────────────────────
    A2ATaskType.ECHO: handle_echo,

    # ── Stubs for remaining 21 types ──────────────────────────────────────
    # ── Not-yet-implemented handlers (return spec-compliant error) ─────────
    # These return a JSON-RPC error instead of silently ACKing, so NPCI
    # knows the message was NOT processed and can alert/retry.
    A2ATaskType.COUNTER_PROPOSAL: _not_implemented_handler,
    A2ATaskType.COUNTER_PROPOSAL_RESPONSE: _not_implemented_handler,
    A2ATaskType.DESIGN_SUBMISSION: _not_implemented_handler,
    A2ATaskType.DESIGN_REVIEW: _not_implemented_handler,
    A2ATaskType.CODING_COMPLETE: _not_implemented_handler,
    A2ATaskType.TEST_RESULTS: _not_implemented_handler,
    A2ATaskType.CHANGE_ACCEPTED: _not_implemented_handler,
    A2ATaskType.CHANGE_REJECTED: _not_implemented_handler,
    A2ATaskType.CHANGE_ABORTED: _not_implemented_handler,
    A2ATaskType.CERT_CONFIG_RESPONSE: _not_implemented_handler,
    A2ATaskType.CERT_SETUP_COMPLETE: _not_implemented_handler,
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
        raise ValueError(
            f"Unknown task_type '{envelope.task_type}'. "
            f"Valid types: {', '.join(sorted(EXTERNAL_HANDLERS.keys()))}"
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
    if envelope.change_id and envelope.task_type in CHANGE_TASK_TARGET:
        advance_change_state(envelope.change_id, envelope.task_type, db)

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
