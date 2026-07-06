"""
main.py — FastAPI application for the NPCI A2A Partner Platform (bank side).

Wires together the platform's surfaces (see routes/ for the endpoints):
    routes/a2a_routes.py    → /a2a/external (NPCI JSON-RPC ingress) + /a2a/internal
    routes/auth_routes.py   → login / session / MFA / logout
    routes/chat_routes.py   → grounded RAG chat + SSE streaming
    routes/admin_routes.py  → portal dashboard, changes, updates, admin ops
    routes/sys_routes.py    → /health, /metrics, /ingest
    routes/ws_routes.py     → WebSocket updates

This module owns app construction: security-config validation, CORS/CSRF/CSP
middleware, lifespan (migration gate, startup recovery, periodic cleanup), and
serving the built SPA.
"""

import os
import re
import json
import time
import secrets as token_secrets
import shutil
import tempfile
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from google import genai
from google.genai import types
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from datetime import timezone

from database import (
    init_db, get_db, User, ChatSession, ChatMessage, MessageFeedback,
    DocumentChunk, DocumentSummary, AuditLog, A2AIngestionJob, SessionLocal, engine
)
from auth import (
    get_current_user, get_current_admin, authenticate_user_for_role,
    create_access_token, revoke_token, UserLogin, Token, AUTH_COOKIE_NAME
)
from config import configure_logging, env_bool, env_int, request_id_var
from compliance import ComplianceBlockedError, require_external_ai_approval
from file_validation import FileValidationError, max_admin_upload_bytes, validate_pdf_bytes
from rate_limit import check_chat_rate_limit, check_rate_limit
from retriever import retrieve, retrieve_with_scores, index_exists, invalidate_cache
from prompt import build_prompt
from ingest import run_ingestion

from guardrails import (
    VerifiedStreamBuffer,
    classifier_circuit_state,
    check_input,
    check_output,
    sanitize_question,
    _check_regex,
)
from routes.a2a_routes import router as a2a_router
from routes.ws_routes import router as ws_router
from routes.auth_routes import router as auth_router
from routes.chat_routes import router as chat_router
from routes.sys_routes import router as sys_router
from routes.admin_routes import router as admin_router
from metrics import inc_counter, observe_histogram, render_prometheus, set_gauge, time_request
from telemetry import configure_telemetry
from csrf import csrf_middleware
from secrets_loader import load_secret_files

configure_logging()
logger = logging.getLogger(__name__)

