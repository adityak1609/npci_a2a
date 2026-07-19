"""
mock_npci_agent/main.py — Standalone mock NPCI counterpart for local A2A dev/testing.

Architecture position (v9 spec §2):
    This is the "Dummy NPCI Agent — localhost:PORT_B".  It acts as the NPCI
    counterpart so end-to-end A2A flows can be exercised without connecting to
    the real NPCI platform.

Responsibilities:
    2.1  Document Push  — CLI/UI trigger → build change_communication → HMAC+JWT signed
                          → POST to bank's /a2a/external (PORT_A)
    2.2  Receives from bank — proposal_acknowledged, change_acknowledgement
                               → log only, no storage (per spec)
    2.3  Query Handler  — receives query from bank → builds clarification_response
                          → JSON-RPC 2.0 + A2A envelope → POST back to PORT_A

Quick start:
    1. In the bank's .env set:
           NPCI_AGENT_URL=http://localhost:8001/a2a
           A2A_MTLS_REQUIRED=false
    2. Run:  uvicorn mock_npci_agent.main:app --port 8001 --reload
             OR:  .\\mock_npci_agent\\start.ps1
    3. Open http://localhost:8001

Environment variables (read from ../.env automatically):
    A2A_HMAC_SECRET           — Shared HMAC secret (must match bank's)
    SECRET_KEY                — JWT signing secret (must match bank's)
    A2A_KNOWN_AGENTS          — e.g. npci_central,npci_cert_lab
    BANK_A2A_URL              — Bank's inbound A2A endpoint
                                (default: http://localhost:8000/a2a/external)
    MOCK_NPCI_PORT            — Port to bind on (default: 8001)
    MOCK_NPCI_AGENT_ID        — Agent ID sent in every envelope
                                (default: first value in A2A_KNOWN_AGENTS, or "npci_central")
    MOCK_AUTO_REPLY           — Auto-send clarification_response on query (default: true)
    MOCK_AUTO_REPLY_DELAY_SEC — Seconds before auto-reply fires (default: 1.5)
    MOCK_VALIDATE_INBOUND_HMAC — Validate HMAC of calls from the bank (default: false)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac_lib
import json
import logging
import os
import random
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

# ── Bootstrap: load .env from the project root (parent of this package) ───
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")


# ── Helpers ────────────────────────────────────────────────────────────────

def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "")
    if not val:
        return default
    return val.strip().lower() in {"1", "true", "yes"}


# ── Configuration ──────────────────────────────────────────────────────────
_HMAC_SECRET: str = os.getenv("A2A_HMAC_SECRET", "").strip()
_SECRET_KEY: str = os.getenv("SECRET_KEY", "").strip()

def _resolve_agent_id() -> str:
    explicit = os.getenv("MOCK_NPCI_AGENT_ID", "").strip()
    if explicit:
        return explicit
    agents = _env_list("A2A_KNOWN_AGENTS")
    return agents[0] if agents else "npci_central"

AGENT_ID: str = _resolve_agent_id()
BANK_A2A_URL: str = os.getenv("BANK_A2A_URL", "http://localhost:8000/a2a/external").rstrip("/")
AUTO_REPLY: bool = _env_bool("MOCK_AUTO_REPLY", default=True)
AUTO_REPLY_DELAY: float = float(os.getenv("MOCK_AUTO_REPLY_DELAY_SEC", "1.5"))
# Manual-first: auto-replies to counter-proposals/blockers are OFF by default so
# an operator can respond by hand. Toggleable at runtime via POST /config/auto-reply.
_runtime = {
    "auto_reply_counter": _env_bool("MOCK_AUTO_REPLY_COUNTER", default=False),
    "auto_reply_blocker": _env_bool("MOCK_AUTO_REPLY_BLOCKER", default=False),
    # Queries default to the legacy MOCK_AUTO_REPLY behaviour; switch OFF at
    # runtime to answer bank queries by hand from the Inbox instead.
    "auto_reply_query": AUTO_REPLY,
}
VALIDATE_INBOUND_HMAC: bool = _env_bool("MOCK_VALIDATE_INBOUND_HMAC", default=False)
_LOG_SIZE: int = 500
OUTBOX_DB_PATH: Path = Path(os.getenv("MOCK_NPCI_OUTBOX_DB", str(_HERE / "outbox.sqlite3")))
OUTBOX_POLL_INTERVAL: float = float(os.getenv("MOCK_NPCI_OUTBOX_POLL_SECONDS", "2"))
OUTBOX_MAX_ATTEMPTS: int = int(os.getenv("MOCK_NPCI_OUTBOX_MAX_ATTEMPTS", "0"))
_outbox_dispatcher_task: asyncio.Task | None = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("mock_npci")

# ── In-memory message log ──────────────────────────────────────────────────
_messages: deque[dict] = deque(maxlen=_LOG_SIZE)


def _append_log(
    direction: str,
    task_type: str,
    change_id: str | None,
    summary: str,
    status: str = "ok",
    detail: str = "",
) -> None:
    _messages.appendleft(
        {
            "id": uuid.uuid4().hex[:8],
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "direction": direction,   # "inbound" | "outbound"
            "task_type": task_type,
            "change_id": change_id or "—",
            "summary": summary[:160],
            "status": status,         # "ok" | "error" | "rejected" | "pending"
            "detail": detail[:300],
        }
    )


# ── HMAC signing (outbound → bank) ─────────────────────────────────────────

def _sqlite_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outbox_connect() -> sqlite3.Connection:
    OUTBOX_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OUTBOX_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_outbox() -> None:
    with _outbox_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY,
                change_id TEXT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_outbox_pending_next "
            "ON outbox (status, next_attempt_at)"
        )


def _outbox_insert(change_id: str | None, rpc_payload: dict) -> int:
    now = _sqlite_now()
    with _outbox_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO outbox (change_id, payload, status, attempt_count, next_attempt_at, created_at)
            VALUES (?, ?, 'pending', 0, ?, ?)
            """,
            (change_id, json.dumps(rpc_payload, separators=(",", ":"), default=str), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def _outbox_due_rows() -> list[sqlite3.Row]:
    with _outbox_connect() as conn:
        return list(
            conn.execute(
                """
                SELECT id, change_id, payload, attempt_count
                FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                """,
                (_sqlite_now(),),
            )
        )


def _outbox_mark_delivered(row_id: int) -> None:
    with _outbox_connect() as conn:
        conn.execute("UPDATE outbox SET status = 'delivered' WHERE id = ?", (row_id,))
        conn.commit()


def _retry_delay_seconds(attempt_count: int) -> float:
    curve = [5, 15, 45, 120, 300, 600]
    base = curve[min(max(attempt_count - 1, 0), len(curve) - 1)]
    return base * random.uniform(0.8, 1.2)


def _outbox_mark_failed(row_id: int, attempt_count: int) -> tuple[str, float | None]:
    if OUTBOX_MAX_ATTEMPTS > 0 and attempt_count >= OUTBOX_MAX_ATTEMPTS:
        with _outbox_connect() as conn:
            conn.execute(
                "UPDATE outbox SET status = 'dead', attempt_count = ? WHERE id = ?",
                (attempt_count, row_id),
            )
            conn.commit()
        return "dead", None

    delay = _retry_delay_seconds(attempt_count)
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
    with _outbox_connect() as conn:
        conn.execute(
            """
            UPDATE outbox
            SET attempt_count = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (attempt_count, next_attempt.isoformat(), row_id),
        )
        conn.commit()
    return "pending", delay


def _outbox_counts() -> dict[str, int]:
    with _outbox_connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


# ── Structured inbox (received bank messages) ──────────────────────────────
# Unlike the summary-only in-memory _messages deque, this retains the full
# payload of counter-proposals / blockers / milestones so an operator can view
# and respond to a specific one. Shares the outbox SQLite DB; survives restart.

def _init_inbox() -> None:
    with _outbox_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY,
                message_id TEXT UNIQUE,
                ts TIMESTAMP NOT NULL,
                task_type TEXT NOT NULL,
                change_id TEXT,
                correlation_id TEXT,
                entity_id TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_inbox_type_status ON inbox (task_type, status)"
        )


def _inbox_insert(
    *,
    message_id: str | None,
    task_type: str,
    change_id: str | None,
    correlation_id: str | None,
    entity_id: str | None,
    payload: dict,
    status: str = "new",
) -> None:
    """Store a received message. Deduped on message_id (INSERT OR IGNORE)."""
    with _outbox_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO inbox
                (message_id, ts, task_type, change_id, correlation_id, entity_id, payload_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                _sqlite_now(),
                task_type,
                change_id,
                correlation_id,
                entity_id,
                json.dumps(payload, separators=(",", ":"), default=str),
                status,
            ),
        )
        conn.commit()


def _inbox_list(
    task_type: str | None = None,
    change_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses, args = [], []
    if task_type:
        clauses.append("task_type = ?"); args.append(task_type)
    if change_id:
        clauses.append("change_id = ?"); args.append(change_id)
    if status:
        clauses.append("status = ?"); args.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _outbox_connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM inbox {where} ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append({
            "id": r["id"],
            "message_id": r["message_id"],
            "ts": r["ts"],
            "task_type": r["task_type"],
            "change_id": r["change_id"],
            "correlation_id": r["correlation_id"],
            "entity_id": r["entity_id"],
            "status": r["status"],
            "payload": payload,
        })
    return out


def _inbox_mark_responded(entity_id: str | None) -> int:
    if not entity_id:
        return 0
    with _outbox_connect() as conn:
        cur = conn.execute(
            "UPDATE inbox SET status = 'responded' WHERE entity_id = ? AND status = 'new'",
            (entity_id,),
        )
        conn.commit()
        return cur.rowcount


def _inbox_counts() -> dict[str, int]:
    with _outbox_connect() as conn:
        open_cp = conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE task_type = 'counter_proposal' AND status = 'new'"
        ).fetchone()[0]
        open_blk = conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE task_type = 'blocker' AND status = 'new'"
        ).fetchone()[0]
        open_q = conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE task_type = 'query' AND status = 'new'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    return {
        "open_counter_proposals": int(open_cp),
        "open_blockers": int(open_blk),
        "open_queries": int(open_q),
        "total": int(total),
    }


def _sign_body(raw_body: bytes) -> dict[str, str]:
    """
    Build X-NPCI-* authentication headers for an outbound request.
    Mirrors exactly what the bank's npci_client._signed_headers() produces.
    """
    if not _HMAC_SECRET:
        logger.warning("[mock] A2A_HMAC_SECRET not set — outbound requests will be unsigned")
        return {}
    timestamp = str(int(time.time()))
    nonce = (uuid.uuid4().hex + uuid.uuid4().hex)[:32]
    body_hash = hashlib.sha256(raw_body).hexdigest()
    signing_string = f"{timestamp}.{nonce}.{body_hash}"
    signature = _hmac_lib.new(
        _HMAC_SECRET.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-NPCI-Timestamp": timestamp,
        "X-NPCI-Nonce": nonce,
        "X-NPCI-Signature": signature,
    }


def _verify_inbound_hmac(request: Request, raw_body: bytes) -> bool:
    """
    Verify HMAC headers from an inbound call (bank → mock).
    Returns True if valid or if validation is disabled.
    """
    if not VALIDATE_INBOUND_HMAC or not _HMAC_SECRET:
        return True
    ts = request.headers.get("X-NPCI-Timestamp", "")
    nonce = request.headers.get("X-NPCI-Nonce", "")
    sig = request.headers.get("X-NPCI-Signature", "")
    if not (ts and nonce and sig):
        return False
    body_hash = hashlib.sha256(raw_body).hexdigest()
    signing_string = f"{ts}.{nonce}.{body_hash}"
    expected = _hmac_lib.new(
        _HMAC_SECRET.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return _hmac_lib.compare_digest(expected, sig)


# ── JWT generation (outbound → bank Layer 4) ───────────────────────────────

def _make_bearer_token() -> str:
    """
    Create a JWT the bank's /a2a/external Layer 4 will accept.
    Uses the same SECRET_KEY + HS256 as the bank (dev-only pattern).
    In production NPCI would use an RS256 key pair.
    """
    if not _SECRET_KEY:
        logger.warning("[mock] SECRET_KEY not set — JWT will be a placeholder")
        return "mock_no_secret_key"
    try:
        from jose import jwt as _jose_jwt  # python-jose is already in requirements.txt
        now = datetime.now(timezone.utc)
        payload = {
            "sub": AGENT_ID,
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": uuid.uuid4().hex,
            "role": "npci_agent",
        }
        return _jose_jwt.encode(payload, _SECRET_KEY, algorithm="HS256")
    except Exception as exc:
        logger.error(f"[mock] JWT generation failed: {exc}")
        return "mock_jwt_error"


# ── Envelope builder ───────────────────────────────────────────────────────

def _build_envelope(
    task_type: str,
    payload: dict,
    change_id: str | None = None,
    cflow_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    msg_id = f"mock-{uuid.uuid4().hex}"
    envelope: dict[str, Any] = {
        "task_type": task_type,
        "from": AGENT_ID,
        "agent_id": AGENT_ID,
        "agent_run_id": uuid.uuid4().hex,
        "correlation_id": correlation_id or uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "protocol_version": "1.0",
        "message_id": msg_id,
    }
    if change_id:
        envelope["change_id"] = change_id
    if cflow_id:
        envelope["cflow_id"] = cflow_id
    return envelope


# ── Outbound sender ────────────────────────────────────────────────────────

def _build_rpc_payload(
    task_type: str,
    payload: dict,
    change_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    envelope = _build_envelope(
        task_type, payload, change_id=change_id, correlation_id=correlation_id
    )
    return {
        "jsonrpc": "2.0",
        "id": envelope["message_id"],
        "method": task_type,
        "params": envelope,
    }


async def _send_rpc_payload(rpc_payload: dict) -> dict:
    task_type = rpc_payload.get("method") or "unknown"
    params = rpc_payload.get("params") or {}
    change_id = params.get("change_id")
    raw = json.dumps(rpc_payload, separators=(",", ":"), default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_make_bearer_token()}",
        **_sign_body(raw),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(BANK_A2A_URL, content=raw, headers=headers)
    resp_data: dict = resp.json() if resp.content else {}
    _append_log(
        "outbound",
        task_type,
        change_id,
        summary=f"bank HTTP {resp.status_code}",
        status="ok" if resp.is_success else "error",
        detail=json.dumps(resp_data, default=str)[:300],
    )
    logger.info("[mock] sent %s HTTP %s change_id=%s", task_type, resp.status_code, change_id)
    if not resp.is_success:
        resp.raise_for_status()
    return resp_data


async def _enqueue_change_communication(payload: dict, change_id: str) -> int:
    rpc_payload = _build_rpc_payload("change_communication", payload, change_id=change_id)
    row_id = await asyncio.to_thread(_outbox_insert, change_id, rpc_payload)
    _append_log(
        "outbound",
        "change_communication",
        change_id,
        summary=f"Queued in SQLite outbox row={row_id}",
        status="pending",
        detail=f"bank_url={BANK_A2A_URL}",
    )
    logger.info("[mock] queued change_communication in outbox row=%s change_id=%s", row_id, change_id)
    return row_id


async def _dispatch_outbox_once() -> None:
    rows = await asyncio.to_thread(_outbox_due_rows)
    for row in rows:
        row_id = int(row["id"])
        change_id = row["change_id"]
        task_type = "change_communication"
        try:
            rpc_payload = json.loads(row["payload"])
            task_type = rpc_payload.get("method") or task_type
            await _send_rpc_payload(rpc_payload)
        except Exception as exc:
            next_attempt_count = int(row["attempt_count"]) + 1
            status, delay = await asyncio.to_thread(
                _outbox_mark_failed, row_id, next_attempt_count
            )
            if status == "dead":
                _append_log("outbound", task_type, change_id, summary="Outbox row marked dead", status="error", detail=str(exc))
                logger.error("[mock] outbox row=%s dead after %s attempts: %s", row_id, next_attempt_count, exc)
            else:
                _append_log(
                    "outbound",
                    task_type,
                    change_id,
                    summary=f"Outbox retry scheduled in {delay:.1f}s",
                    status="pending",
                    detail=str(exc),
                )
                logger.warning(
                    "[mock] outbox row=%s attempt=%s failed; retry in %.1fs: %s",
                    row_id,
                    next_attempt_count,
                    delay,
                    exc,
                )
            continue

        await asyncio.to_thread(_outbox_mark_delivered, row_id)
        logger.info("[mock] outbox row=%s delivered change_id=%s", row_id, change_id)


async def _outbox_dispatcher() -> None:
    while True:
        try:
            await _dispatch_outbox_once()
            await asyncio.sleep(OUTBOX_POLL_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("[mock] outbox dispatcher error: %s", exc)
            await asyncio.sleep(OUTBOX_POLL_INTERVAL)


async def _post_to_bank(
    task_type: str,
    payload: dict,
    change_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """POST a HMAC-signed, JWT-authenticated JSON-RPC message to the bank."""
    envelope = _build_envelope(
        task_type, payload, change_id=change_id, correlation_id=correlation_id
    )
    rpc_body_dict = {
        "jsonrpc": "2.0",
        "id": envelope["message_id"],
        "method": task_type,
        "params": envelope,
    }
    raw = json.dumps(rpc_body_dict, separators=(",", ":"), default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_make_bearer_token()}",
        **_sign_body(raw),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(BANK_A2A_URL, content=raw, headers=headers)
        resp_data: dict = resp.json() if resp.content else {}
        _append_log(
            "outbound",
            task_type,
            change_id,
            summary=f"→ bank  HTTP {resp.status_code}",
            status="ok" if resp.is_success else "error",
            detail=json.dumps(resp_data, default=str)[:300],
        )
        logger.info(f"[mock] → {task_type}  HTTP {resp.status_code}  change_id={change_id}")
        return resp_data
    except Exception as exc:
        _append_log("outbound", task_type, change_id, summary="→ bank  FAILED", status="error", detail=str(exc))
        logger.error(f"[mock] → {task_type} failed: {exc}")
        raise


# ── Inbound task handlers ──────────────────────────────────────────────────

def _handle_proposal_acknowledged(params: dict) -> dict:
    """Log only — no storage (per spec §2.2)."""
    change_id = params.get("change_id")
    payload = params.get("payload", {})
    _append_log("inbound", "proposal_acknowledged", change_id, summary="Bank acknowledged proposal", detail=json.dumps(payload)[:200])
    return {"status": "logged"}


def _handle_change_acknowledgement(params: dict) -> dict:
    """Log only — no storage."""
    change_id = params.get("change_id")
    _append_log("inbound", "change_acknowledgement", change_id, summary="Bank acknowledged change")
    return {"status": "logged"}


def _handle_echo(params: dict) -> dict:
    """Echo connectivity test."""
    payload = params.get("payload", {})
    return {"pong": "pong", "protocol_version": "1.0", "agent_id": AGENT_ID, "echo_payload": payload}


async def _handle_query(
    params: dict,
    background_tasks: BackgroundTasks,
    request_id: str | None,
) -> dict:
    """
    Receive a query from the bank (spec §2.3).
    Immediately ACKs; fires a clarification_response after AUTO_REPLY_DELAY.
    """
    change_id = params.get("change_id")
    correlation_id = params.get("correlation_id")
    inner = params.get("payload", {})
    query_id = inner.get("query_id") or f"Q-{uuid.uuid4().hex[:12].upper()}"
    ticket_id = f"TKT-MOCK-{uuid.uuid4().hex[:10].upper()}"
    query_text = inner.get("query_text", "")
    clause_ref = inner.get("clause_ref", "")
    phase = inner.get("phase", "")

    # Store in the structured inbox so an operator can answer it by hand.
    _inbox_insert(
        message_id=params.get("message_id"), task_type="query",
        change_id=change_id, correlation_id=params.get("correlation_id"),
        entity_id=query_id,
        payload={
            "change_id": change_id,
            "query_id": query_id,
            "ticket_id": ticket_id,
            "query_text": query_text,
            "clause_ref": clause_ref,
            "phase": phase,
            "bank_interpretation": inner.get("bank_interpretation", ""),
            "priority": inner.get("priority", "normal"),
        },
        status="new",
    )
    _append_log(
        "inbound",
        "query",
        change_id,
        summary=f"Query received: {query_text[:80]}",
        detail=f"query_id={query_id} clause_ref={clause_ref} phase={phase}",
    )
    logger.info(f"[mock] ← query  change_id={change_id}  query_id={query_id}")

    if _runtime["auto_reply_query"]:
        background_tasks.add_task(
            _auto_reply_clarification,
            change_id=change_id,
            query_id=query_id,
            ticket_id=ticket_id,
            query_text=query_text,
            clause_ref=clause_ref,
            correlation_id=correlation_id,
        )

    return {
        "status": "received",
        "ticket_id": ticket_id,
        "query_id": query_id,
        "auto_reply": _runtime["auto_reply_query"],
        "auto_reply_delay_seconds": AUTO_REPLY_DELAY,
    }


def _extract_doc_text(data: bytes, filename: str) -> str:
    """Extract text from a pushed document's bytes (PDF via PyMuPDF, else utf-8)."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=data, filetype="pdf")
            try:
                return "\n".join(page.get_text("text") for page in doc)
            finally:
                doc.close()
        except Exception:
            return ""
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _change_doc_text(change_id: str | None, max_chars: int = 14000) -> tuple[str, list[str]]:
    """Best-effort text of the document(s) this mock pushed for a change.

    Reads the most recent change_communication from the outbox, decodes each
    product_kit[] content_b64, and extracts text. Returns (text, filenames);
    empty text when nothing usable is found.
    """
    if not change_id:
        return "", []
    try:
        with _outbox_connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM outbox WHERE change_id = ? ORDER BY id DESC LIMIT 20",
                (change_id,),
            ).fetchall()
    except Exception:
        return "", []
    for r in rows:
        try:
            rpc = json.loads(r["payload"])
        except Exception:
            continue
        if rpc.get("method") != "change_communication":
            continue
        kit = (((rpc.get("params") or {}).get("payload")) or {}).get("product_kit") or []
        texts, names = [], []
        for item in kit:
            b64 = item.get("content_b64")
            fn = item.get("filename") or "document"
            if not b64:
                continue
            try:
                data = base64.b64decode(b64)
            except Exception:
                continue
            txt = _extract_doc_text(data, fn).strip()
            if txt:
                texts.append(f"=== {fn} ===\n{txt}")
                names.append(fn)
        if texts:
            return ("\n\n".join(texts))[:max_chars], names
    return "", []


def _ai_clarification(query_text: str, clause_ref: str, doc_text: str, filenames: list[str]):
    """Generate a grounded NPCI answer via Gemini. Returns (answer, grounded_in) or None."""
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key or not doc_text.strip():
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "You are the NPCI clarification desk answering a partner bank's question "
            "about a change document you (NPCI) published. Answer ONLY from the DOCUMENT "
            "below, be specific and concise (2-5 sentences), and cite the relevant part. "
            "If the document does not contain the answer, say so plainly and state what the "
            "bank should refer to or provide. Do not invent policy.\n\n"
            f"=== DOCUMENT ===\n{doc_text}\n\n"
            f"=== BANK QUESTION ===\n{query_text}\n"
            + (f"(clause in question: {clause_ref})\n" if clause_ref else "")
            + "\n=== YOUR ANSWER ==="
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
        )
        answer = (resp.text or "").strip()
        if not answer:
            return None
        grounded = ([clause_ref] if clause_ref else []) + list(filenames[:3])
        return answer, grounded[:4]
    except Exception as exc:
        logger.warning("[mock] AI clarification failed; falling back to canned reply: %s", exc)
        return None


