"""WebSocket routes for ticket-scoped NPCI reply notifications."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from auth import get_current_admin
from database import SessionLocal, User, WebSocketTicket, get_db
from ws.manager import manager

router = APIRouter()


def _hash_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _ws_ticket_ttl_seconds() -> int:
    try:
        return int(os.getenv("WS_TICKET_TTL_SECONDS", "60"))
    except (TypeError, ValueError):
        return 60


@router.post("/ws/tickets/{ticket_id}")
def issue_ws_ticket(
    ticket_id: str,
    current_admin: User = Depends(get_current_admin),
    db=Depends(get_db),
):
    """Issue a short-lived, single-use WebSocket ticket for one NPCI ticket."""
    raw_ticket = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_ws_ticket_ttl_seconds())
    token_hash = _hash_ticket(raw_ticket)

    db.query(WebSocketTicket).filter(WebSocketTicket.expires_at < now).delete(
        synchronize_session=False
    )
    db.add(WebSocketTicket(
        token_hash=token_hash,
        ticket_id=ticket_id,
        admin_id=current_admin.id,
        expires_at=expires_at,
    ))
    db.commit()

    return {
        "ticket": raw_ticket,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def _consume_ws_ticket(raw_ticket: str, ticket_id: str) -> bool:
    """Atomically consume a single-use WebSocket ticket."""
    if not raw_ticket:
        return False

    from sqlalchemy import text

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.query(WebSocketTicket).filter(WebSocketTicket.expires_at < now).delete(
            synchronize_session=False
        )
        result = db.execute(
            text("""
                UPDATE ws_tickets
                SET used_at = NOW()
                WHERE token_hash = :token_hash
                  AND ticket_id = :ticket_id
                  AND used_at IS NULL
                  AND expires_at >= NOW()
            """),
            {"token_hash": _hash_ticket(raw_ticket), "ticket_id": ticket_id},
        )
        db.commit()
        return result.rowcount == 1
    finally:
        db.close()


@router.websocket("/ws/{ticket_id}")
async def ticket_websocket(websocket: WebSocket, ticket_id: str):
    """Connect to /ws/{ticket_id}?ticket=<short-lived-single-use-ticket>."""
    ws_ticket = websocket.query_params.get("ticket", "")

    if not _consume_ws_ticket(ws_ticket, ticket_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(ticket_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ticket_id, websocket)
