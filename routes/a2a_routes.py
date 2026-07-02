"""FastAPI routes for JSON-RPC A2A and admin Inbox data.

/a2a/external — NPCI-facing endpoint secured by the 8-layer auth stack
                (spec §4.1).  Dispatches on envelope task_type.
/a2a/internal — Admin-frontend endpoint secured by admin JWT.
                Dispatches on custom method names (agent.get_summary, etc.).
"""

import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jose import JWTError
from sqlalchemy.orm import Session

from a2a_audit import log_a2a_message, log_a2a_rejection
from a2a_auth import A2AAuthContext, A2AAuthError, a2a_external_auth
from a2a_models import A2AEnvelope
from agent import methods
from agent.dispatcher import dispatch_rpc
from agent.errors import INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, rpc_error
from auth import AUTH_COOKIE_NAME, decode_token, get_current_admin
from database import (
    A2AIngestionJob,
    AuditLog,
    DocumentSummary,
    NpciTicket,
    ReceivedDocument,
    User,
    get_db,
)
from rate_limit import check_a2a_external_rate_limit
from services.a2a_ingestor import retry_ingestion_job
from metrics import inc_counter

router = APIRouter()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _max_a2a_request_bytes() -> int:
    decoded_limit = _env_int("A2A_MAX_INGEST_BYTES", 50 * 1024 * 1024)
    default_wire_limit = int(decoded_limit * 1.45) + (1024 * 1024)
    return _env_int("A2A_MAX_REQUEST_BYTES", default_wire_limit)


async def _read_limited_body(request: Request) -> bytes:
    """Read a request body with an app-level cap before HMAC verification."""
    limit = _max_a2a_request_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"A2A request body exceeds {limit} bytes.",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header.",
            ) from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"A2A request body exceeds {limit} bytes.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# /a2a/internal — Admin-only JSON-RPC (unchanged method names)
# ---------------------------------------------------------------------------

INTERNAL_METHODS = {
    methods.GET_SUMMARY,
    methods.QUERY_RAG,
    methods.ESCALATE_TO_NPCI,
}


def _format_dt(dt) -> str:
    """Return an ISO timestamp string for route responses."""
    return dt.isoformat() if dt else ""


def _audit(db: Session, action: str, target: str | None = None, detail: str | None = None) -> None:
    try:
        db.add(AuditLog(admin_id=None, action=action, target=target, detail=detail))
        db.commit()
    except Exception:
        db.rollback()


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return request.cookies.get(AUTH_COOKIE_NAME, "")
    return token


def _require_admin_jwt(request: Request, db: Session) -> None:
    token = _bearer_token(request)
    if not token:
        _audit(db, "a2a_auth_failed", "internal", "missing bearer token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required.")

    try:
        payload = decode_token(token)
    except (HTTPException, JWTError):
        _audit(db, "a2a_auth_failed", "internal", "invalid admin JWT")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")

    email = payload.get("sub")
    user = db.query(User).filter(User.email == email).first() if email else None
    if not user or not user.is_admin:
        _audit(db, "a2a_auth_failed", "internal", f"not admin: {email or 'unknown'}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")


class _ParseError(Exception):
    """Raised when the request body is not valid JSON."""


async def _read_payload(request: Request):
    """Parse the JSON body, returning a dict or list. Raises _ParseError on failure."""
    try:
        body = await request.json()
    except Exception as exc:
        raise _ParseError(str(exc))
    # JSON-RPC payloads must be an object or an array (batch).
    if not isinstance(body, (dict, list)):
        raise _ParseError("Body must be a JSON object or array")
    return body


def _check_method_allowlist_single(payload: dict, allowed_methods: set[str]) -> dict | None:
    """Pre-check a single request's method against the allowlist.

    Returns an error response dict if blocked, or None if the method is allowed.
    """
    request_id = payload.get("id") if isinstance(payload, dict) else None
    method = payload.get("method") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return rpc_error(request_id, INVALID_REQUEST, "Invalid Request")
    if method not in allowed_methods:
        # Notifications (no "id") should not get a response, but an
        # unknown method on a restricted endpoint is an auth boundary
        # issue — always report it.
        return rpc_error(request_id, METHOD_NOT_FOUND, "Method not available on this A2A endpoint")
    return None


