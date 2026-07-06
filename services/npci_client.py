"""Outbound NPCI query client and internal review routing.

Implements the spec's query flow (§Appendix B):
- If the matched chunk has a change_id → construct a conformant query envelope
- If no change_id → route to internal review queue (spec misuse prevention)

Also provides a generic outbound A2A message sender for proposal_acknowledged
and other Bank→NPCI messages.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx

from database import (
    A2AChange,
    AuditLog,
    DocumentChunk,
    NpciTicket,
    SessionLocal,
)

# Outbound retry configuration
_OUTBOUND_MAX_RETRIES = 3
_OUTBOUND_BACKOFF_BASE = 1  # seconds — 1, 2, 4
_OUTBOUND_TIMEOUT = 30      # per-attempt timeout


def _outbound_cert():
    cert_file = os.getenv("NPCI_CLIENT_CERT_FILE", "").strip()
    key_file = os.getenv("NPCI_CLIENT_KEY_FILE", "").strip()
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        if not cert_file or not key_file:
            raise EnvironmentError(
                "NPCI_CLIENT_CERT_FILE and NPCI_CLIENT_KEY_FILE are required in production."
            )
    if cert_file and key_file:
        return (cert_file, key_file)
    if cert_file:
        return cert_file
    return None


def _signed_headers(raw_body: bytes) -> dict:
    from services.settings_store import get_setting
    api_key = get_setting("NPCI_API_KEY", os.getenv("NPCI_API_KEY"))
    if not api_key:
        raise EnvironmentError("NPCI_API_KEY must be set.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    hmac_secret = (get_setting("A2A_HMAC_SECRET", os.getenv("A2A_HMAC_SECRET")) or "").strip()
    if hmac_secret:
        timestamp = str(int(time.time()))
        nonce = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"[:32]
        body_hash = hashlib.sha256(raw_body).hexdigest()
        signing_string = f"{timestamp}.{nonce}.{body_hash}"
        signature = hmac.new(
            hmac_secret.encode("utf-8"),
            signing_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers.update({
            "X-NPCI-Timestamp": timestamp,
            "X-NPCI-Nonce": nonce,
            "X-NPCI-Signature": signature,
        })
    return headers


async def post_npci_rpc(rpc_payload: dict) -> dict:
    """POST a JSON-RPC payload to NPCI with retry and outbound auth headers."""
    from services.settings_store import get_setting
    url = get_setting("NPCI_AGENT_URL", os.getenv("NPCI_AGENT_URL"))
    if not url:
        raise EnvironmentError("NPCI_AGENT_URL must be set.")

    raw_body = json.dumps(rpc_payload, separators=(",", ":"), default=str).encode("utf-8")
    cert = _outbound_cert()
    last_exc = None

    for attempt in range(_OUTBOUND_MAX_RETRIES + 1):
        try:
            headers = _signed_headers(raw_body)
            async with httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT, cert=cert) as client:
                response = await client.post(url, content=raw_body, headers=headers)

            if 400 <= response.status_code < 500:
                response.raise_for_status()
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Server error {response.status_code}",
                    request=response.request,
                    response=response,
                )

            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                raise ValueError(data["error"].get("message", "NPCI RPC failed."))
            return data
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < _OUTBOUND_MAX_RETRIES:
                wait = _OUTBOUND_BACKOFF_BASE * (2 ** attempt)
                print(
                    f"[npci_client] Transient error on attempt {attempt + 1}/"
                    f"{_OUTBOUND_MAX_RETRIES + 1}, retrying in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
        except Exception as exc:
            last_exc = exc
            break

    raise last_exc or RuntimeError("NPCI RPC failed without an exception.")


async def handle_escalate(params: dict) -> dict:
    """agent.escalate_to_npci — Construct and send a spec-conformant query,
    or route to internal review if no change_id is resolvable.

    This is called from /a2a/internal (admin frontend) when the RAG chatbot
    hits low confidence and the user chooses to escalate.

    Params:
        query: str — The question text
        session_id: str — Chat session (optional)
        context: dict — Optional context from the RAG pipeline
        change_id: str — If provided, scopes the query to a change
        chunk_change_id: str — change_id from the best-matching chunk (if any)
        clause_ref: str — Reference to the clause in question (optional)
        bank_interpretation: str — Bank's interpretation of the clause (optional)
    """
    query_text = (params.get("query") or "").strip()
    if not query_text:
        raise ValueError("query is required.")

    # Try to resolve change_id from params or from the matched chunk
    change_id = params.get("change_id") or params.get("chunk_change_id")
    clause_ref = params.get("clause_ref")
    bank_interpretation = params.get("bank_interpretation") or ""

    # --- If no change_id: route to internal review ---
    if not change_id:
        return await _route_to_internal_review(query_text, params)

    # --- change_id exists: construct conformant query payload ---
    return await _send_npci_query(
        query_text=query_text,
        change_id=change_id,
        clause_ref=clause_ref,
        bank_interpretation=bank_interpretation,
        params=params,
    )


async def _route_to_internal_review(query_text: str, params: dict) -> dict:
    """Route a query to the internal review queue.

    This is used when no change_id is resolvable — sending it as a spec
    query to NPCI would be rejected with unknown_id.
    """
    generated_ticket_id = f"INT-{uuid.uuid4().hex[:12].upper()}"

    db = SessionLocal()
    try:
        db.add(NpciTicket(
            ticket_id=generated_ticket_id,
            query=query_text,
            status="internal_review",
            # Injected server-side by /a2a/internal — scopes updates to the raiser.
            raised_by_user_id=params.get("_acting_user_id"),
        ))
        db.add(AuditLog(
            admin_id=None,
            action="npci_internal_review",
            target=generated_ticket_id,
            detail="No change_id resolvable — routed to internal review instead of NPCI query.",
        ))
        db.commit()
    finally:
        db.close()

    return {
        "ticket_id": generated_ticket_id,
        "status": "internal_review",
        "routed": "internal_review",
        "reason": "no_associated_change",
        "message": (
            "This query cannot be auto-routed to NPCI — no associated change found. "
            "It has been escalated to the internal review queue."
        ),
    }


async def _send_npci_query(
    query_text: str,
    change_id: str,
    clause_ref: str | None,
    bank_interpretation: str,
    params: dict,
) -> dict:
    """Construct and send a spec-conformant query payload (Bank → NPCI).

    Per spec §Appendix B, the query envelope requires:
    - change_id (resolved above)
    - clause_ref (from chunk metadata if available)
    - phase (from the change's current lifecycle state)
    - bank_interpretation (from user or LLM-drafted)
    - priority
    - query_text
    """

    # Look up the change's current lifecycle phase
    phase = "unknown"
    db = SessionLocal()
    try:
        change = db.query(A2AChange).filter(A2AChange.change_id == change_id).first()
        if change:
            phase = change.current_state
    finally:
        db.close()

    # Generate IDs
    request_id = str(uuid.uuid4())
    query_id = f"Q-{uuid.uuid4().hex[:12].upper()}"
    generated_ticket_id = f"TKT-{uuid.uuid4().hex[:12].upper()}"
    correlation_id = str(uuid.uuid4())
    agent_run_id = str(uuid.uuid4())

    # Construct the conformant query payload
    query_payload = {
        "change_id": change_id,
        "query_id": query_id,
        "clause_ref": clause_ref or "",
        "phase": phase,
        "bank_interpretation": bank_interpretation,
        "priority": params.get("priority", "normal"),
        "query_text": query_text,
    }

    # Wrap in A2A envelope (as JSON-RPC params)
    envelope = {
        "task_type": "query",
        "change_id": change_id,
        "from": "mindgate_a2a_agent",
        "agent_id": "mindgate_a2a_agent",
        "agent_run_id": agent_run_id,
        "correlation_id": correlation_id,
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "payload": query_payload,
        "protocol_version": "1.0",
        "message_id": request_id,
    }

    rpc_payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "query",
        "params": envelope,
    }

    try:
        # Route through the transport selector so NPCI_TRANSPORT=a2a sends the
        # query over Google A2A; legacy resolves back to this module's sender.
        from services.npci_transport import post_npci_rpc as _post_npci_rpc
        data = await _post_npci_rpc(rpc_payload)
    except Exception as exc:
        db = SessionLocal()
        try:
            db.add(NpciTicket(
                ticket_id=generated_ticket_id,
                query=query_text,
                status="failed",
                change_id=change_id,
                query_id=query_id,
                correlation_id=correlation_id,
                agent_run_id=agent_run_id,
                clause_ref=clause_ref,
                phase=phase,
                bank_interpretation=bank_interpretation,
                priority=params.get("priority", "normal"),
                raised_by_user_id=params.get("_acting_user_id"),
            ))
            db.add(AuditLog(
                admin_id=None,
                action="npci_query_failed",
                target=generated_ticket_id,
                detail=f"Failed after {_OUTBOUND_MAX_RETRIES + 1} attempts: {exc}",
            ))
            db.commit()
        finally:
            db.close()
        raise

    result = data.get("result") if isinstance(data, dict) else {}
    ticket_id = (result or {}).get("ticket_id") or generated_ticket_id

    db = SessionLocal()
    try:
        existing = db.query(NpciTicket).filter(NpciTicket.ticket_id == ticket_id).first()
        if not existing:
            db.add(NpciTicket(
                ticket_id=ticket_id,
                query=query_text,
                status="pending",
                change_id=change_id,
                query_id=query_id,
                correlation_id=correlation_id,
                agent_run_id=agent_run_id,
                clause_ref=clause_ref,
                phase=phase,
                bank_interpretation=bank_interpretation,
                priority=params.get("priority", "normal"),
                raised_by_user_id=params.get("_acting_user_id"),
            ))
        db.add(AuditLog(
            admin_id=None,
            action="npci_query_raised",
            target=ticket_id,
            detail=f"change_id={change_id}; query_id={query_id}; phase={phase}",
        ))
        db.commit()
    finally:
        db.close()

    return {
        "ticket_id": ticket_id,
        "status": "raised",
        "change_id": change_id,
        "query_id": query_id,
        "phase": phase,
    }
