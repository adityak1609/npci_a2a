"""Transport-level A2A auth for the bank's Google-A2A (ADK) server.

Enforces spec §4.1 **layers 1–4** at the ASGI boundary — the insertion point
the ADK agent itself cannot reach (a :class:`~google.adk.agents.BaseAgent`
receives message *parts*, never the HTTP request, so mTLS headers, the client
IP, the HMAC envelope headers, and ``Authorization`` are invisible inside the
agent).

Per the spec's transport stack, the Google A2A SDK is the *transport* and the
NPCI protocol layers ride on top of it — the HMAC envelope and JWT bearer are
BOTH required, exactly as on the legacy custom endpoint:

    L1 mTLS   → ``a2a_auth.check_mtls``        (reverse-proxy headers)
    L2 CIDR   → ``a2a_auth.check_cidr``
    L3 HMAC   → ``a2a_auth.verify_hmac``       (over the exact raw body bytes)
    L4 JWT    → ``a2a_auth.verify_jwt_session`` (+ session revocation)

This is a **pure ASGI middleware**, not ``BaseHTTPMiddleware``: L3 must verify
the signature over the exact bytes received, before any JSON parsing, so the
body is buffered here (with the same app-level size cap as the legacy
endpoint) and replayed downstream to the a2a-sdk handler.

The authenticated subject is published on a :class:`contextvars.ContextVar` so
the agent can run the partner-binding + business-authz layers (L5–L8) with it
(see ``agent/adk/bank_agent.py``).

The whole middleware is gated by ``A2A_ADK_TRANSPORT_AUTH`` (default off) so the
bundled mock keeps working in local development; production sets it ``true``
together with the usual ``A2A_MTLS_REQUIRED`` / ``A2A_CIDR_ALLOWLIST`` /
``A2A_HMAC_SECRET`` / ``A2A_JWT_REQUIRED_EXTERNAL`` config.
"""

from __future__ import annotations

import contextvars
import logging
import os
from dataclasses import dataclass
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a_auth import (
    A2AAuthError,
    check_cidr,
    check_mtls,
    verify_hmac,
    verify_jwt_session,
)
from config import env_bool
from database import SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class AdkPrincipal:
    """The authenticated caller, published from the middleware to the agent."""

    jwt_sub: Optional[str] = None
    caller_ip: str = ""


# Set by the middleware before dispatching downstream; read by the agent in the
# same request context. contextvars propagate into the tasks the ADK executor
# spawns (they copy the current context), so the agent sees the value set here.
adk_principal: contextvars.ContextVar[Optional[AdkPrincipal]] = contextvars.ContextVar(
    "adk_principal", default=None
)


def transport_auth_enabled() -> bool:
    return env_bool("A2A_ADK_TRANSPORT_AUTH", default=False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _max_request_bytes() -> int:
    """Same wire-size cap derivation as the legacy /a2a/external endpoint."""
    decoded_limit = _env_int("A2A_MAX_INGEST_BYTES", 50 * 1024 * 1024)
    default_wire_limit = int(decoded_limit * 1.45) + (1024 * 1024)
    return _env_int("A2A_MAX_REQUEST_BYTES", default_wire_limit)


class TransportAuthMiddleware:
    """Runs A2A transport auth (L1–L4) before the request reaches the agent."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # Only guard RPC sends (POST). GET /.well-known/agent-card.json discovery
        # must stay open so a client can read the securitySchemes it must satisfy.
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not transport_auth_enabled()
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)  # header/client access only — body is buffered below
        caller_ip = request.client.host if request.client else "0.0.0.0"

        # Buffer the raw body with the legacy endpoint's app-level cap. The HMAC
        # envelope (L3) signs the exact bytes on the wire, so verification must
        # see them before the a2a-sdk parses/reconstructs the JSON-RPC message.
        limit = _max_request_bytes()
        messages: list[dict] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # client went away — nothing to respond to
            messages.append(message)
            total += len(message.get("body", b""))
            if total > limit:
                response = self._error_response(
                    "request_too_large", f"A2A request body exceeds {limit} bytes.", 413
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body"):
                break
        raw_body = b"".join(m.get("body", b"") for m in messages)

        db = SessionLocal()
        try:
            check_mtls(request)                        # L1
            check_cidr(request)                        # L2
            verify_hmac(request, raw_body, db)         # L3 — HMAC envelope
            jwt_sub = verify_jwt_session(request, db)  # L4 — JWT (+ revocation)
        except A2AAuthError as exc:
            self._audit_rejection(db, caller_ip, raw_body, exc)
            response = self._error_response(exc.error_code, exc.detail, exc.http_status)
            await response(scope, receive, send)
            return
        finally:
            db.close()

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return await receive()

        token = adk_principal.set(AdkPrincipal(jwt_sub=jwt_sub, caller_ip=caller_ip))
        try:
            await self.app(scope, replay_receive, send)
        finally:
            adk_principal.reset(token)

    @staticmethod
    def _audit_rejection(db, caller_ip: str, raw_body: bytes, exc: A2AAuthError) -> None:
        """Log the rejection to a2a_messages + bump the metric (parity with the
        legacy endpoint). Auditing must never mask the real rejection."""
        try:
            from a2a_audit import log_a2a_rejection
            from metrics import inc_counter

            inc_counter("a2a_auth_rejections_total", {"error_code": exc.error_code})
            log_a2a_rejection(
                db,
                raw_body=raw_body,
                caller_ip=caller_ip,
                error_code=exc.error_code,
                detail=exc.detail,
                http_status=exc.http_status,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to audit ADK transport-auth rejection")

    @staticmethod
    def _error_response(error_code: str, detail: str, http_status: int) -> JSONResponse:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32000,
                    "message": detail,
                    "data": {"a2a_error": error_code},
                },
            },
            status_code=http_status,
        )


def add_transport_auth(app):
    """Wrap the ADK Starlette app with transport-level A2A auth (L1–L4)."""
    app.add_middleware(TransportAuthMiddleware)
    return app