async def _dispatch_with_method_allowlist(payload, db: Session, allowed_methods: set[str]):
    """Dispatch a single or batch JSON-RPC payload with per-method allowlisting."""

    # ── Batch request (JSON-RPC 2.0 §6) ──────────────────────────────────
    if isinstance(payload, list):
        if not payload:
            return JSONResponse(rpc_error(None, INVALID_REQUEST, "Invalid Request: empty batch"))

        # Pre-filter: check allowlist before dispatching.
        checked: list = []
        for item in payload:
            error = _check_method_allowlist_single(item, allowed_methods)
            if error is not None:
                checked.append(error)
            else:
                checked.append(item)  # Pass through for dispatch

        # Now dispatch allowed items and collect responses.
        responses: list[dict] = []
        for item in checked:
            if isinstance(item, dict) and "error" in item:
                # Already an error from the allowlist check.
                responses.append(item)
            else:
                from agent.dispatcher import _dispatch_single
                result = await _dispatch_single(item, db)
                if result is not None:  # Notifications return None
                    responses.append(result)

        if not responses:
            # §6: If all requests were notifications, return nothing.
            return Response(status_code=204)

        return JSONResponse(responses)

    # ── Single request ────────────────────────────────────────────────────
    error = _check_method_allowlist_single(payload, allowed_methods)
    if error is not None:
        return JSONResponse(error)

    response = await dispatch_rpc(payload, db)

    # Notification: dispatcher returns None → no HTTP body (204).
    if response is None:
        return Response(status_code=204)

    return JSONResponse(response)


# ---------------------------------------------------------------------------
# /a2a/external — NPCI-facing, 8-layer auth + task_type dispatch
# ---------------------------------------------------------------------------

@router.post("/a2a/external")
async def a2a_external_endpoint(request: Request, db: Session = Depends(get_db)):
    """NPCI-facing A2A endpoint secured by the full spec §4.1 auth stack.

    Dispatches on the A2A envelope's ``task_type`` field against the 28
    defined message types (+ echo).  This endpoint does NOT use custom
    method names — it speaks the spec's vocabulary verbatim.

    NOTE: This is a spec-conformant endpoint for dev/cert interoperability
    with NPCI's actual platform.  Full PKI onboarding with NPCI is required
    for production use.
    """
    caller_ip = request.client.host if request.client else "0.0.0.0"
    check_a2a_external_rate_limit(caller_ip)

    # Read raw body for HMAC verification (must be read before JSON parsing).
    # Keep an application limit here so direct-to-Uvicorn deployments are not
    # relying solely on reverse-proxy body limits.
    try:
        raw_body = await _read_limited_body(request)
    except HTTPException as exc:
        error_code = "request_too_large" if exc.status_code == 413 else "invalid_request_body"
        detail = str(exc.detail)
        log_a2a_rejection(
            db,
            raw_body=b"",
            caller_ip=caller_ip,
            error_code=error_code,
            detail=detail,
            http_status=exc.status_code,
        )
        return JSONResponse(
            rpc_error(None, -32000, detail, {"a2a_error": error_code}),
            status_code=exc.status_code,
        )

    # Run 8-layer auth stack
    try:
        ctx = await a2a_external_auth(request, raw_body, db)
    except A2AAuthError as exc:
        inc_counter("a2a_auth_rejections_total", {"error_code": exc.error_code})
        # Log the rejection to a2a_messages audit table
        log_a2a_rejection(
            db,
            raw_body=raw_body,
            caller_ip=caller_ip,
            error_code=exc.error_code,
            detail=exc.detail,
            http_status=exc.http_status,
        )
        # Return JSON-RPC error with spec error code
        request_id = None
        try:
            body_json = json.loads(raw_body)
            request_id = body_json.get("id") if isinstance(body_json, dict) else None
        except Exception:
            pass

        return JSONResponse(
            rpc_error(request_id, -32000, exc.detail, {"a2a_error": exc.error_code}),
            status_code=exc.http_status,
        )

    # Auth passed — dispatch on task_type
    envelope = ctx.envelope
    request_id = None
    try:
        body_json = json.loads(raw_body)
        request_id = body_json.get("id") if isinstance(body_json, dict) else None
    except Exception:
        pass

    try:
        from agent.external_dispatcher import NotImplementedTaskError, dispatch_external
        result = await dispatch_external(envelope, db, ctx)
    except NotImplementedTaskError as exc:
        inc_counter("a2a_not_implemented_total", {"task_type": envelope.task_type})
        return JSONResponse(
            rpc_error(request_id, -32601, str(exc), {"a2a_error": "not_implemented"}),
            status_code=501,
        )
    except ValueError as exc:
        return JSONResponse(
            rpc_error(request_id, -32602, str(exc)),
            status_code=400,
        )
    except Exception as exc:
        return JSONResponse(
            rpc_error(request_id, -32603, "Internal error"),
            status_code=500,
        )

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    })