def _route_metric_label(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or "__unmatched__"


def _log_internal_error(scope: str, exc: Exception) -> None:
    logger.exception("Internal error in %s", scope, exc_info=exc)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()
load_secret_files()

google_api_key = os.getenv("GOOGLE_API_KEY")
if not google_api_key:
    raise EnvironmentError("GOOGLE_API_KEY not set. Create a .env file with your key.")

client = genai.Client(api_key=google_api_key)

# ---------------------------------------------------------------------------
# FastAPI app with lifespan
# ---------------------------------------------------------------------------
_nonce_cleanup_task = None

# Advisory-lock keys electing a single process for singleton work across
# Gunicorn workers.  Stable arbitrary 64-bit values; worker.py's retention
# key (…_0003) shares this keyspace.
_STARTUP_RECOVERY_LOCK_KEY = 0x4E50_4349_0000_0001
_MAINTENANCE_LOCK_KEY = 0x4E50_4349_0000_0002


def _expected_alembic_head() -> str:
    """Return the head revision of the checked-in migration chain.

    Derived from the Alembic scripts themselves (never a hand-maintained
    constant, which drifts) so the production migration gate always compares
    the database against the code actually being deployed.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = AlembicConfig(os.path.join(base_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(base_dir, "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        raise EnvironmentError("No Alembic head revision found under alembic/versions.")
    return head

# ---------------------------------------------------------------------------
# SQL constants — maintenance queries used by the periodic cleanup task.
# Keeping them at module level makes them easy to locate and review.
# ---------------------------------------------------------------------------
_SQL_DELETE_EXPIRED_NONCES = "DELETE FROM a2a_nonces WHERE expires_at < NOW()"
_SQL_DELETE_EXPIRED_WS_TICKETS = "DELETE FROM ws_tickets WHERE expires_at < NOW()"
_SQL_TIMEOUT_STALE_IDEMPOTENCY_KEYS = """
    UPDATE a2a_idempotency_keys
    SET status = 'failed',
        error_code = 'processing_timeout',
        http_status = 500,
        updated_at = NOW()
    WHERE status = 'processing'
      AND updated_at < NOW() - INTERVAL '30 minutes'
"""


def _env_bool(name: str, default: bool = False) -> bool:
    return env_bool(name, default)


def _is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def _env_int(name: str, default: int) -> int:
    return env_int(name, default)


def _is_placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return (
        not normalized
        or normalized.startswith("<")
        or "your-" in normalized
        or "example.com" in normalized
    )


def _invalid_production_cors_origin(origin: str) -> bool:
    normalized = (origin or "").strip().lower()
    parsed = urlparse(normalized)
    return (
        not normalized
        or normalized == "*"
        or _is_placeholder(normalized)
        or "localhost" in normalized
        or "127.0.0.1" in normalized
        or parsed.scheme != "https"
        or not parsed.netloc
    )


def _validate_security_config() -> None:
    """Fail fast when security-critical production settings are unsafe.

    mTLS and JWT-required flags are only enforced in production — in dev/stub
    mode they may be disabled to allow the local Mock NPCI Agent (plain HTTP)
    to connect without a certificate.
    """
    errors = []
    is_production = _is_production()

    hmac_secret = os.getenv("A2A_HMAC_SECRET", "")
    if _is_placeholder(hmac_secret) or len(hmac_secret) < 32:
        errors.append("A2A_HMAC_SECRET must be a real high-entropy secret of at least 32 characters.")

    npci_key = os.getenv("NPCI_API_KEY", "")
    if _is_placeholder(npci_key) or len(npci_key) < 24:
        errors.append("NPCI_API_KEY must be a real secret of at least 24 characters.")

    secret_key = os.getenv("SECRET_KEY", "")
    if _is_placeholder(secret_key) or len(secret_key) < 32:
        errors.append("SECRET_KEY must be at least 32 characters.")

    nonce_ttl = _env_int("A2A_NONCE_TTL_SECONDS", 600)
    if nonce_ttl < 300:
        errors.append("A2A_NONCE_TTL_SECONDS must be at least 300 seconds to cover timestamp skew.")

    if is_production:
        if not _env_bool("A2A_MTLS_REQUIRED", default=True):
            errors.append("A2A_MTLS_REQUIRED must be true in production.")
        if not _env_bool("A2A_JWT_REQUIRED_EXTERNAL", default=True):
            errors.append("A2A_JWT_REQUIRED_EXTERNAL must be true in production.")
        if not _env_bool("AUTH_COOKIE_SECURE", default=False):
            errors.append("AUTH_COOKIE_SECURE must be true when APP_ENV=production.")
        if not os.getenv("REDIS_URL", "").strip():
            errors.append("REDIS_URL must be configured when APP_ENV=production.")
        if os.getenv("A2A_INGESTION_MODE", "inline").strip().lower() != "worker":
            errors.append("A2A_INGESTION_MODE must be worker when APP_ENV=production.")
        if not os.getenv("A2A_CIDR_ALLOWLIST", "").strip():
            errors.append("A2A_CIDR_ALLOWLIST must be configured when APP_ENV=production.")
        if not os.getenv("A2A_MTLS_FINGERPRINTS", "").strip():
            errors.append("A2A_MTLS_FINGERPRINTS must be configured when APP_ENV=production.")
        if not os.getenv("A2A_MTLS_PARTNER_MAP", "").strip():
            errors.append("A2A_MTLS_PARTNER_MAP must bind certificate fingerprints to partners in production.")
        if not os.getenv("A2A_JWT_PARTNER_MAP", "").strip():
            errors.append("A2A_JWT_PARTNER_MAP must bind JWT subjects to partners in production.")
        if not os.getenv("A2A_KNOWN_AGENTS", "").strip():
            errors.append("A2A_KNOWN_AGENTS must be configured when APP_ENV=production.")
        if not os.getenv("NPCI_AGENT_URL", "").strip():
            errors.append("NPCI_AGENT_URL must be configured when APP_ENV=production.")
        if not os.getenv("NPCI_CLIENT_CERT_FILE", "").strip():
            errors.append("NPCI_CLIENT_CERT_FILE must be configured for outbound NPCI mTLS in production.")
        if not os.getenv("NPCI_CLIENT_KEY_FILE", "").strip():
            errors.append("NPCI_CLIENT_KEY_FILE must be configured for outbound NPCI mTLS in production.")
        cors_origins = [
            origin.strip().lower()
            for origin in os.getenv("CORS_ORIGINS", "").split(",")
            if origin.strip()
        ]
        if not cors_origins or any(_invalid_production_cors_origin(origin) for origin in cors_origins):
            errors.append("CORS_ORIGINS must contain only explicit HTTPS production origins.")

    if errors:
        raise EnvironmentError("Unsafe security configuration: " + " ".join(errors))


def _assert_database_migrated() -> None:
    """Require Alembic to have applied the expected production schema revision."""
    from sqlalchemy import text

    expected = _expected_alembic_head()
    db = SessionLocal()
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as exc:
        raise EnvironmentError(
            "Database is not Alembic-managed. Run `alembic upgrade head` before "
            "starting with APP_ENV=production."
        ) from exc
    finally:
        db.close()

    if revision != expected:
        raise EnvironmentError(
            f"Database migration revision mismatch: expected "
            f"{expected}, found {revision or 'none'}. "
            "Run `alembic upgrade head` before starting production."
        )


def _cleanup_expired_security_rows() -> None:
    """Delete expired nonce and WebSocket ticket rows.

    Advisory-lock guarded: with N Gunicorn workers each running the periodic
    cleanup loop, only one performs the sweep per interval — the rest skip.
    """
    from sqlalchemy import text

    from database import pg_try_advisory_lock

    with pg_try_advisory_lock(_MAINTENANCE_LOCK_KEY) as acquired:
        if not acquired:
            return
        db = SessionLocal()
        try:
            db.execute(text(_SQL_DELETE_EXPIRED_NONCES))
            db.execute(text(_SQL_DELETE_EXPIRED_WS_TICKETS))
            db.execute(text(_SQL_TIMEOUT_STALE_IDEMPOTENCY_KEYS))
            db.commit()
        finally:
            db.close()


async def _periodic_nonce_cleanup():
    """Background task to periodically delete expired A2A HMAC nonces."""
    interval = _env_int("A2A_NONCE_CLEANUP_INTERVAL_SECONDS", 600)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_cleanup_expired_security_rows)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Nonce cleanup task error", exc_info=e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB tables, configure threadpool, start file watcher, and recover orphaned jobs."""
    _validate_security_config()
    # Increase the default executor so asyncio.to_thread can service many
    # concurrent blocking calls (Gemini API, DB queries, etc.).  Default to
    # this process's DB pool ceiling so threads never outnumber the
    # connections they might each need; override for LLM-heavy workloads
    # (which hold threads but not DB connections) via env.
    from database import DB_MAX_OVERFLOW, DB_POOL_SIZE, pg_try_advisory_lock
    loop = asyncio.get_running_loop()
    max_workers = _env_int("ASYNC_EXECUTOR_MAX_WORKERS", DB_POOL_SIZE + DB_MAX_OVERFLOW)
    loop.set_default_executor(ThreadPoolExecutor(max_workers=max_workers))
    if _is_production():
        _assert_database_migrated()
    else:
        init_db()

    # Startup recovery — elect one process via advisory lock.  Claiming is
    # FOR UPDATE SKIP LOCKED-safe regardless, but N workers racing to flip
    # orphaned rows back to 'queued' is redundant, and in worker mode this
    # work belongs to a single process anyway.
    with pg_try_advisory_lock(_STARTUP_RECOVERY_LOCK_KEY) as acquired:
        if acquired:
            await asyncio.to_thread(_cleanup_expired_security_rows)

            # Recover durable received changes before accepting new traffic.
            from services.a2a_ingestor import recover_orphaned_jobs, recover_received_changes
            recovered_changes = await recover_received_changes()
            if recovered_changes:
                logger.info("Recovered received A2A changes", extra={"recovered": recovered_changes})

            # Recover ingestion jobs orphaned by a previous unclean shutdown.
            recovered = await recover_orphaned_jobs()
            if recovered:
                logger.info("Recovered orphaned ingestion jobs", extra={"recovered": recovered})
        else:
            logger.info("Another worker is running startup recovery; skipping in this process.")

    global _nonce_cleanup_task
    _nonce_cleanup_task = asyncio.create_task(_periodic_nonce_cleanup())

    yield

    if _nonce_cleanup_task:
        _nonce_cleanup_task.cancel()
        try:
            await asyncio.wait_for(_nonce_cleanup_task, timeout=_env_int("SHUTDOWN_GRACE_SECONDS", 30))
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


app = FastAPI(
    title="NPCI A2A Partner Platform (Bank)",
    description=(
        "Bank-side NPCI Agent-to-Agent change-management platform: spec-conformant "
        "A2A ingress, change lifecycle, partner portal, and a grounded RAG assistant."
    ),
    version="2.0.0",
    lifespan=lifespan,
)
configure_telemetry(app)

# CORS — use env-configured origins; never wildcard with credentials.
# Methods and headers are explicit — allow_methods=["*"] is unnecessarily
# permissive even when origins are restricted.
_cors_origins_raw = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)