async def _auto_reply_clarification(
    change_id: str | None,
    query_id: str,
    ticket_id: str,
    query_text: str,
    clause_ref: str,
    correlation_id: str | None,
) -> None:
    """Background task: send clarification_response to bank after delay (spec §2.3).

    Prefers a Gemini answer grounded in the document this mock pushed for the
    change; falls back to a canned template if the doc/text/LLM is unavailable.
    """
    await asyncio.sleep(AUTO_REPLY_DELAY)

    ai = None
    doc_text, filenames = await asyncio.to_thread(_change_doc_text, change_id)
    if doc_text:
        ai = await asyncio.to_thread(_ai_clarification, query_text, clause_ref, doc_text, filenames)

    if ai:
        response_text, grounded = ai
        generation_mode = "ai_assisted"
    else:
        grounded = ([clause_ref] if clause_ref else []) + ["TSD §4.2.3", "BRD §2.1", "NPCI Circular 2024-001"]
        grounded = grounded[:4]
        generation_mode = "auto"
        response_text = (
            f"[MOCK NPCI AUTO-REPLY] Regarding your query: '{query_text[:120]}'. "
            "After review, the bank's interpretation is confirmed as per the specification. "
            "For detailed technical guidance refer to TSD §4.2.3 and BRD §2.1. "
            "No amendment to the change document is required at this time."
        )

    clarification_payload = {
        "change_id": change_id or "",
        "query_id": query_id,
        "ticket_id": ticket_id,
        "response_text": response_text,
        "interpretation_confirmed": True,
        "grounded_in": grounded,
        "amends_change": False,
        "approver": f"mock_npci_system/{AGENT_ID}",
        "generation_mode": generation_mode,
    }

    try:
        await _post_to_bank(
            "clarification_response",
            clarification_payload,
            change_id=change_id,
            correlation_id=correlation_id,
        )
        await asyncio.to_thread(_inbox_mark_responded, query_id)
        logger.info(f"[mock] → clarification_response sent  ticket={ticket_id}")
    except Exception as exc:
        logger.error(f"[mock] → clarification_response FAILED  ticket={ticket_id}: {exc}")


# ── Structured inbound handlers: counter-proposal / blocker / milestone ────