# ---------------------------------------------------------------------------
# /a2a/internal — Admin-only (unchanged)
# ---------------------------------------------------------------------------

@router.post("/a2a/internal")
async def a2a_internal_endpoint(request: Request, db: Session = Depends(get_db)):
    """Admin-frontend JSON-RPC endpoint secured exclusively by admin JWT."""
    _require_admin_jwt(request, db)
    try:
        payload = await _read_payload(request)
    except _ParseError:
        return JSONResponse(rpc_error(None, PARSE_ERROR, "Parse error"))
    return await _dispatch_with_method_allowlist(payload, db, INTERNAL_METHODS)


@router.post("/a2a", deprecated=True)
async def a2a_deprecated_endpoint():
    """Deprecated shared A2A endpoint retained only to point callers at split routes."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use /a2a/external for NPCI calls or /a2a/internal for admin frontend calls.",
    )


# ---------------------------------------------------------------------------
# Admin data endpoints (unchanged)
# ---------------------------------------------------------------------------

@router.get("/documents")
def list_received_documents(
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Return A2A received documents ordered newest first."""
    rows = db.query(ReceivedDocument).order_by(ReceivedDocument.created_at.desc()).all()
    if not rows:
        summaries = db.query(DocumentSummary).order_by(DocumentSummary.created_at.desc()).all()
        return [
            {
                "id": row.id,
                "document_id": row.document_id,
                "filename": row.filename,
                "source": row.source,
                "status": "ready",
                "summary": row.summary,
                "job_id": None,
                "job_error": None,
                "created_at": _format_dt(row.created_at),
            }
            for row in summaries
        ]

    result = []
    for row in rows:
        latest_job = (
            db.query(A2AIngestionJob)
            .filter(A2AIngestionJob.document_id == row.id)
            .order_by(A2AIngestionJob.created_at.desc())
            .first()
        )
        result.append({
            "id": row.id,
            "document_id": row.id,
            "filename": row.filename,
            "source": row.source,
            "file_type": row.file_type,
            "status": row.status,
            "change_id": row.change_id,
            "doc_type": row.doc_type,
            "version": row.version,
            "summary": row.summary.summary if row.summary else "",
            "job_id": latest_job.id if latest_job else None,
            "job_error": latest_job.error if latest_job else None,
            "content_hash": row.content_hash,
            "created_at": _format_dt(row.created_at),
            "updated_at": _format_dt(row.updated_at),
        })
    return result


@router.post("/ingestion-jobs/{job_id}/retry")
async def retry_failed_ingestion_job(
    job_id: int,
    current_admin: User = Depends(get_current_admin),
):
    """Retry a failed A2A ingestion job."""
    try:
        return await retry_ingestion_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/npci-tickets")
def list_npci_tickets(
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Return NPCI tickets ordered newest first; params: admin JWT; returns ticket list."""
    rows = db.query(NpciTicket).order_by(NpciTicket.created_at.desc()).all()
    return [
        {
            "id": row.id,
            "ticket_id": row.ticket_id,
            "query": row.query,
            "status": row.status,
            "answer": row.answer,
            "change_id": row.change_id,
            "provisional": row.provisional,
            "amends_change": row.amends_change,
            "approver": row.approver,
            "created_at": _format_dt(row.created_at),
        }
        for row in rows
    ]
