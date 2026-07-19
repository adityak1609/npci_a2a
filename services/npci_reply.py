"""Inbound NPCI clarification_response handling and WebSocket broadcast.

Implements spec §Appendix B reply handling:
- Parses all ClarificationResponsePayload fields
- If amends_change == true: stores as provisional, does NOT embed in RAG
- If amends_change == false: embeds with grounded_in[] as citations_json
- Stores approver and generation_mode for audit compliance
"""

import asyncio
import json
import os

from google import genai

from database import AuditLog, DocumentChunk, NpciTicket, SessionLocal
from rag.ingest import generate_embeddings
from ws.manager import manager


async def handle_npci_reply(params: dict) -> dict:
    """Legacy agent.receive_npci_reply handler — delegates to the new flow.

    Kept for backward compatibility with any existing callers.
    """
    # Map legacy params to the new clarification_response flow
    ticket_id = (params.get("ticket_id") or "").strip()
    answer = (params.get("answer") or "").strip()
    if not ticket_id or not answer:
        raise ValueError("ticket_id and answer are required.")

    # Create a minimal envelope-like object for the new handler
    class _LegacyEnvelope:
        task_type = "clarification_response"
        change_id = params.get("change_id")
        payload = {
            "ticket_id": ticket_id,
            "response_text": answer,
            "interpretation_confirmed": params.get("interpretation_confirmed", False),
            "grounded_in": params.get("grounded_in"),
            "amends_change": params.get("amends_change", False),
            "amendment_ref": params.get("amendment_ref"),
            "approver": params.get("approver") or params.get("resolved_by"),
            "generation_mode": params.get("generation_mode"),
            "change_id": params.get("change_id"),
            "query_id": params.get("query_id"),
        }

    return await handle_clarification_response_ingest(_LegacyEnvelope(), None)


async def handle_clarification_response_ingest(envelope, db_session) -> dict:
    """Process an inbound clarification_response (NPCI → Bank).

    Per spec §Appendix B, respects:
    - interpretation_confirmed: whether NPCI agrees with bank's reading
    - grounded_in[]: citation references for the response
    - amends_change: if True, response is PROVISIONAL — do NOT embed
    - amendment_ref: reference to the amendment
    - approver: who approved this clarification
    - generation_mode: "manual" | "ai_assisted" | "auto"
    """
    from a2a_core.models import ClarificationResponsePayload

    payload = envelope.payload
    try:
        cr = ClarificationResponsePayload.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"Invalid clarification_response payload: {exc}")

    ticket_id = cr.ticket_id or ""
    answer = cr.response_text
    change_id = cr.change_id or getattr(envelope, "change_id", None)

    if not answer:
        raise ValueError("response_text is required in clarification_response.")

    # Persist all fields to npci_tickets
    db = SessionLocal()
    try:
        ticket = None
        if ticket_id:
            ticket = db.query(NpciTicket).filter(NpciTicket.ticket_id == ticket_id).first()

        # Fallback correlation by query_id: the bank does not put its internal
        # ticket_id on the outbound query wire (only query_id), so replies echo
        # ticket_id=None. Without this, the reply lands as a detached CR- row and
        # the original TKT- stays "pending" forever. query_id is the stable key
        # the bank stored on the ticket, so match the original outbound ticket by
        # it — excluding earlier detached reply rows.
        if ticket is None and cr.query_id:
            ticket = (
                db.query(NpciTicket)
                .filter(NpciTicket.query_id == cr.query_id)
                .filter(NpciTicket.query != "NPCI clarification response")
                .order_by(NpciTicket.created_at.asc())
                .first()
            )
            if ticket:
                ticket_id = ticket.ticket_id

        if ticket:
            ticket.answer = answer
            ticket.change_id = change_id
            ticket.query_id = cr.query_id
            ticket.interpretation_confirmed = cr.interpretation_confirmed
            ticket.grounded_in = json.dumps(cr.grounded_in) if cr.grounded_in else None
            ticket.amends_change = cr.amends_change
            ticket.amendment_ref = cr.amendment_ref
            ticket.approver = cr.approver
            ticket.generation_mode = cr.generation_mode
            ticket.provisional = cr.amends_change  # Provisional if it amends the change
            ticket.status = "provisional" if cr.amends_change else "resolved"
        else:
            ticket_id = ticket_id or f"CR-{__import__('uuid').uuid4().hex[:12].upper()}"
            db.add(NpciTicket(
                ticket_id=ticket_id,
                query="NPCI clarification response",
                status="provisional" if cr.amends_change else "resolved",
                answer=answer,
                change_id=change_id,
                query_id=cr.query_id,
                interpretation_confirmed=cr.interpretation_confirmed,
                grounded_in=json.dumps(cr.grounded_in) if cr.grounded_in else None,
                amends_change=cr.amends_change,
                amendment_ref=cr.amendment_ref,
                approver=cr.approver,
                generation_mode=cr.generation_mode,
                provisional=cr.amends_change,
            ))

        db.add(AuditLog(
            admin_id=None,
            action="npci_clarification_received",
            target=ticket_id,
            detail=(
                f"amends_change={cr.amends_change}; "
                f"interpretation_confirmed={cr.interpretation_confirmed}; "
                f"approver={cr.approver}; generation_mode={cr.generation_mode}"
            ),
        ))
        db.commit()
    finally:
        db.close()

    # --- Branching on amends_change ---
    if cr.amends_change:
        return await _handle_provisional_response(ticket_id, answer, change_id, cr)
    else:
        return await _handle_authoritative_response(ticket_id, answer, change_id, cr)