def _handle_counter_proposal(params: dict, background_tasks: BackgroundTasks) -> dict:
    """Store a received counter-proposal for operator review; optional auto-decision."""
    payload = params.get("payload", {}) or {}
    change_id = params.get("change_id") or payload.get("change_id")
    cp_id = payload.get("counter_proposal_id")
    counters = payload.get("counters", []) or []
    _inbox_insert(
        message_id=params.get("message_id"), task_type="counter_proposal",
        change_id=change_id, correlation_id=params.get("correlation_id"),
        entity_id=cp_id, payload=payload, status="new",
    )
    _append_log("inbound", "counter_proposal", change_id,
                summary=f"Counter-proposal {cp_id} ({len(counters)} clause(s))")
    if _runtime["auto_reply_counter"] and cp_id and change_id:
        background_tasks.add_task(
            _auto_reply_counter_decision, change_id=change_id,
            counter_proposal_id=cp_id, counters=counters,
            correlation_id=params.get("correlation_id"),
        )
    return {"status": "received", "counter_proposal_id": cp_id,
            "auto_reply": _runtime["auto_reply_counter"]}


def _handle_blocker(params: dict, background_tasks: BackgroundTasks) -> dict:
    """Store a received blocker for operator review; optional auto-resolution."""
    payload = params.get("payload", {}) or {}
    change_id = params.get("change_id") or payload.get("change_id")
    blk_id = payload.get("blocker_id")
    _inbox_insert(
        message_id=params.get("message_id"), task_type="blocker",
        change_id=change_id, correlation_id=params.get("correlation_id"),
        entity_id=blk_id, payload=payload, status="new",
    )
    _append_log("inbound", "blocker", change_id,
                summary=f"Blocker {blk_id} [{payload.get('severity','')}] {payload.get('subject','')}"[:150])
    if _runtime["auto_reply_blocker"] and blk_id and change_id:
        background_tasks.add_task(
            _auto_reply_blocker, change_id=change_id, blocker_id=blk_id,
            correlation_id=params.get("correlation_id"),
        )
    return {"status": "received", "blocker_id": blk_id,
            "auto_reply": _runtime["auto_reply_blocker"]}


def _handle_milestone_inbound(params: dict, task_type: str) -> dict:
    """Store a milestone_update / milestone_status_report (view-only)."""
    payload = params.get("payload", {}) or {}
    change_id = params.get("change_id") or payload.get("change_id")
    entity = str(payload.get("milestone") or payload.get("current_milestone") or task_type)
    _inbox_insert(
        message_id=params.get("message_id"), task_type=task_type,
        change_id=change_id, correlation_id=params.get("correlation_id"),
        entity_id=entity, payload=payload, status="info",
    )
    _append_log("inbound", task_type, change_id,
                summary=f"{task_type}: {entity} {payload.get('state','')}"[:150])
    return {"status": "received", "task_type": task_type}


async def _auto_reply_counter_decision(
    change_id: str, counter_proposal_id: str, counters: list, correlation_id: str | None
) -> None:
    """Background auto-response: accept-with-modification each clause after a delay."""
    await asyncio.sleep(AUTO_REPLY_DELAY)
    per_counter = [
        {
            "clause_ref": c.get("clause_ref", ""),
            "decision": "accepted_with_modification",
            "modified_text": c.get("proposed_text", ""),
            "reasoning": "[MOCK NPCI AUTO] accepted with modification",
        }
        for c in (counters or [])
    ] or [{"clause_ref": "", "decision": "accepted_with_modification"}]
    payload = {
        "change_id": change_id or "", "in_response_to": counter_proposal_id,
        "decided_at": _sqlite_now(), "per_counter": per_counter,
    }
    try:
        await _post_to_bank("counter_decision", payload, change_id=change_id, correlation_id=correlation_id)
        await asyncio.to_thread(_inbox_mark_responded, counter_proposal_id)
    except Exception as exc:
        logger.error(f"[mock] → auto counter_decision FAILED  cp={counter_proposal_id}: {exc}")


async def _auto_reply_blocker(change_id: str, blocker_id: str, correlation_id: str | None) -> None:
    """Background auto-response: triage then resolve the blocker after a delay."""
    await asyncio.sleep(AUTO_REPLY_DELAY)
    try:
        await _post_to_bank(
            "blocker_status_update",
            {"change_id": change_id or "", "blocker_id": blocker_id, "status": "triaged",
             "updated_at": _sqlite_now(), "notes": "[MOCK NPCI AUTO] triaged"},
            change_id=change_id, correlation_id=correlation_id,
        )
        await asyncio.sleep(AUTO_REPLY_DELAY)
        await _post_to_bank(
            "blocker_resolution",
            {"change_id": change_id or "", "blocker_id": blocker_id, "resolution": "resolved",
             "resolved_at": _sqlite_now(), "resolution_text": "[MOCK NPCI AUTO] resolved"},
            change_id=change_id, correlation_id=correlation_id,
        )
        await asyncio.to_thread(_inbox_mark_responded, blocker_id)
    except Exception as exc:
        logger.error(f"[mock] → auto blocker resolution FAILED  blk={blocker_id}: {exc}")


# ── FastAPI application ────────────────────────────────────────────────────

app = FastAPI(
    title="Mock NPCI Agent",
    version="1.0.0",
    description="Dummy NPCI counterpart for local A2A development (Architecture v9 §2)",
    docs_url=None,
    redoc_url=None,
)


# ── POST /a2a — inbound from bank ──────────────────────────────────────────