app.include_router(a2a_router)
app.include_router(ws_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(sys_router)
app.include_router(admin_router)
# CSRF protection — Origin/Referer validation on cookie-authenticated mutating
# routes.  Must be registered AFTER the CORS middleware so preflight OPTIONS
# requests are handled before CSRF runs.
app.middleware("http")(csrf_middleware)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach baseline browser security headers to every HTTP response."""
    request_id = request.headers.get("X-Request-ID") or token_secrets.token_hex(16)
    request_id_token = request_id_var.set(request_id)
    csp_nonce = token_secrets.token_urlsafe(16)
    request.state.csp_nonce = csp_nonce
    started = time_request()
    try:
        if request.url.path in {"/chat", "/chat/stream"}:
            content_length = request.headers.get("content-length")
            max_bytes = _env_int("CHAT_MAX_REQUEST_BYTES", 65536)
            try:
                body_size = int(content_length) if content_length else 0
            except ValueError:
                response = PlainTextResponse("Invalid Content-Length.", status_code=400)
            else:
                if body_size > max_bytes:
                    response = PlainTextResponse("Request body too large.", status_code=413)
                else:
                    response = await call_next(request)
        else:
            response = await call_next(request)

        route = _route_metric_label(request)
        inc_counter("http_requests_total", {
            "method": request.method,
            "path": route,
            "status": response.status_code,
        })
        observe_histogram(
            "http_request_duration_seconds",
            time_request() - started,
            labels={"method": request.method, "path": route},
        )
        response.headers.setdefault("X-Request-ID", request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{csp_nonce}'; "
            "script-src-attr 'none'; "
            f"style-src 'self' 'nonce-{csp_nonce}'; "
            "style-src-attr 'none'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response
    finally:
        request_id_var.reset(request_id_token)

# NOTE: Gemini model selection, retry policy, and chat-memory constants live in
# config.py (env-driven) — do not redefine them here.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
frontend_dist = os.path.join(frontend_dir, "dist")

# Serve the built Vite bundle's hashed assets. check_dir=False so an unbuilt
# dev tree does not crash app import; the multi-stage Docker build produces dist/.
app.mount(
    "/assets",
    StaticFiles(directory=os.path.join(frontend_dist, "assets"), check_dir=False),
    name="assets",
)

# Inject a CSP nonce into every <script>/<style> opening tag that does not
# already carry one.  A regex — rather than a naive str.replace of "<script>"
# — ensures tags written with attributes (e.g. <script type="module">,
# <style media="...">) are also nonced and therefore not blocked by the
# strict Content-Security-Policy.  The lookahead `(?=[\s>])` requires a
# complete tag name so `<scripting>`-style tokens are never matched, and
# `(?![^>]*\bnonce=)` avoids double-injecting.
_CSP_TAG_RE = re.compile(
    r"<(script|style)(?=[\s>])(?![^>]*\bnonce=)",
    re.IGNORECASE,
)


def _inject_csp_nonce(html: str, nonce: str) -> str:
    if not nonce:
        return html
    return _CSP_TAG_RE.sub(lambda m: f'<{m.group(1)} nonce="{nonce}"', html)


def _frontend_response(request: Request) -> HTMLResponse:
    csp_nonce = getattr(request.state, "csp_nonce", "")
    index_path = os.path.join(frontend_dist, "index.html")
    if not os.path.isfile(index_path):
        return HTMLResponse(
            "<h1>Partner Platform</h1><p>Frontend bundle not built. Run "
            "<code>npm ci &amp;&amp; npm run build</code> in <code>frontend/</code>.</p>",
            status_code=200,
        )
    with open(index_path, "r", encoding="utf-8") as file:
        html = file.read()
    return HTMLResponse(_inject_csp_nonce(html, csp_nonce))


@app.get("/", tags=["Frontend"])
async def serve_frontend(request: Request):
    """Serve the chat UI at the root URL."""
    return _frontend_response(request)


@app.get("/admin/{path:path}", tags=["Frontend"])
async def serve_admin_frontend(path: str, request: Request):
    """Serve the admin SPA for browser navigation paths under /admin."""
    return _frontend_response(request)