async def _handle_provisional_response(
    ticket_id: str, answer: str, change_id: str | None, cr
) -> dict:
    """amends_change == true: Store as provisional, do NOT embed in RAG.

    The authoritative update will land later as change_communication v+1.
    Surface a UI banner: "answer pending formal change update".
    """
    # Mark any existing chunk for this ticket as provisional
    db = SessionLocal()
    try:
        existing_chunks = db.query(DocumentChunk).filter(
            DocumentChunk.ticket_id == ticket_id
        ).all()
        for chunk in existing_chunks:
            chunk.provisional = True

        db.add(AuditLog(
            admin_id=None,
            action="npci_reply_provisional",
            target=ticket_id,
            detail=f"amends_change=true; NOT embedded in RAG. "
                   f"Awaiting change_communication v+1 for change_id={change_id}.",
        ))
        db.commit()
    finally:
        db.close()

    # Broadcast via WebSocket with provisional banner
    await manager.broadcast_to_ticket(ticket_id, {
        "event": "npci_reply",
        "ticket_id": ticket_id,
        "answer": answer,
        "status": "provisional",
        "banner": "Answer pending formal change update — this response is provisional "
                  "and will be superseded by an official change communication.",
        "amends_change": True,
        "amendment_ref": cr.amendment_ref,
    })

    return {
        "status": "provisional",
        "ticket_id": ticket_id,
        "amends_change": True,
        "message": "Response stored as provisional. Will be promoted to RAG "
                   "when the formal change update arrives.",
    }


async def _handle_authoritative_response(
    ticket_id: str, answer: str, change_id: str | None, cr
) -> dict:
    """amends_change == false: Embed as authoritative KB content with citations.

    Persists grounded_in[] as citations_json on the document_chunks row so
    the chatbot can surface "per NPCI, see TSD §4.2.3".
    """
    auto_ingest = os.getenv("NPCI_REPLY_AUTO_INGEST", "true").lower() in {"1", "true", "yes", "on"}

    if not auto_ingest:
        await manager.broadcast_to_ticket(ticket_id, {
            "event": "npci_reply",
            "ticket_id": ticket_id,
            "answer": answer,
            "status": "pending_review",
        })
        return {"status": "pending_review", "ticket_id": ticket_id}

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise OSError("GOOGLE_API_KEY not set.")

    client = genai.Client(api_key=api_key)
    embeddings = await asyncio.to_thread(generate_embeddings, [answer], client)

    # Build chunk text with citation context
    citation_text = ""
    if cr.grounded_in:
        citation_text = "\nCitations: " + ", ".join(cr.grounded_in)
    chunk_text = f"NPCI Official Response\nTicket: {ticket_id}{citation_text}\n\n{answer}"
    source_file = f"npci_reply_{ticket_id}"

    # Prepare citations_json
    citations_json = json.dumps(cr.grounded_in) if cr.grounded_in else None

    db = SessionLocal()
    try:
        db.query(DocumentChunk).filter(DocumentChunk.source_file == source_file).delete()
        db.add(DocumentChunk(
            source_file=source_file,
            chunk_index=0,
            text=chunk_text,
            embedding=embeddings[0],
            source="npci_official",
            source_doc=cr.amendment_ref,
            ticket_id=ticket_id,
            change_id=change_id,
            citations_json=citations_json,
            provisional=False,  # Authoritative — NOT provisional
        ))

        ticket = db.query(NpciTicket).filter(NpciTicket.ticket_id == ticket_id).first()
        if ticket:
            ticket.status = "resolved"
            ticket.answer = answer
            ticket.provisional = False

        db.add(AuditLog(
            admin_id=None,
            action="npci_reply_ingested",
            target=ticket_id,
            detail=f"grounded_in={cr.grounded_in}; approver={cr.approver}; "
                   f"generation_mode={cr.generation_mode}",
        ))
        db.commit()
    finally:
        db.close()

    await manager.broadcast_to_ticket(ticket_id, {
        "event": "npci_reply",
        "ticket_id": ticket_id,
        "answer": answer,
        "status": "resolved",
        "citations": cr.grounded_in,
    })

    return {
        "status": "ingested",
        "ticket_id": ticket_id,
        "amends_change": False,
        "citations": cr.grounded_in,
    }