@app.post("/a2a", tags=["A2A"])
async def inbound_a2a(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Inbound JSON-RPC 2.0 endpoint — receives messages from the bank (PORT_A).
    Per spec §2.2: proposal_acknowledged and change_acknowledgement are logged only.
    Per spec §2.3: query triggers an async clarification_response back to PORT_A.
    """
    raw_body = await request.body()

    # Optional inbound HMAC validation
    if not _verify_inbound_hmac(request, raw_body):
        _append_log("inbound", "?", None, summary="Rejected — HMAC mismatch", status="rejected")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "HMAC verification failed"}},
            status_code=401,
        )

    # Parse JSON-RPC body
    try:
        body: dict = json.loads(raw_body)
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    request_id = body.get("id")
    params: dict = body.get("params") or {}
    task_type: str = params.get("task_type") or body.get("method") or "unknown"

    # Dispatch on task_type
    try:
        if task_type == "proposal_acknowledged":
            result = _handle_proposal_acknowledged(params)

        elif task_type in {"change_acknowledgement", "change_accepted", "change_rejected", "change_aborted"}:
            result = _handle_change_acknowledgement(params)
            result["task_type"] = task_type

        elif task_type == "echo":
            result = _handle_echo(params)

        elif task_type == "query":
            result = await _handle_query(params, background_tasks, request_id)

        elif task_type == "counter_proposal":
            result = _handle_counter_proposal(params, background_tasks)

        elif task_type == "blocker":
            result = _handle_blocker(params, background_tasks)

        elif task_type in {"milestone_update", "milestone_status_report"}:
            result = _handle_milestone_inbound(params, task_type)

        elif task_type == "clarification_response":
            # Bank should never send this to us, but handle gracefully
            change_id = params.get("change_id")
            _append_log("inbound", task_type, change_id, summary="Unexpected clarification_response from bank (logged)")
            result = {"status": "logged", "note": "clarification_response received from bank — unexpected but noted"}

        else:
            change_id = params.get("change_id")
            _append_log("inbound", task_type, change_id, summary=f"Unrecognised task_type: {task_type}", status="ok")
            logger.info(f"[mock] ← {task_type} (no specific handler — logged)")
            result = {"status": "received", "note": f"task_type '{task_type}' received and logged by mock"}

    except Exception as exc:
        logger.exception(f"[mock] Handler error for task_type={task_type}: {exc}")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"Internal error: {exc}"}},
            status_code=500,
        )

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


# ── POST /trigger/change_communication — push a document to bank ───────────

@app.post("/trigger/change_communication", tags=["Triggers"])
async def trigger_change_communication(
    change_id: str = Form(..., description="Unique change identifier e.g. CHG-UPI-2026-001"),
    version: int = Form(1, description="Document version"),
    title: str = Form("Mock NPCI Change Communication"),
    description: str = Form("Test document pushed from Mock NPCI Agent"),
    doc_type: str = Form("circular", description="One of: brd, tech_spec, cert_test_cases, circular, faq, manifest, product_deck, product_note"),
    file: UploadFile = File(..., description="PDF or document file to push"),
) -> JSONResponse:
    """
    Trigger a change_communication to the bank (spec §2.1 Document Push).
    Reads the uploaded file, base64-encodes it, wraps it in a spec-conformant
    product_kit[], signs the envelope, and POSTs to the bank's /a2a/external.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    content_b64 = base64.b64encode(content).decode("ascii")
    filename = file.filename or "mock_document.pdf"

    payload = {
        "change_id": change_id,
        "version": version,
        "title": title,
        "description": description,
        "product_kit": [
            {
                "doc_type": doc_type,
                "filename": filename,
                "content_b64": content_b64,
                "version": version,
                "description": description,
            }
        ],
        "notify_list": [],
    }

    outbox_id = await _enqueue_change_communication(payload, change_id)

    return JSONResponse(
        {
            "status": "queued",
            "change_id": change_id,
            "filename": filename,
            "file_size_bytes": len(content),
            "bank_url": BANK_A2A_URL,
            "outbox_id": outbox_id,
        }
    )


# ── POST /trigger/echo — connectivity test ─────────────────────────────────

@app.post("/trigger/echo", tags=["Triggers"])
async def trigger_echo() -> JSONResponse:
    """Send an echo message to the bank to verify end-to-end A2A connectivity."""
    try:
        result = await _post_to_bank("echo", {"ping": "ping", "source": "mock_npci_agent"})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Echo failed: {exc}")
    return JSONResponse({"status": "echo_sent", "bank_response": result})


# ── POST /trigger/clarification_response — manual push ────────────────────

@app.post("/trigger/clarification_response", tags=["Triggers"])
async def trigger_clarification(
    change_id: str = Form(...),
    query_id: str = Form(...),
    ticket_id: str = Form(""),
    response_text: str = Form("Manual clarification from Mock NPCI Agent."),
    interpretation_confirmed: bool = Form(True),
    amends_change: bool = Form(False),
    grounded_in: str = Form("", description="Comma-separated citation refs, e.g. 'TSD §4.2.3, BRD §2.1'"),
) -> JSONResponse:
    """Manually push a clarification_response to the bank (e.g. for a specific ticket)."""
    citations = [c.strip() for c in grounded_in.split(",") if c.strip()] or ["TSD §4.2.3", "BRD §2.1"]
    payload = {
        "change_id": change_id,
        "query_id": query_id,
        "ticket_id": ticket_id or f"TKT-MOCK-{uuid.uuid4().hex[:10].upper()}",
        "response_text": response_text,
        "interpretation_confirmed": interpretation_confirmed,
        "grounded_in": citations,
        "amends_change": amends_change,
        "approver": f"mock_operator/{AGENT_ID}",
        "generation_mode": "manual",
    }
    try:
        result = await _post_to_bank("clarification_response", payload, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await asyncio.to_thread(_inbox_mark_responded, query_id)
    return JSONResponse({"status": "sent", "bank_response": result})


# ── Respond to a specific counter-proposal / blocker / milestone ───────────

@app.post("/trigger/counter_decision", tags=["Triggers"])
async def trigger_counter_decision(
    change_id: str = Form(...),
    counter_proposal_id: str = Form(...),
    clause_ref: str = Form(""),
    decision: str = Form("accepted_with_modification"),
    modified_text: str = Form(""),
    reasoning: str = Form(""),
    per_counter: str = Form("", description="Optional JSON array for multi-clause decisions"),
) -> JSONResponse:
    """Send a counter_decision to the bank for a specific counter-proposal (spec §2.4)."""
    if per_counter.strip():
        try:
            items = json.loads(per_counter)
            if not isinstance(items, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="per_counter must be a JSON array.")
    else:
        items = [{
            "clause_ref": clause_ref,
            "decision": decision,
            "modified_text": modified_text,
            "reasoning": reasoning,
        }]
    payload = {
        "change_id": change_id,
        "in_response_to": counter_proposal_id,
        "decided_at": _sqlite_now(),
        "per_counter": items,
    }
    try:
        result = await _post_to_bank("counter_decision", payload, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await asyncio.to_thread(_inbox_mark_responded, counter_proposal_id)
    return JSONResponse({"status": "sent", "bank_response": result})


@app.post("/trigger/blocker_status_update", tags=["Triggers"])
async def trigger_blocker_status_update(
    change_id: str = Form(...),
    blocker_id: str = Form(...),
    status: str = Form("triaged"),
    assigned_team: str = Form(""),
    notes: str = Form(""),
) -> JSONResponse:
    """Send a blocker_status_update to the bank (spec §2.5)."""
    payload = {
        "change_id": change_id,
        "blocker_id": blocker_id,
        "status": status,
        "updated_at": _sqlite_now(),
    }
    if assigned_team:
        payload["assigned_team"] = assigned_team
    if notes:
        payload["notes"] = notes
    try:
        result = await _post_to_bank("blocker_status_update", payload, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"status": "sent", "bank_response": result})


@app.post("/trigger/blocker_resolution", tags=["Triggers"])
async def trigger_blocker_resolution(
    change_id: str = Form(...),
    blocker_id: str = Form(...),
    resolution: str = Form("resolved"),
    resolution_text: str = Form(""),
) -> JSONResponse:
    """Send a terminal blocker_resolution to the bank (spec §2.5)."""
    payload = {
        "change_id": change_id,
        "blocker_id": blocker_id,
        "resolution": resolution,
        "resolved_at": _sqlite_now(),
    }
    if resolution_text:
        payload["resolution_text"] = resolution_text
    try:
        result = await _post_to_bank("blocker_resolution", payload, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await asyncio.to_thread(_inbox_mark_responded, blocker_id)
    return JSONResponse({"status": "sent", "bank_response": result})


@app.post("/trigger/milestone_status_request", tags=["Triggers"])
async def trigger_milestone_status_request(
    change_id: str = Form(...),
    scope: str = Form("all"),
) -> JSONResponse:
    """Poll the bank for a milestone status report (spec §2.6). Stores the reply."""
    payload = {"change_id": change_id, "scope": scope}
    try:
        result = await _post_to_bank("milestone_status_request", payload, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Bank replies synchronously with a milestone_status_report in the JSON-RPC result.
    report = None
    if isinstance(result, dict):
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        if isinstance(inner, dict) and inner.get("task_type") == "milestone_status_report":
            report = inner.get("payload")
    if isinstance(report, dict):
        await asyncio.to_thread(
            _inbox_insert,
            message_id=None, task_type="milestone_status_report",
            change_id=change_id, correlation_id=None,
            entity_id=report.get("current_milestone") or "report",
            payload=report, status="info",
        )
    return JSONResponse({"status": "sent", "bank_response": result, "report": report})


@app.post("/config/auto-reply", tags=["Config"])
async def set_auto_reply(
    auto_reply_counter: bool = Form(...),
    auto_reply_blocker: bool = Form(...),
    auto_reply_query: bool | None = Form(None),
) -> JSONResponse:
    """Toggle the manual-first auto-reply behaviour at runtime."""
    _runtime["auto_reply_counter"] = auto_reply_counter
    _runtime["auto_reply_blocker"] = auto_reply_blocker
    if auto_reply_query is not None:
        _runtime["auto_reply_query"] = auto_reply_query
    return JSONResponse({"status": "ok", **_runtime})


# ── GET /status ────────────────────────────────────────────────────────────

@app.get("/status", tags=["Health"])
async def get_status() -> JSONResponse:
    """Return current configuration and bank reachability."""
    bank_reachable = False
    bank_status_code = None
    try:
        health_url = BANK_A2A_URL.split("/a2a")[0] + "/health"
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(health_url)
        bank_reachable = r.status_code == 200
        bank_status_code = r.status_code
    except Exception:
        pass

    return JSONResponse(
        {
            "agent_id": AGENT_ID,
            "bank_a2a_url": BANK_A2A_URL,
            "hmac_configured": bool(_HMAC_SECRET),
            "jwt_configured": bool(_SECRET_KEY),
            "auto_reply": AUTO_REPLY,
            "auto_reply_delay_seconds": AUTO_REPLY_DELAY,
            "auto_reply_counter": _runtime["auto_reply_counter"],
            "auto_reply_blocker": _runtime["auto_reply_blocker"],
            "auto_reply_query": _runtime["auto_reply_query"],
            "validate_inbound_hmac": VALIDATE_INBOUND_HMAC,
            "bank_reachable": bank_reachable,
            "bank_status_code": bank_status_code,
            "messages_logged": len(_messages),
            "outbox": await asyncio.to_thread(_outbox_counts),
            "inbox": await asyncio.to_thread(_inbox_counts),
        }
    )


# ── GET /messages — log feed ───────────────────────────────────────────────

@app.get("/messages", tags=["Health"])
async def get_messages(limit: int = 100) -> JSONResponse:
    """Return the last N log entries (newest first)."""
    return JSONResponse({"messages": list(_messages)[: min(limit, _LOG_SIZE)]})


# ── GET /inbox — structured received messages ──────────────────────────────

@app.get("/inbox", tags=["Inbox"])
async def get_inbox(
    task_type: str | None = None,
    change_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> JSONResponse:
    """List received counter-proposals / blockers / milestones with full payloads."""
    items = await asyncio.to_thread(_inbox_list, task_type, change_id, status, min(limit, 500))
    return JSONResponse({"items": items})


# ── GET / — Web UI ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def serve_ui() -> HTMLResponse:
    return HTMLResponse(content=_HTML_UI)


# ── Embedded Web UI ────────────────────────────────────────────────────────
_HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Mock NPCI Agent</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:          #070b14;
      --bg-card:     #0d1424;
      --bg-elevated: #111e35;
      --bg-input:    #0a1120;
      --border:      rgba(59,130,246,.14);
      --border-dim:  rgba(255,255,255,.05);
      --accent:      #3b82f6;
      --accent-dim:  rgba(59,130,246,.12);
      --accent-glow: rgba(59,130,246,.25);
      --orange:      #f97316;
      --orange-dim:  rgba(249,115,22,.12);
      --green:       #22c55e;
      --green-dim:   rgba(34,197,94,.12);
      --red:         #ef4444;
      --red-dim:     rgba(239,68,68,.12);
      --yellow:      #eab308;
      --text:        #e2e8f0;
      --text-muted:  #94a3b8;
      --text-dim:    #4e6280;
      --radius:      12px;
      --radius-sm:   8px;
    }

    body {
      font-family: 'Inter', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      font-size: 14px;
      line-height: 1.6;
    }

    /* ── Header ── */
    .header {
      position: sticky; top: 0; z-index: 100;
      background: rgba(7,11,20,.9);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      padding: 0 2rem;
      height: 60px;
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .header-logo {
      display: flex; align-items: center; gap: .6rem;
      font-weight: 700; font-size: 15px; letter-spacing: -.3px;
    }
    .header-logo .badge {
      background: linear-gradient(135deg, #1d4ed8, #7c3aed);
      color: #fff; font-size: 10px; font-weight: 600;
      padding: 2px 8px; border-radius: 20px; letter-spacing: .5px;
      text-transform: uppercase;
    }
    .header-spacer { flex: 1; }
    .status-pill {
      display: flex; align-items: center; gap: .4rem;
      font-size: 12px; padding: 4px 12px;
      border-radius: 20px; font-weight: 500;
      border: 1px solid transparent;
      transition: all .3s;
    }
    .status-pill.ok  { background: var(--green-dim); border-color: rgba(34,197,94,.3); color: var(--green); }
    .status-pill.err { background: var(--red-dim);   border-color: rgba(239,68,68,.3); color: var(--red);   }
    .status-pill .dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: currentColor; animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

    /* ── Layout ── */
    .layout { display: grid; grid-template-columns: 260px 1fr; min-height: calc(100vh - 60px); }

    /* ── Sidebar ── */
    .sidebar {
      border-right: 1px solid var(--border);
      padding: 1.5rem 1rem;
      display: flex; flex-direction: column; gap: .25rem;
    }
    .sidebar-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
                     color: var(--text-dim); padding: .5rem .75rem .25rem; }
    .nav-item {
      display: flex; align-items: center; gap: .65rem;
      padding: .55rem .75rem; border-radius: var(--radius-sm);
      cursor: pointer; font-weight: 500; font-size: 13.5px;
      color: var(--text-muted); border: 1px solid transparent;
      transition: all .2s; user-select: none;
    }
    .nav-item:hover  { background: var(--bg-elevated); color: var(--text); }
    .nav-item.active { background: var(--accent-dim); border-color: var(--border); color: var(--accent); }
    .nav-item .icon  { font-size: 16px; width: 20px; text-align: center; }
    .nav-badge {
      margin-left: auto; background: var(--accent); color: #fff;
      font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 10px;
      min-width: 20px; text-align: center;
    }
    .sidebar-divider { height: 1px; background: var(--border-dim); margin: .75rem 0; }

    /* ── Content ── */
    .content { padding: 2rem; overflow-y: auto; }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* ── Section heading ── */
    .section-title {
      font-size: 18px; font-weight: 700; margin-bottom: .25rem;
      background: linear-gradient(135deg, #e2e8f0, #94a3b8);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .section-sub { color: var(--text-muted); font-size: 13px; margin-bottom: 1.5rem; }

    /* ── Card ── */
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.5rem;
      margin-bottom: 1.25rem;
    }
    .card-title {
      font-size: 13px; font-weight: 600; text-transform: uppercase;
      letter-spacing: .5px; color: var(--text-muted); margin-bottom: 1rem;
      display: flex; align-items: center; gap: .5rem;
    }

    /* ── Form elements ── */
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .form-grid.cols-1 { grid-template-columns: 1fr; }
    .form-grid.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
    .form-group { display: flex; flex-direction: column; gap: .4rem; }
    .form-group.span-2 { grid-column: span 2; }
    label { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .4px; }
    input, select, textarea {
      background: var(--bg-input); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text);
      padding: .6rem .85rem; font-size: 13.5px; font-family: inherit;
      outline: none; transition: border-color .2s, box-shadow .2s;
      width: 100%;
    }
    input:focus, select:focus, textarea:focus {
      border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow);
    }
    textarea { resize: vertical; min-height: 80px; }
    select option { background: #0d1424; }

    /* ── File drop zone ── */
    .drop-zone {
      border: 2px dashed var(--border);
      border-radius: var(--radius);
      padding: 2.5rem 1.5rem;
      text-align: center;
      cursor: pointer;
      transition: all .25s;
      position: relative;
      overflow: hidden;
    }
    .drop-zone:hover, .drop-zone.drag-over {
      border-color: var(--accent); background: var(--accent-dim);
    }
    .drop-zone input[type="file"] {
      position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
    }
    .drop-zone .icon { font-size: 36px; margin-bottom: .5rem; }
    .drop-zone .text { color: var(--text-muted); font-size: 13px; }
    .drop-zone .filename { color: var(--accent); font-weight: 600; margin-top: .5rem; font-size: 13px; }

    /* ── Buttons ── */
    .btn {
      display: inline-flex; align-items: center; gap: .5rem;
      padding: .65rem 1.25rem; border-radius: var(--radius-sm);
      font-size: 13.5px; font-weight: 600; cursor: pointer;
      border: 1px solid transparent; transition: all .2s;
      font-family: inherit;
    }
    .btn-primary {
      background: linear-gradient(135deg, #1d4ed8, #4f46e5);
      color: #fff; border-color: rgba(99,102,241,.3);
    }
    .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 20px var(--accent-glow); }
    .btn-primary:active { transform: translateY(0); }
    .btn-primary:disabled { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }
    .btn-ghost { background: var(--bg-elevated); color: var(--text-muted); border-color: var(--border); }
    .btn-ghost:hover { color: var(--text); border-color: var(--accent); }
    .btn-danger { background: var(--red-dim); color: var(--red); border-color: rgba(239,68,68,.3); }
    .btn-success { background: var(--green-dim); color: var(--green); border-color: rgba(34,197,94,.3); }

    /* ── Config stat tiles ── */
    .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .75rem; }
    .stat-tile {
      background: var(--bg-elevated); border: 1px solid var(--border);
      border-radius: var(--radius-sm); padding: 1rem;
    }
    .stat-tile .label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .4rem; }
    .stat-tile .value { font-size: 16px; font-weight: 700; }
    .stat-tile .value.ok  { color: var(--green); }
    .stat-tile .value.err { color: var(--red); }
    .stat-tile .value.warn { color: var(--yellow); }
    .stat-tile .value.info { color: var(--accent); }

    /* ── Message log ── */
    .log-controls { display: flex; align-items: center; gap: .75rem; margin-bottom: 1rem; }
    .log-filter { display: flex; gap: .4rem; flex-wrap: wrap; }
    .filter-chip {
      font-size: 11px; font-weight: 600; padding: 3px 10px;
      border-radius: 20px; cursor: pointer; border: 1px solid var(--border);
      background: var(--bg-elevated); color: var(--text-muted);
      transition: all .2s; letter-spacing: .3px;
    }
    .filter-chip:hover { border-color: var(--accent); color: var(--text); }
    .filter-chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .auto-refresh { display: flex; align-items: center; gap: .4rem; font-size: 12px; color: var(--text-muted); margin-left: auto; }
    .auto-refresh input[type=checkbox] { width:auto; }

    .message-list { display: flex; flex-direction: column; gap: .5rem; }
    .message-item {
      background: var(--bg-card); border: 1px solid var(--border-dim);
      border-radius: var(--radius-sm); padding: .85rem 1rem;
      display: grid; grid-template-columns: 80px 130px 110px 1fr 90px;
      align-items: start; gap: .75rem;
      transition: border-color .2s;
      cursor: default;
    }
    .message-item:hover { border-color: var(--border); }
    .message-item .ts { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-dim); }
    .dir-badge {
      font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px;
      text-transform: uppercase; letter-spacing: .5px; display: inline-block;
    }
    .dir-badge.inbound  { background: var(--green-dim); color: var(--green); }
    .dir-badge.outbound { background: var(--orange-dim); color: var(--orange); }
    .task-type-badge {
      font-family: 'JetBrains Mono', monospace; font-size: 11px;
      color: var(--accent); font-weight: 500;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .msg-summary { font-size: 12.5px; color: var(--text-muted); overflow: hidden; }
    .status-badge {
      font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px;
      text-align: center; text-transform: uppercase; letter-spacing: .3px;
    }
    .status-badge.ok      { background: var(--green-dim); color: var(--green); }
    .status-badge.error   { background: var(--red-dim);   color: var(--red);   }
    .status-badge.rejected{ background: var(--red-dim);   color: var(--red);   }
    .status-badge.pending { background: var(--yellow-dim, rgba(234,179,8,.12)); color: var(--yellow); }
    .empty-state { text-align: center; padding: 4rem 2rem; color: var(--text-dim); }
    .empty-state .icon { font-size: 3rem; margin-bottom: 1rem; opacity: .4; }

    /* ── Toast ── */
    .toast-container { position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 9999; display: flex; flex-direction: column; gap: .5rem; }
    .toast {
      display: flex; align-items: flex-start; gap: .75rem;
      background: var(--bg-elevated); border: 1px solid var(--border);
      border-radius: var(--radius-sm); padding: .85rem 1rem;
      min-width: 280px; max-width: 380px;
      animation: slideIn .3s ease;
      box-shadow: 0 8px 32px rgba(0,0,0,.5);
    }
    .toast.success { border-color: rgba(34,197,94,.4); }
    .toast.error   { border-color: rgba(239,68,68,.4);  }
    .toast .toast-icon { font-size: 16px; margin-top: 1px; }
    .toast .toast-title { font-weight: 600; font-size: 13px; margin-bottom: 2px; }
    .toast .toast-body  { font-size: 12px; color: var(--text-muted); }
    @keyframes slideIn { from{opacity:0;transform:translateX(1rem)} to{opacity:1;transform:none} }

    /* ── Spinner ── */
    .spinner { width:16px;height:16px;border:2px solid rgba(255,255,255,.2);border-top-color:#fff;
               border-radius:50%;animation:spin .6s linear infinite;display:inline-block; }
    @keyframes spin { to{transform:rotate(360deg)} }

    /* ── Scrollbar ── */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    /* ── Inbox ── */
    .inbox-toolbar {
      display: flex; align-items: center; gap: .75rem; flex-wrap: wrap;
      margin-bottom: 1.25rem;
    }
    .inbox-summary { display: flex; gap: .5rem; flex-wrap: wrap; }
    .summary-pill {
      display: flex; align-items: center; gap: .45rem;
      font-size: 12px; font-weight: 600; padding: .4rem .8rem;
      border-radius: 20px; border: 1px solid var(--border);
      background: var(--bg-elevated); color: var(--text-muted);
    }
    .summary-pill .count {
      font-weight: 700; color: var(--text);
      background: var(--accent-dim); border-radius: 10px;
      padding: 0 .5rem; min-width: 20px; text-align: center;
    }
    .summary-pill.alert .count { background: var(--orange-dim); color: var(--orange); }
    .summary-pill.alert { border-color: rgba(249,115,22,.3); }

    /* Inbox item cards */
    .ibx {
      background: var(--bg-card); border: 1px solid var(--border);
      border-left: 3px solid var(--border);
      border-radius: var(--radius); padding: 1.15rem 1.25rem;
      margin-bottom: .9rem; transition: border-color .2s, opacity .2s;
    }
    .ibx.new { border-left-color: var(--accent); }
    .ibx.new.blocker-crit { border-left-color: var(--red); }
    .ibx.new.blocker-high { border-left-color: var(--orange); }
    .ibx.done { opacity: .55; border-left-color: var(--green); }
    .ibx.done:hover { opacity: 1; }
    .ibx-head {
      display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
      margin-bottom: .35rem;
    }
    .ibx-id { font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 13.5px; color: var(--text); }
    .ibx-meta { margin-left: auto; font-size: 11px; color: var(--text-dim); display: flex; gap: .75rem; align-items: center; }
    .ibx-summary { font-size: 13px; color: var(--text-muted); margin-bottom: .75rem; }

    /* severity chips */
    .chip {
      font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px;
      text-transform: uppercase; letter-spacing: .3px;
    }
    .chip.mandatory, .chip.sev-critical { background: var(--red-dim); color: var(--red); }
    .chip.sev-high    { background: var(--orange-dim); color: var(--orange); }
    .chip.sev-medium  { background: rgba(234,179,8,.12); color: var(--yellow); }
    .chip.optional, .chip.sev-low { background: var(--accent-dim); color: var(--accent); }
    .chip.type { background: var(--bg-elevated); color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }

    /* clause rows */
    .clause {
      background: var(--bg-input); border: 1px solid var(--border-dim);
      border-radius: var(--radius-sm); padding: .85rem; margin-bottom: .6rem;
    }
    .clause-head { display: flex; align-items: center; gap: .5rem; margin-bottom: .5rem; }
    .clause-ref { font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 12.5px; color: var(--text); }
    .textline { font-size: 12.5px; line-height: 1.5; margin: .2rem 0; padding-left: .65rem; border-left: 2px solid var(--border); }
    .textline .tl-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .4px; color: var(--text-dim); display: block; }
    .textline.current .tl-label { color: var(--text-dim); }
    .textline.proposed { border-left-color: var(--accent); }
    .textline.proposed .tl-label { color: var(--accent); }
    .textline.rationale { border-left-color: var(--orange); color: var(--text-muted); font-style: italic; }
    .textline.rationale .tl-label { color: var(--orange); font-style: normal; }

    .clause-decision { display: flex; gap: .6rem; align-items: flex-end; flex-wrap: wrap; margin-top: .65rem; }
    .clause-decision .form-group { gap: .25rem; }
    .clause-decision label { font-size: 10px; }
    .clause-decision select, .clause-decision input { padding: .45rem .6rem; font-size: 12.5px; }
    .clause-decision select { min-width: 190px; }
    .mini { padding: .45rem .9rem; font-size: 12px; }

    .ibx-actions { display: flex; gap: .6rem; align-items: flex-end; flex-wrap: wrap; margin-top: .75rem; padding-top: .75rem; border-top: 1px solid var(--border-dim); }
    .action-group { display: flex; gap: .5rem; align-items: flex-end; flex-wrap: wrap; padding: .55rem .7rem; background: var(--bg-input); border: 1px solid var(--border-dim); border-radius: var(--radius-sm); }
    .action-group .ag-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .4px; color: var(--text-dim); align-self: center; margin-right: .1rem; }

    .raw-toggle { font-size: 11px; color: var(--text-dim); cursor: pointer; user-select: none; }
    .raw-toggle:hover { color: var(--accent); }
    .raw-json { display: none; margin-top: .6rem; background: #060a12; border: 1px solid var(--border-dim); border-radius: var(--radius-sm); padding: .7rem .85rem; font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-muted); white-space: pre-wrap; max-height: 260px; overflow: auto; }
    .raw-json.open { display: block; }

    .bulk-bar { display: flex; align-items: center; gap: .5rem; margin-bottom: .6rem; font-size: 11px; color: var(--text-dim); }
    .bulk-bar select { width: auto; padding: .3rem .5rem; font-size: 11.5px; }
  </style>
</head>
<body>

<!-- Header -->
<header class="header">
  <div class="header-logo">
    <span>🔵</span>
    <span>Mock NPCI Agent</span>
    <span class="badge">STUB MODE</span>
  </div>
  <div class="header-spacer"></div>
  <div id="bankStatus" class="status-pill err">
    <span class="dot"></span>
    <span id="bankStatusText">Checking…</span>
  </div>
</header>

<!-- Layout -->
<div class="layout">
  <!-- Sidebar nav -->
  <aside class="sidebar">
    <div class="sidebar-label">Navigation</div>
    <div class="nav-item active" onclick="switchTab('dashboard')" id="nav-dashboard">
      <span class="icon">📊</span> Dashboard
    </div>
    <div class="nav-item" onclick="switchTab('push')" id="nav-push">
      <span class="icon">📤</span> Push Document
    </div>
    <div class="nav-item" onclick="switchTab('reply')" id="nav-reply">
      <span class="icon">💬</span> Manual Reply
    </div>
    <div class="nav-item" onclick="switchTab('inbox')" id="nav-inbox">
      <span class="icon">📥</span> Inbox
      <span class="nav-badge" id="inboxBadge">0</span>
    </div>
    <div class="nav-item" onclick="switchTab('log')" id="nav-log">
      <span class="icon">📋</span> Message Log
      <span class="nav-badge" id="logBadge">0</span>
    </div>
    <div class="sidebar-divider"></div>
    <div class="sidebar-label">Config</div>
    <div class="nav-item" onclick="switchTab('config')" id="nav-config">
      <span class="icon">⚙️</span> Configuration
    </div>
    <div style="margin-top:auto;padding-top:1rem;">
      <div class="nav-item" onclick="triggerEcho()" id="nav-echo">
        <span class="icon">🔊</span> Test Echo
      </div>
    </div>
  </aside>

  <!-- Content area -->
  <main class="content">

    <!-- Dashboard -->
    <div class="tab-panel active" id="tab-dashboard">
      <div class="section-title">Dashboard</div>
      <div class="section-sub">Live status of the Mock NPCI Agent and the bank connection.</div>

      <div class="stat-grid" id="statGrid">
        <div class="stat-tile"><div class="label">Agent ID</div><div class="value info" id="st-agentId">—</div></div>
        <div class="stat-tile"><div class="label">Bank URL</div><div class="value info" style="font-size:11px;word-break:break-all" id="st-bankUrl">—</div></div>
        <div class="stat-tile"><div class="label">Bank Reachable</div><div class="value err" id="st-bankReachable">—</div></div>
        <div class="stat-tile"><div class="label">HMAC Secret</div><div class="value" id="st-hmac">—</div></div>
        <div class="stat-tile"><div class="label">JWT Secret</div><div class="value" id="st-jwt">—</div></div>
        <div class="stat-tile"><div class="label">Auto-Reply</div><div class="value" id="st-autoReply">—</div></div>
        <div class="stat-tile"><div class="label">Reply Delay</div><div class="value info" id="st-delay">—</div></div>
        <div class="stat-tile"><div class="label">Messages Logged</div><div class="value info" id="st-msgCount">—</div></div>
      </div>

      <div class="card" style="margin-top:1.5rem">
        <div class="card-title">📝 Recent Activity</div>
        <div class="message-list" id="dashRecentList">
          <div class="empty-state"><div class="icon">📭</div>No messages yet</div>
        </div>
      </div>
    </div>

    <!-- Push Document -->
    <div class="tab-panel" id="tab-push">
      <div class="section-title">Push Document</div>
      <div class="section-sub">Trigger a <code style="color:var(--accent)">change_communication</code> to the bank's <code style="color:var(--orange)">/a2a/external</code> endpoint (spec §2.1).</div>

      <div class="card">
        <div class="card-title">📎 Document Details</div>
        <form id="pushForm">
          <div class="form-grid">
            <div class="form-group">
              <label>Change ID *</label>
              <input id="p-changeId" name="change_id" type="text" placeholder="CHG-UPI-2026-001" required />
            </div>
            <div class="form-group">
              <label>Version</label>
              <input id="p-version" name="version" type="number" value="1" min="1" />
            </div>
            <div class="form-group span-2">
              <label>Title</label>
              <input id="p-title" name="title" type="text" value="Mock NPCI Change Communication" />
            </div>
            <div class="form-group">
              <label>Document Type</label>
              <select id="p-docType" name="doc_type">
                <option value="circular">Circular</option>
                <option value="brd">BRD</option>
                <option value="tech_spec">Tech Spec</option>
                <option value="cert_test_cases">Cert Test Cases</option>
                <option value="faq">FAQ</option>
                <option value="manifest">Manifest</option>
                <option value="product_deck">Product Deck</option>
                <option value="product_note">Product Note</option>
              </select>
            </div>
            <div class="form-group">
              <label>Description</label>
              <input id="p-description" name="description" type="text" value="Test document from Mock NPCI Agent" />
            </div>
          </div>

          <div class="drop-zone" id="dropZone" style="margin-top:1.25rem">
            <input type="file" id="p-file" name="file" accept=".pdf,.txt,.docx" required onchange="onFileSelected(this)" />
            <div class="icon">📄</div>
            <div class="text">Drag &amp; drop a PDF here, or <strong>click to browse</strong></div>
            <div class="filename" id="selectedFileName"></div>
          </div>

          <div style="display:flex;gap:.75rem;margin-top:1.25rem">
            <button type="submit" class="btn btn-primary" id="pushBtn">
              <span>📤</span> Push to Bank
            </button>
            <button type="button" class="btn btn-ghost" onclick="document.getElementById('pushForm').reset();document.getElementById('selectedFileName').textContent=''">
              Clear
            </button>
          </div>
        </form>
      </div>

      <div class="card" id="pushResponseCard" style="display:none">
        <div class="card-title">🔄 Bank Response</div>
        <pre id="pushResponse" style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-muted);white-space:pre-wrap;word-break:break-word;"></pre>
      </div>
    </div>

    <!-- Manual Reply -->
    <div class="tab-panel" id="tab-reply">
      <div class="section-title">Manual Clarification Reply</div>
      <div class="section-sub">Push a <code style="color:var(--accent)">clarification_response</code> manually for a specific ticket (overrides auto-reply).</div>

      <div class="card">
        <form id="replyForm">
          <div class="form-grid">
            <div class="form-group">
              <label>Change ID *</label>
              <input id="r-changeId" name="change_id" type="text" placeholder="CHG-UPI-2026-001" required />
            </div>
            <div class="form-group">
              <label>Query ID *</label>
              <input id="r-queryId" name="query_id" type="text" placeholder="Q-ABC123456789" required />
            </div>
            <div class="form-group">
              <label>Ticket ID</label>
              <input id="r-ticketId" name="ticket_id" type="text" placeholder="TKT-MOCK-… (auto-generated if blank)" />
            </div>
            <div class="form-group">
              <label>Interpretation Confirmed</label>
              <select name="interpretation_confirmed" id="r-confirmed">
                <option value="true">Yes — confirmed</option>
                <option value="false">No — disputed</option>
              </select>
            </div>
            <div class="form-group span-2">
              <label>Response Text *</label>
              <textarea id="r-responseText" name="response_text" rows="4" required>Manual clarification from Mock NPCI Agent. The bank's interpretation is confirmed per TSD §4.2.3.</textarea>
            </div>
          </div>
          <button type="submit" class="btn btn-primary" id="replyBtn" style="margin-top:1rem">
            <span>💬</span> Send Clarification
          </button>
        </form>
      </div>

      <div class="card" id="replyResponseCard" style="display:none">
        <div class="card-title">🔄 Bank Response</div>
        <pre id="replyResponse" style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-muted);white-space:pre-wrap;word-break:break-word;"></pre>
      </div>
    </div>

    <!-- Message Log -->
    <!-- Inbox: view & respond to received counter-proposals / blockers / milestones -->
    <div class="tab-panel" id="tab-inbox">
      <div class="section-title">Inbox — View &amp; Respond</div>
      <div class="section-sub">Counter-proposals, blockers and milestones the bank sent. Decide each clause independently, then send. Persisted to SQLite (survives restart).</div>

      <div class="inbox-toolbar">
        <div class="inbox-summary">
          <span class="summary-pill alert" id="sumQ"><span class="count">0</span> queries</span>
          <span class="summary-pill alert" id="sumCp"><span class="count">0</span> counter-proposals</span>
          <span class="summary-pill alert" id="sumBlk"><span class="count">0</span> blockers</span>
          <span class="summary-pill" id="sumMs"><span class="count">0</span> milestones</span>
        </div>
        <div class="auto-refresh" style="margin-left:auto">
          <input type="checkbox" id="showRespondedToggle" onchange="renderInbox()" />
          <label for="showRespondedToggle">Show responded</label>
        </div>
        <button class="btn btn-ghost mini" onclick="loadInbox()">↻ Refresh</button>
      </div>

      <div class="card">
        <div class="card-title">❓ Queries from Bank
          <label style="margin-left:auto;display:flex;gap:.4rem;align-items:center;font-size:11px;text-transform:none;letter-spacing:0;cursor:pointer">
            <input type="checkbox" id="arQuery" onchange="saveAutoReply()" style="width:auto" /> auto-reply
          </label>
        </div>
        <div id="qList"><div class="empty-state"><div class="icon">📭</div>No queries received yet.</div></div>
      </div>

      <div class="card" style="margin-top:1rem">
        <div class="card-title">📝 Counter-Proposals</div>
        <div id="cpList"><div class="empty-state"><div class="icon">📭</div>None received yet.</div></div>
      </div>

      <div class="card" style="margin-top:1rem">
        <div class="card-title">🚧 Blockers</div>
        <div id="blkList"><div class="empty-state"><div class="icon">📭</div>None received yet.</div></div>
      </div>

      <div class="card" style="margin-top:1rem">
        <div class="card-title">🎯 Milestones</div>
        <form id="msReqForm" style="display:flex;gap:.5rem;align-items:flex-end;flex-wrap:wrap;margin-bottom:1rem">
          <div class="form-group" style="flex:1;min-width:200px">
            <label>Change ID</label>
            <input name="change_id" type="text" placeholder="CHG-UPI-2026-001" required />
          </div>
          <div class="form-group">
            <label>Scope</label>
            <select name="scope"><option value="all">all</option><option value="current_only">current_only</option><option value="with_risks">with_risks</option></select>
          </div>
          <button type="submit" class="btn btn-primary">Request status</button>
        </form>
        <div id="msList"><div class="empty-state"><div class="icon">📭</div>No milestone updates yet.</div></div>
      </div>
    </div>

    <div class="tab-panel" id="tab-log">
      <div class="section-title">Message Log</div>
      <div class="section-sub">All A2A messages exchanged (inbound from bank + outbound to bank). In-memory; resets on restart.</div>

      <div class="log-controls">
        <div class="log-filter">
          <span class="filter-chip active" onclick="setFilter('all',this)">All</span>
          <span class="filter-chip" onclick="setFilter('inbound',this)">Inbound</span>
          <span class="filter-chip" onclick="setFilter('outbound',this)">Outbound</span>
          <span class="filter-chip" onclick="setFilter('error',this)">Errors</span>
        </div>
        <div class="auto-refresh">
          <input type="checkbox" id="autoRefreshToggle" checked onchange="toggleAutoRefresh(this.checked)" />
          <label for="autoRefreshToggle">Auto-refresh</label>
        </div>
        <button class="btn btn-ghost" onclick="loadMessages()" style="padding:.4rem .8rem;font-size:12px">↻ Refresh</button>
        <button class="btn btn-danger" onclick="clearLog()" style="padding:.4rem .8rem;font-size:12px">🗑 Clear</button>
      </div>

      <div class="message-list" id="messageList">
        <div class="empty-state"><div class="icon">📭</div>No messages yet. Send an echo or push a document to begin.</div>
      </div>
    </div>

    <!-- Config -->
    <div class="tab-panel" id="tab-config">
      <div class="section-title">Configuration</div>
      <div class="section-sub">Current agent configuration loaded from <code style="color:var(--accent)">.env</code>. Restart the process to pick up changes.</div>

      <div class="card">
        <div class="card-title">🔐 Auth Stack</div>
        <div class="stat-grid" id="configGrid" style="margin-bottom:0">
          <!-- Filled by JS -->
        </div>
      </div>

      <div class="card" style="margin-top:1rem">
        <div class="card-title">📋 Quick Setup (bank .env)</div>
        <pre id="envSnippet" style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-muted);
             background:var(--bg-input);padding:1rem;border-radius:var(--radius-sm);border:1px solid var(--border);white-space:pre-wrap;"></pre>
        <button class="btn btn-ghost" onclick="copyEnv()" style="margin-top:.75rem;font-size:12px">📋 Copy to clipboard</button>
      </div>

      <div class="card" style="margin-top:1rem">
        <div class="card-title">🤖 Auto-Reply (manual-first)</div>
        <div class="section-sub">When ON, the mock auto-responds to received counter-proposals / blockers after the reply delay. Default OFF so you can respond by hand from the Inbox.</div>
        <label style="display:flex;gap:.5rem;align-items:center;margin:.6rem 0">
          <input type="checkbox" id="arCounter" onchange="saveAutoReply()" /> Auto-decide counter-proposals (accepted_with_modification)
        </label>
        <label style="display:flex;gap:.5rem;align-items:center">
          <input type="checkbox" id="arBlocker" onchange="saveAutoReply()" /> Auto-resolve blockers (triaged → resolved)
        </label>
      </div>
    </div>

  </main>
