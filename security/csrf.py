"""security/csrf.py — Origin/Referer-based CSRF protection for cookie-authenticated routes.

CSRF is a cookie-specific attack: a malicious third-party page tricks the
browser into sending the victim's auth cookie to the server.

Defence strategy
----------------
Modern browsers attach an ``Origin`` header to every cross-site request.  We
validate that header against the configured ``CORS_ORIGINS`` allowlist on all
state-mutating methods (POST, PUT, PATCH, DELETE).  If ``Origin`` is absent
(older browsers / some form POSTs), we fall back to the ``Referer`` header.

Exemptions
----------
- GET / HEAD / OPTIONS — read-only, no state change.
- ``/a2a/*`` routes — secured by HMAC + mTLS, not browser cookies.
- ``/auth/admin/login``, ``/auth/login`` — pre-auth, no cookie exists yet.
- ``/auth/logout`` — clearing a cookie is low-risk; the alternative is a
  locked-out user if their browser omits Origin on the logout request.
- ``/health*``, ``/metrics`` — read-only or machine-to-machine.
- Requests that carry a Bearer token but NO auth cookie — API clients are
  not subject to CSRF because they don't use cookies.

Configuration
-------------
CSRF_ENFORCE=true (default in production; set false to disable in dev/test).
AUTH_COOKIE_NAME — must match the value in auth.py (default: mg_access_token).
CORS_ORIGINS      — comma-separated list of allowed origins (same as for CORS
                    middleware).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import PlainTextResponse

# ── Constants ─────────────────────────────────────────────────────────────

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Path prefixes where CSRF does not apply
_EXEMPT_PREFIXES = (
    "/a2a/",     # HMAC + mTLS machine-to-machine
    "/health",   # Read-only liveness / readiness probes
    "/metrics",  # Prometheus scrape (Bearer token protected)
)

# Exact paths exempt from CSRF (pre-auth or read-only)
_EXEMPT_EXACT = frozenset({
    "/auth/admin/login",
    "/auth/login",
    "/auth/logout",
})


# ── Helpers ───────────────────────────────────────────────────────────────

def _allowed_origins() -> frozenset[str]:
    """Return the normalised set of allowed origins from CORS_ORIGINS."""
    raw = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
    return frozenset(
        o.strip().lower().rstrip("/")
        for o in raw.split(",")
        if o.strip()
    )


def _normalise_origin(value: str) -> str:
    return value.strip().lower().rstrip("/")


def _referer_origin(request: Request) -> str | None:
    """Extract ``scheme://host`` from the Referer header, or None."""
    referer = request.headers.get("referer", "").strip()
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".lower()
    except Exception:
        pass
    return None


def _is_exempt(request: Request) -> bool:
    """Return True when the request does not need CSRF validation."""
    if request.method.upper() not in _MUTATING_METHODS:
        return True

    path = request.url.path

    if path in _EXEMPT_EXACT:
        return True

    for prefix in _EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return True

    # Bearer-token-only API clients are not subject to CSRF
    auth_cookie_name = os.getenv("AUTH_COOKIE_NAME", "mg_access_token")
    has_cookie = bool(request.cookies.get(auth_cookie_name))
    has_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    if has_bearer and not has_cookie:
        return True

    return False


# ── Middleware ────────────────────────────────────────────────────────────

async def csrf_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Validate Origin/Referer on mutating cookie-authenticated requests.

    Returns 403 when the origin cannot be confirmed as an allowed origin.
    Skips validation when CSRF_ENFORCE is falsy (useful in dev/test).
    """
    enforce = os.getenv("CSRF_ENFORCE", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }

    if not enforce or _is_exempt(request):
        return await call_next(request)

    allowed = _allowed_origins()
    auth_cookie_name = os.getenv("AUTH_COOKIE_NAME", "mg_access_token")

    # 1. Validate the Origin header (present on all cross-origin browser requests)
    raw_origin = request.headers.get("origin", "").strip()
    if raw_origin:
        if _normalise_origin(raw_origin) not in allowed:
            return PlainTextResponse(
                "CSRF check failed: Origin not in allowed list.",
                status_code=403,
            )
        return await call_next(request)

    # 2. Fall back to Referer (older browsers / direct form POSTs)
    referer_orig = _referer_origin(request)
    if referer_orig is not None:
        if referer_orig not in allowed:
            return PlainTextResponse(
                "CSRF check failed: Referer not in allowed list.",
                status_code=403,
            )
        return await call_next(request)

    # 3. Neither header present — only reject if the request carries an auth
    #    cookie (i.e. it is a browser session that should include Origin).
    if request.cookies.get(auth_cookie_name):
        return PlainTextResponse(
            "CSRF check failed: Origin/Referer header required for cookie-authenticated requests.",
            status_code=403,
        )

    # No cookie, no origin header — not a browser cookie flow, pass through.
    return await call_next(request)