</div>

<!-- Toast container -->
<div class="toast-container" id="toastContainer"></div>

<script>
  // ── State ──
  let currentFilter = 'all';
  let allMessages = [];
  let autoRefreshEnabled = true;
  let refreshTimer = null;
  let statusData = null;

  // ── Tab switching ──
  function switchTab(id) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('tab-'+id).classList.add('active');
    document.getElementById('nav-'+id).classList.add('active');
  }

  // ── Toast notifications ──
  function toast(title, body, type='success') {
    const c = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `
      <span class="toast-icon">${type === 'success' ? '✅' : '❌'}</span>
      <div><div class="toast-title">${title}</div><div class="toast-body">${body}</div></div>
    `;
    c.appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  // ── Status polling ──
  async function loadStatus() {
    try {
      const r = await fetch('/status');
      statusData = await r.json();
      updateStatusUI(statusData);
    } catch(e) { /* ignore */ }
  }

  function updateStatusUI(d) {
    const pill = document.getElementById('bankStatus');
    const txt  = document.getElementById('bankStatusText');
    if (d.bank_reachable) {
      pill.className='status-pill ok'; txt.textContent='Bank Connected';
    } else {
      pill.className='status-pill err'; txt.textContent='Bank Unreachable';
    }
    // Dashboard stats
    const set = (id,val,cls) => {
      const el=document.getElementById(id);
      if(el){el.textContent=val;if(cls)el.className='value '+cls;}
    };
    set('st-agentId', d.agent_id, 'info');
    set('st-bankUrl', d.bank_a2a_url, '');
    set('st-bankReachable', d.bank_reachable ? '✓ Yes' : '✗ No', d.bank_reachable?'ok':'err');
    set('st-hmac', d.hmac_configured ? '✓ Set' : '✗ Missing', d.hmac_configured?'ok':'err');
    set('st-jwt',  d.jwt_configured  ? '✓ Set' : '✗ Missing', d.jwt_configured?'ok':'err');
    set('st-autoReply', d.auto_reply ? '✓ On' : 'Off', d.auto_reply?'ok':'warn');
    set('st-delay', d.auto_reply_delay_seconds + 's', 'info');
    set('st-msgCount', d.messages_logged, 'info');
    // Auto-reply toggles + inbox badge
    const arc = document.getElementById('arCounter'); if (arc) arc.checked = !!d.auto_reply_counter;
    const arb = document.getElementById('arBlocker'); if (arb) arb.checked = !!d.auto_reply_blocker;
    const arq = document.getElementById('arQuery'); if (arq) arq.checked = !!d.auto_reply_query;
    if (d.inbox) {
      const b = document.getElementById('inboxBadge');
      if (b) b.textContent = (d.inbox.open_counter_proposals || 0) + (d.inbox.open_blockers || 0) + (d.inbox.open_queries || 0);
    }
    // Config grid
    const cg = document.getElementById('configGrid');
    if(cg) {
      cg.innerHTML = `
        <div class="stat-tile"><div class="label">Agent ID</div><div class="value info">${d.agent_id}</div></div>
        <div class="stat-tile"><div class="label">Bank A2A URL</div><div class="value info" style="font-size:11px;word-break:break-all">${d.bank_a2a_url}</div></div>
        <div class="stat-tile"><div class="label">Bank Reachable</div><div class="value ${d.bank_reachable?'ok':'err'}">${d.bank_reachable?'✓ Yes':'✗ No'}</div></div>
        <div class="stat-tile"><div class="label">HMAC Secret</div><div class="value ${d.hmac_configured?'ok':'err'}">${d.hmac_configured?'✓ Configured':'✗ Not set'}</div></div>
        <div class="stat-tile"><div class="label">JWT Secret</div><div class="value ${d.jwt_configured?'ok':'err'}">${d.jwt_configured?'✓ Configured':'✗ Not set'}</div></div>
        <div class="stat-tile"><div class="label">Auto-Reply</div><div class="value ${d.auto_reply?'ok':'warn'}">${d.auto_reply?'✓ Enabled':'Disabled'}</div></div>
        <div class="stat-tile"><div class="label">Reply Delay</div><div class="value info">${d.auto_reply_delay_seconds}s</div></div>
        <div class="stat-tile"><div class="label">Validate Inbound HMAC</div><div class="value ${d.validate_inbound_hmac?'warn':'info'}">${d.validate_inbound_hmac?'Enabled':'Disabled'}</div></div>
      `;
    }
    // Env snippet
    const es = document.getElementById('envSnippet');
    if(es) {
      es.textContent = [
        '# Add these to the bank\\'s .env for local dev:',
        `NPCI_AGENT_URL=http://localhost:8001/a2a`,
        `A2A_MTLS_REQUIRED=false`,
        `A2A_JWT_REQUIRED_EXTERNAL=true`,
        `# A2A_KNOWN_AGENTS must contain: ${d.agent_id}`,
        `A2A_KNOWN_AGENTS=${d.agent_id},npci_cert_lab`,
        `# BANK_A2A_URL used by mock to call back:`,
        `BANK_A2A_URL=${window.location.protocol}//${window.location.hostname}:8000/a2a/external`,
      ].join('\\n');
    }
  }

  function copyEnv() {
    const t = document.getElementById('envSnippet').textContent;
    navigator.clipboard.writeText(t).then(() => toast('Copied!', 'Env snippet copied to clipboard'));
  }

  // ── Messages ──
  async function loadMessages() {
    try {
      const r = await fetch('/messages?limit=200');
      const d = await r.json();
      allMessages = d.messages || [];
      document.getElementById('logBadge').textContent = allMessages.length;
      renderMessages();
      renderDashRecent();
    } catch(e) { /* ignore */ }
  }

  function renderMessages() {
    const list = document.getElementById('messageList');
    const filtered = currentFilter === 'all' ? allMessages
      : currentFilter === 'error' ? allMessages.filter(m => m.status === 'error' || m.status === 'rejected')
      : allMessages.filter(m => m.direction === currentFilter);

    if (!filtered.length) {
      list.innerHTML = '<div class="empty-state"><div class="icon">📭</div>No messages match the current filter.</div>';
      return;
    }
    list.innerHTML = filtered.map(m => `
      <div class="message-item">
        <span class="ts">${istClock(m.ts)}</span>
        <span class="dir-badge ${m.direction}">${m.direction === 'inbound' ? '← IN' : '→ OUT'}</span>
        <span class="task-type-badge">${m.task_type}</span>
        <div class="msg-summary">
          <div>${m.summary}</div>
          <div style="color:var(--text-dim);font-size:11px;margin-top:2px">change_id: ${m.change_id}</div>
        </div>
        <span class="status-badge ${m.status}">${m.status}</span>
      </div>
    `).join('');
  }

  function renderDashRecent() {
    const list = document.getElementById('dashRecentList');
    const recent = allMessages.slice(0, 5);
    if (!recent.length) {
      list.innerHTML = '<div class="empty-state"><div class="icon">📭</div>No messages yet</div>';
      return;
    }
    list.innerHTML = recent.map(m => `
      <div class="message-item">
        <span class="ts">${istClock(m.ts)}</span>
        <span class="dir-badge ${m.direction}">${m.direction === 'inbound' ? '← IN' : '→ OUT'}</span>
        <span class="task-type-badge">${m.task_type}</span>
        <div class="msg-summary">${m.summary}</div>
        <span class="status-badge ${m.status}">${m.status}</span>
      </div>
    `).join('');
  }

  function setFilter(f, el) {
    currentFilter = f;
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    renderMessages();
  }

  function clearLog() {
    allMessages = [];
    document.getElementById('logBadge').textContent = '0';
    renderMessages();
    renderDashRecent();
  }

  function toggleAutoRefresh(on) {
    autoRefreshEnabled = on;
    if (on) startAutoRefresh(); else stopAutoRefresh();
  }

  function startAutoRefresh() {
    stopAutoRefresh();
    refreshTimer = setInterval(() => { loadStatus(); loadMessages(); loadInbox(); }, 2500);
  }
  function stopAutoRefresh() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }

  // ── File drop zone ──
  function onFileSelected(input) {
    const name = input.files[0]?.name || '';
    document.getElementById('selectedFileName').textContent = name ? '📎 ' + name : '';
  }
  const dz = document.getElementById('dropZone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => { dz.classList.remove('drag-over'); });

  // ── Push Document form ──
  document.getElementById('pushForm').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = document.getElementById('pushBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Pushing…';
    try {
      const fd = new FormData(e.target);
      const r = await fetch('/trigger/change_communication', { method:'POST', body:fd });
      const d = await r.json();
      if (r.ok) {
        toast('Document pushed!', `change_id: ${d.change_id} | ${d.file_size_bytes} bytes`);
        document.getElementById('pushResponse').textContent = JSON.stringify(d, null, 2);
        document.getElementById('pushResponseCard').style.display = 'block';
      } else {
        toast('Push failed', d.detail || 'Unknown error', 'error');
        document.getElementById('pushResponse').textContent = JSON.stringify(d, null, 2);
        document.getElementById('pushResponseCard').style.display = 'block';
      }
    } catch(ex) {
      toast('Network error', ex.message, 'error');
    }
    btn.disabled = false;
    btn.innerHTML = '<span>📤</span> Push to Bank';
    loadMessages();
  });

  // ── Manual reply form ──
  document.getElementById('replyForm').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = document.getElementById('replyBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Sending…';
    try {
      const fd = new FormData(e.target);
      const r = await fetch('/trigger/clarification_response', { method:'POST', body:fd });
      const d = await r.json();
      if (r.ok) {
        toast('Clarification sent!', `Response sent to bank for query ${fd.get('query_id')}`);
        document.getElementById('replyResponse').textContent = JSON.stringify(d, null, 2);
        document.getElementById('replyResponseCard').style.display = 'block';
      } else {
        toast('Failed', d.detail || 'Error', 'error');
      }
    } catch(ex) {
      toast('Network error', ex.message, 'error');
    }
    btn.disabled = false;
    btn.innerHTML = '<span>💬</span> Send Clarification';
    loadMessages();
  });

  // ── Echo test ──
  async function triggerEcho() {
    const nav = document.getElementById('nav-echo');
    nav.innerHTML = '<span class="spinner"></span> Testing…';
    try {
      const r = await fetch('/trigger/echo', { method:'POST' });
      const d = await r.json();
      if (r.ok) toast('Echo OK!', 'Bank responded: ' + JSON.stringify(d.bank_response?.result || {}));
      else toast('Echo failed', d.detail || '', 'error');
    } catch(ex) {
      toast('Echo error', ex.message, 'error');
    }
    nav.innerHTML = '<span class="icon">🔊</span> Test Echo';
    loadMessages();
  }

  // ── Inbox: view & respond ──
  let inboxData = {};
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function emptyHtml(msg){ return `<div class="empty-state"><div class="icon">📭</div>${msg}</div>`; }

  async function loadInbox(){
    try {
      const r = await fetch('/inbox?limit=200');
      const d = await r.json();
      inboxData = {};
      for (const it of (d.items || [])) { (inboxData[it.task_type] = inboxData[it.task_type] || []).push(it); }
      renderInbox();
    } catch(e) { /* ignore */ }
  }

  // sort helper: new/actionable first, then by most recent
  function bySortKey(a, b){
    const an = (a.status === 'responded') ? 1 : 0;
    const bn = (b.status === 'responded') ? 1 : 0;
    if (an !== bn) return an - bn;
    return String(b.ts || '').localeCompare(String(a.ts || ''));
  }
  // Timestamps are stored/sent in UTC (correct for the A2A wire format); display them in IST.
  function _istDate(ts){ if(!ts) return null; const d = new Date(ts); return isNaN(d) ? null : d; }
  function fmtTs(ts){
    const d = _istDate(ts);
    if(!d) return (ts || '').replace('T', ' ').slice(0, 19);
    const s = d.toLocaleString('en-GB', { timeZone:'Asia/Kolkata', year:'numeric', month:'2-digit',
      day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });
    return s.replace(',', '') + ' IST';
  }
  function istClock(ts){
    const d = _istDate(ts);
    if(!d) return (String(ts).split('T')[1] || '').replace('Z', '');
    return d.toLocaleTimeString('en-GB', { timeZone:'Asia/Kolkata', hour12:false });
  }
  function showResponded(){ const el = document.getElementById('showRespondedToggle'); return el && el.checked; }

  function renderInbox(){
    const qAll = (inboxData.query || []).slice().sort(bySortKey);
    const cpAll = (inboxData.counter_proposal || []).slice().sort(bySortKey);
    const blkAll = (inboxData.blocker || []).slice().sort(bySortKey);
    const ms = (inboxData.milestone_update || []).concat(inboxData.milestone_status_report || []);

    const sr = showResponded();
    const q = sr ? qAll : qAll.filter(x => x.status !== 'responded');
    const cp = sr ? cpAll : cpAll.filter(x => x.status !== 'responded');
    const blk = sr ? blkAll : blkAll.filter(x => x.status !== 'responded');

    // summary pills reflect OPEN counts
    const openQ = qAll.filter(x => x.status !== 'responded').length;
    const openCp = cpAll.filter(x => x.status !== 'responded').length;
    const openBlk = blkAll.filter(x => x.status !== 'responded').length;
    setSummary('sumQ', openQ, 'query', 'queries');
    setSummary('sumCp', openCp, 'counter-proposal', 'counter-proposals');
    setSummary('sumBlk', openBlk, 'blocker', 'blockers');
    setSummary('sumMs', ms.length, 'milestone', 'milestones');

    const qEmpty = qAll.length ? emptyHtml('Nothing open. Toggle "Show responded" to review answered queries.') : emptyHtml('No queries received yet.');
    const cpEmpty = cpAll.length ? emptyHtml('Nothing open. Toggle "Show responded" to review past decisions.') : emptyHtml('None received yet.');
    const blkEmpty = blkAll.length ? emptyHtml('Nothing open. Toggle "Show responded" to review resolved blockers.') : emptyHtml('None received yet.');
    document.getElementById('qList').innerHTML = q.length ? q.map(renderQueryCard).join('') : qEmpty;
    document.getElementById('cpList').innerHTML = cp.length ? cp.map(renderCpCard).join('') : cpEmpty;
    document.getElementById('blkList').innerHTML = blk.length ? blk.map(renderBlkCard).join('') : blkEmpty;
    document.getElementById('msList').innerHTML = ms.length ? ms.map(renderMsRow).join('') : emptyHtml('No milestone updates yet.');
  }

  function renderQueryCard(it){
    const p = it.payload || {};
    const cid = it.change_id || p.change_id || '';
    const qid = it.entity_id || p.query_id || '';
    const done = it.status === 'responded';
    const key = cssKey(qid);
    return `<div class="ibx ${done ? 'done' : 'new'}">
      <div class="ibx-head">
        <span class="ibx-id">${esc(qid)}</span>
        ${p.priority && p.priority !== 'normal' ? `<span class="chip sev-high">${esc(p.priority)}</span>` : ''}
        ${p.phase ? `<span class="chip type">${esc(p.phase)}</span>` : ''}
        <span class="status-badge ${done ? 'ok' : 'pending'}">${done ? 'answered' : 'needs answer'}</span>
        <span class="ibx-meta"><span>change_id: ${esc(cid)}</span><span>${fmtTs(it.ts)}</span></span>
      </div>
      <div class="ibx-summary"><b>${esc(p.query_text || '(no question text)')}</b></div>
      ${p.clause_ref ? `<div class="textline current"><span class="tl-label">Clause ref</span>${esc(p.clause_ref)}</div>` : ''}
      ${p.bank_interpretation ? `<div class="textline rationale"><span class="tl-label">Bank's interpretation</span>${esc(p.bank_interpretation)}</div>` : ''}
      ${done ? `<div style="margin-top:.6rem">${rawToggle(it)}</div>` : `<div class="ibx-actions" data-qkey="${key}" style="display:block">
        <div class="form-group" style="margin-bottom:.6rem"><label>Response</label>
          <textarea class="q-resp" rows="3" placeholder="Type NPCI's official answer to this query…"></textarea></div>
        <div style="display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap">
          <div class="form-group" style="flex:1;min-width:180px"><label>Grounded in (citations, comma-sep)</label>
            <input class="q-cite" type="text" placeholder="TSD §4.2.3, BRD §2.1" value="${esc(p.clause_ref || '')}" /></div>
          <label style="display:flex;gap:.4rem;align-items:center;font-size:12px;text-transform:none;letter-spacing:0"><input class="q-conf" type="checkbox" checked style="width:auto" /> interpretation confirmed</label>
          <label style="display:flex;gap:.4rem;align-items:center;font-size:12px;text-transform:none;letter-spacing:0" title="If ticked, the answer is provisional and will NOT be added to the bank's knowledge base until a formal change update"><input class="q-amend" type="checkbox" style="width:auto" /> amends change</label>
          <button class="btn btn-primary mini" onclick="sendQueryReply('${key}','${esc(qid)}','${esc(cid)}','${esc(p.ticket_id || '')}')">Send answer</button>
          ${rawToggle(it)}
        </div>
      </div>`}
      ${rawBlock(it)}
    </div>`;
  }

  function sendQueryReply(key, qId, changeId, ticketId){
    const grp = document.querySelector(`.ibx-actions[data-qkey="${key}"]`);
    const resp = grp.querySelector('.q-resp').value.trim();
    if (!resp) { toast('Response required', 'Type an answer before sending.', 'error'); return; }
    const fd = new FormData();
    fd.append('change_id', changeId); fd.append('query_id', qId);
    fd.append('ticket_id', ticketId);
    fd.append('response_text', resp);
    fd.append('interpretation_confirmed', grp.querySelector('.q-conf').checked);
    fd.append('amends_change', grp.querySelector('.q-amend').checked);
    fd.append('grounded_in', grp.querySelector('.q-cite').value);
    postTrigger('/trigger/clarification_response', fd, 'Answer sent to bank');
  }

  function setSummary(id, n, singular, plural){
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector('.count').textContent = n;
    el.childNodes[el.childNodes.length - 1].textContent = ' ' + (n === 1 ? singular : plural);
    el.classList.toggle('alert', n > 0 && id !== 'sumMs');
  }

  const CP_DECISIONS = ['accepted', 'accepted_with_modification', 'rejected', 'addressed_in_revision'];
  function decisionOptions(sel){
    return CP_DECISIONS.map(d => `<option value="${d}"${d === sel ? ' selected' : ''}>${d}</option>`).join('');
  }

  function renderCpCard(it){
    const p = it.payload || {};
    const cid = it.change_id || p.change_id || '';
    const cpid = it.entity_id || p.counter_proposal_id || '';
    const done = it.status === 'responded';
    const key = cssKey(cpid);
    const counters = (p.counters || []);

    const clauses = counters.map((c, i) => {
      const ref = c.clause_ref || ('clause ' + (i + 1));
      const cls = (c.classification || '').toLowerCase();
      const clsChip = cls ? `<span class="chip ${cls === 'mandatory' ? 'mandatory' : 'optional'}">${esc(cls)}</span>` : '';
      const cur = c.clause_text ? `<div class="textline current"><span class="tl-label">Current</span>${esc(c.clause_text)}</div>` : '';
      const prop = c.proposed_text ? `<div class="textline proposed"><span class="tl-label">Bank proposes</span>${esc(c.proposed_text)}</div>` : '';
      const rat = c.rationale ? `<div class="textline rationale"><span class="tl-label">Bank rationale</span>${esc(c.rationale)}</div>` : '';
      const alt = c.alternative_offered ? `<div class="textline"><span class="tl-label">Alternative offered</span>${esc(c.alternative_offered)}</div>` : '';
      return `<div class="clause">
        <div class="clause-head"><span class="clause-ref">${esc(ref)}</span>${clsChip}</div>
        ${cur}${prop}${rat}${alt}
        ${done ? '' : `<div class="clause-decision" data-ref="${esc(ref)}" data-cpkey="${key}">
          <div class="form-group"><label>Decision</label>
            <select class="cp-dec">${decisionOptions('accepted_with_modification')}</select></div>
          <div class="form-group" style="flex:1;min-width:200px"><label>Modified text</label>
            <input class="cp-mod" type="text" value="${esc(c.proposed_text || '')}" placeholder="applies when accepted_with_modification" /></div>
          <div class="form-group" style="flex:1;min-width:180px"><label>Reasoning</label>
            <input class="cp-rsn" type="text" placeholder="why NPCI decided this" /></div>
        </div>`}
      </div>`;
    }).join('');

    const bulk = (done || counters.length < 2) ? '' :
      `<div class="bulk-bar">Set all clauses to
        <select onchange="cpBulk('${key}', this.value)">
          <option value="">—</option>${decisionOptions('')}
        </select></div>`;

    return `<div class="ibx ${done ? 'done' : 'new'}">
      <div class="ibx-head">
        <span class="ibx-id">${esc(cpid)}</span>
        <span class="status-badge ${done ? 'ok' : 'pending'}">${done ? 'responded' : 'needs decision'}</span>
        <span class="chip type">${counters.length} clause${counters.length === 1 ? '' : 's'}</span>
        <span class="ibx-meta"><span>change_id: ${esc(cid)}</span><span>${fmtTs(it.ts)}</span></span>
      </div>
      ${p.summary ? `<div class="ibx-summary">${esc(p.summary)}</div>` : ''}
      ${bulk}
      ${clauses || emptyHtml('No clauses in this counter-proposal.')}
      ${done ? '' : `<div class="ibx-actions">
        <button class="btn btn-primary" onclick="sendCounterDecision('${key}','${esc(cpid)}','${esc(cid)}')">Send decision</button>
        ${rawToggle(it)}
      </div>`}
      ${done ? `<div style="margin-top:.6rem">${rawToggle(it)}</div>` : ''}
      ${rawBlock(it)}
    </div>`;
  }

  const SEVS = { critical: 'sev-critical', high: 'sev-high', medium: 'sev-medium', low: 'sev-low' };
  function renderBlkCard(it){
    const p = it.payload || {};
    const cid = it.change_id || p.change_id || '';
    const bid = it.entity_id || p.blocker_id || '';
    const done = it.status === 'responded';
    const key = cssKey(bid);
    const sev = (p.severity || '').toLowerCase();
    const sevCls = SEVS[sev] || 'sev-low';
    const cardCls = done ? 'done' : ('new ' + (sev === 'critical' ? 'blocker-crit' : (sev === 'high' ? 'blocker-high' : '')));

    return `<div class="ibx ${cardCls}">
      <div class="ibx-head">
        <span class="ibx-id">${esc(bid)}</span>
        ${sev ? `<span class="chip ${sevCls}">${esc(sev)}</span>` : ''}
        ${p.type ? `<span class="chip type">${esc(p.type)}</span>` : ''}
        <span class="status-badge ${done ? 'ok' : 'pending'}">${done ? 'resolved' : esc(it.status || 'new')}</span>
        <span class="ibx-meta"><span>change_id: ${esc(cid)}</span><span>${fmtTs(it.ts)}</span></span>
      </div>
      <div class="ibx-summary"><b>${esc(p.subject || '(no subject)')}</b>${p.description ? '<br>' + esc(p.description) : ''}</div>
      ${done ? `<div>${rawToggle(it)}</div>` : `<div class="ibx-actions" data-blkkey="${key}">
        <div class="action-group">
          <span class="ag-label">Triage</span>
          <div class="form-group"><label>Status</label>
            <select class="blk-status"><option>received</option><option selected>triaged</option><option>assigned</option><option>in_investigation</option></select></div>
          <div class="form-group"><label>Team</label><input class="blk-team" type="text" placeholder="optional" style="min-width:120px" /></div>
          <div class="form-group" style="flex:1;min-width:140px"><label>Notes</label><input class="blk-notes" type="text" placeholder="optional" /></div>
          <button class="btn btn-ghost mini" onclick="sendBlockerStatus('${key}','${esc(bid)}','${esc(cid)}')">Update</button>
        </div>
        <div class="action-group">
          <span class="ag-label">Resolve</span>
          <div class="form-group"><label>Resolution</label>
            <select class="blk-res"><option selected>resolved</option><option>wontfix</option><option>deferred</option></select></div>
          <div class="form-group" style="flex:1;min-width:160px"><label>Resolution text</label><input class="blk-restext" type="text" placeholder="how it was resolved" /></div>
          <button class="btn btn-primary mini" onclick="sendBlockerResolution('${key}','${esc(bid)}','${esc(cid)}')">Resolve &amp; close</button>
        </div>
      </div>`}
      ${rawBlock(it)}
    </div>`;
  }

  // Build a DOM-safe key from an id (ids may contain chars invalid in element IDs / selectors).
  function cssKey(id){ return String(id || '').replace(/[^a-zA-Z0-9_-]/g, '_'); }
  function rawToggle(it){ return `<span class="raw-toggle" onclick="toggleRaw('${cssKey(it.entity_id)}_${it.id}')">⟨/⟩ raw JSON</span>`; }
  function rawBlock(it){
    return `<pre class="raw-json" id="raw-${cssKey(it.entity_id)}_${it.id}">${esc(JSON.stringify(it.payload || {}, null, 2))}</pre>`;
  }
  function toggleRaw(k){ const el = document.getElementById('raw-' + k); if (el) el.classList.toggle('open'); }

  function cpBulk(key, val){
    if (!val) return;
    document.querySelectorAll(`.clause-decision[data-cpkey="${key}"] .cp-dec`).forEach(s => { s.value = val; });
  }

  function renderMsRow(it){
    const p = it.payload || {};
    const label = it.task_type === 'milestone_status_report'
      ? ('report · current: ' + (p.current_milestone || '—'))
      : ((p.milestone || '') + ' — ' + (p.state || ''));
    return `<div class="message-item">
      <span class="task-type-badge">${esc(it.task_type)}</span>
      <div class="msg-summary"><div>${esc(label)}</div>
        <div style="color:var(--text-dim);font-size:11px">change_id: ${esc(it.change_id||'')}${p.notes?(' · '+esc(p.notes)):''}</div></div>
      <span class="status-badge info">${esc(fmtTs(it.ts))}</span>
    </div>`;
  }

  async function postTrigger(url, fd, okMsg){
    try {
      const r = await fetch(url, { method:'POST', body: fd });
      const d = await r.json();
      const inner = (d.bank_response && (d.bank_response.result || d.bank_response.error)) || d.report || d.bank_response || {};
      if (r.ok) toast(okMsg, JSON.stringify(inner).slice(0, 140));
      else toast('Failed', d.detail || 'Error', 'error');
    } catch(ex) { toast('Network error', ex.message, 'error'); }
    loadInbox(); loadMessages();
  }

  function sendCounterDecision(key, cpId, changeId){
    const rows = document.querySelectorAll(`.clause-decision[data-cpkey="${key}"]`);
    let per;
    if (rows.length){
      per = Array.from(rows).map(row => {
        const decision = row.querySelector('.cp-dec').value;
        const mod = row.querySelector('.cp-mod').value.trim();
        const rsn = row.querySelector('.cp-rsn').value.trim();
        const out = { clause_ref: row.dataset.ref || '', decision };
        if (decision === 'accepted_with_modification' && mod) out.modified_text = mod;
        out.reasoning = rsn || 'Manual decision from Mock NPCI operator';
        return out;
      });
    } else {
      per = [{ clause_ref: '', decision: 'accepted_with_modification', reasoning: 'Manual decision from Mock NPCI operator' }];
    }
    const fd = new FormData();
    fd.append('change_id', changeId); fd.append('counter_proposal_id', cpId);
    fd.append('per_counter', JSON.stringify(per));
    postTrigger('/trigger/counter_decision', fd, 'Counter decision sent');
  }

  function sendBlockerStatus(key, bId, changeId){
    const grp = document.querySelector(`.ibx-actions[data-blkkey="${key}"]`);
    const fd = new FormData();
    fd.append('change_id', changeId); fd.append('blocker_id', bId);
    fd.append('status', grp.querySelector('.blk-status').value);
    const team = grp.querySelector('.blk-team').value.trim();
    const notes = grp.querySelector('.blk-notes').value.trim();
    if (team) fd.append('assigned_team', team);
    if (notes) fd.append('notes', notes);
    postTrigger('/trigger/blocker_status_update', fd, 'Blocker status updated');
  }

  function sendBlockerResolution(key, bId, changeId){
    const grp = document.querySelector(`.ibx-actions[data-blkkey="${key}"]`);
    const fd = new FormData();
    fd.append('change_id', changeId); fd.append('blocker_id', bId);
    fd.append('resolution', grp.querySelector('.blk-res').value);
    fd.append('resolution_text', grp.querySelector('.blk-restext').value);
    postTrigger('/trigger/blocker_resolution', fd, 'Blocker resolved');
  }

  document.getElementById('msReqForm').addEventListener('submit', e => {
    e.preventDefault();
    postTrigger('/trigger/milestone_status_request', new FormData(e.target), 'Milestone status requested');
  });

  async function saveAutoReply(){
    const fd = new FormData();
    fd.append('auto_reply_counter', document.getElementById('arCounter').checked);
    fd.append('auto_reply_blocker', document.getElementById('arBlocker').checked);
    const arq = document.getElementById('arQuery');
    if (arq) fd.append('auto_reply_query', arq.checked);
    try { await fetch('/config/auto-reply', { method:'POST', body: fd }); toast('Saved', 'Auto-reply preference updated'); }
    catch(ex) { toast('Error', ex.message, 'error'); }
  }

  // ── Boot ──
  loadStatus();
  loadMessages();
  loadInbox();
  startAutoRefresh();
</script>
</body>
</html>"""


# ── Startup message ────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    global _outbox_dispatcher_task
    await asyncio.to_thread(_init_outbox)
    await asyncio.to_thread(_init_inbox)
    _outbox_dispatcher_task = asyncio.create_task(_outbox_dispatcher())
    logger.info("=" * 60)
    logger.info(f"  Mock NPCI Agent starting up")
    logger.info(f"  Agent ID  : {AGENT_ID}")
    logger.info(f"  Bank URL  : {BANK_A2A_URL}")
    logger.info(f"  HMAC      : {'✓ configured' if _HMAC_SECRET else '✗ NOT SET'}")
    logger.info(f"  JWT       : {'✓ configured' if _SECRET_KEY else '✗ NOT SET'}")
    logger.info(f"  Auto-reply: {AUTO_REPLY} (delay={AUTO_REPLY_DELAY}s)")
    logger.info(f"  UI        : http://localhost:{os.getenv('MOCK_NPCI_PORT', '8001')}")
    logger.info("=" * 60)
    if not _HMAC_SECRET:
        logger.warning("  A2A_HMAC_SECRET is not set — outbound messages will not be HMAC-signed")
    if not _SECRET_KEY:
        logger.warning("  SECRET_KEY is not set — JWTs will be invalid (bank will reject Layer 4)")


# ── Entry point ────────────────────────────────────────────────────────────

# ESC_SEND / bank ack-message outbox is deferred; this batch covers NPCI document pushes only.


@app.on_event("shutdown")
async def _shutdown():
    if _outbox_dispatcher_task:
        _outbox_dispatcher_task.cancel()
        try:
            await asyncio.wait_for(_outbox_dispatcher_task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            pass


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MOCK_NPCI_PORT", "8001"))
    uvicorn.run("mock_npci_agent.main:app", host="0.0.0.0", port=port, reload=True)
