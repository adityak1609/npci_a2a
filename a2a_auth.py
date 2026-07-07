"""
a2a_auth.py — 8-layer authentication and authorization stack for /a2a/external.

Implements the NPCI A2A spec §4.1 auth chain.  Each layer short-circuits on
failure with the exact spec error code string so that NPCI SOC tooling (and
our own monitoring) can pattern-match on it.

Layer order (each short-circuits):
  1. mTLS check (reverse-proxy headers)         → mtls_required / mtls_fingerprint_mismatch
  2. CIDR allowlist                              → ip_not_allowed
  3. HMAC envelope verification                  → missing_envelope_headers / timestamp_skew /
                                                   replay_detected / signature_mismatch
  4. JWT verification + session check            → invalid_token / session_revoked / session_expired
  5. Partner status check                        → partner_inactive
  6. change_id / cflow_id ownership check        → partner_mismatch / unknown_id
  7. State-machine validity                      → invalid_state_transition
  8. Agent identity check                        → unknown_agent / agent_not_authorized_for_task

Usage:
    The ``a2a_external_auth`` async function is used as a FastAPI dependency
    on the /a2a/external route.  It returns an ``A2AAuthContext`` dataclass
    on success, or raises ``A2AAuthError`` on any failure.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote

from fastapi import Request
from sqlalchemy.orm import Session

from a2a_models import A2AEnvelope, A2AErrorCode, A2ATaskType
from config import env_bool
from database import A2AChange, A2ACflow, A2AMessage, A2ANonce, SessionLocal


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _env_list(name: str) -> list[str]:
    """Return a comma-separated env var as a list of stripped strings."""
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    return env_bool(name, default)


def _env_mapping(name: str, *, normalize_keys: bool = False) -> dict[str, str]:
    """Parse comma-separated key=value mappings from environment variables."""
    mapping: dict[str, str] = {}
    for item in _env_list(name):
        key, sep, value = item.partition("=")
        if sep and key.strip() and value.strip():
            map_key = _normalize_fingerprint(key) if normalize_keys else key.strip().lower()
            mapping[map_key] = value.strip().lower()
    return mapping


def _normalize_fingerprint(value: str) -> str:
    """Normalize colon-separated or plain certificate fingerprints."""
    return re.sub(r"[^a-fA-F0-9]", "", value or "").lower()


def _env_fingerprint_mapping(name: str) -> dict[str, str]:
    """Parse env mappings whose keys are certificate fingerprints."""
    return _env_mapping(name, normalize_keys=True)


def _client_cert_sha256(request: Request) -> str:
    """Compute SHA-256 from a proxy-forwarded PEM client certificate."""
    cert_header = request.headers.get("X-SSL-Client-Cert", "")
    if not cert_header:
        return ""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        pem = unquote(cert_header).encode("utf-8")
        cert = x509.load_pem_x509_certificate(pem)
        return cert.fingerprint(hashes.SHA256()).hex()
    except Exception:
        return ""


def _request_mtls_fingerprints(request: Request) -> set[str]:
    """Return normalized mTLS fingerprints supplied or derivable from the proxy."""
    fingerprints = {
        _normalize_fingerprint(request.headers.get("X-SSL-Client-Fingerprint", "")),
        _normalize_fingerprint(request.headers.get("X-SSL-Client-SHA256-Fingerprint", "")),
        _client_cert_sha256(request),
    }
    return {fp for fp in fingerprints if fp}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class A2AAuthError(Exception):
    """Raised when any auth layer rejects the request.

    Attributes:
        error_code: One of A2AErrorCode values (pattern-matchable string).
        detail: Human-readable detail message.
        http_status: HTTP status code to return (default 403).
    """

    def __init__(
        self,
        error_code: str,
        detail: str,
        http_status: int = 403,
    ) -> None:
        self.error_code = error_code
        self.detail = detail
        self.http_status = http_status
        super().__init__(f"[{error_code}] {detail}")


# ---------------------------------------------------------------------------
# Auth context — returned on success
# ---------------------------------------------------------------------------

@dataclass
class A2AAuthContext:
    """Context passed downstream after all 8 auth layers succeed."""

    caller_ip: str = ""
    jwt_sub: Optional[str] = None
    partner_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    envelope: Optional[A2AEnvelope] = None
    raw_body: bytes = b""


# ---------------------------------------------------------------------------
# Layer 1: mTLS (reverse-proxy headers)
# ---------------------------------------------------------------------------

def check_mtls(request: Request) -> None:
    """Verify mTLS if required via env config.

    Expects the reverse proxy to set:
      - X-SSL-Client-Verify: SUCCESS | FAILED | NONE
      - X-SSL-Client-Fingerprint: proxy-native fingerprint
      - X-SSL-Client-Cert: escaped PEM cert, used to compute SHA-256
    """
    if not _env_bool("A2A_MTLS_REQUIRED", default=True):
        return  # mTLS explicitly disabled via env — skip

    verify = request.headers.get("X-SSL-Client-Verify", "NONE").upper()
    if verify == "NONE":
        raise A2AAuthError(
            A2AErrorCode.MTLS_REQUIRED,
            "mTLS client certificate is required but was not presented.",
            http_status=403,
        )

    if verify != "SUCCESS":
        raise A2AAuthError(
            A2AErrorCode.MTLS_FINGERPRINT_MISMATCH,
            f"mTLS verification failed: {verify}",
            http_status=403,
        )

    allowed_fps = {_normalize_fingerprint(fp) for fp in _env_list("A2A_MTLS_FINGERPRINTS")}
    fingerprints = _request_mtls_fingerprints(request)
    if allowed_fps and not (fingerprints & allowed_fps):
        raise A2AAuthError(
            A2AErrorCode.MTLS_FINGERPRINT_MISMATCH,
            "Client certificate fingerprint is not in the allowlist.",
            http_status=403,
        )


# ---------------------------------------------------------------------------
# Layer 2: CIDR allowlist
# ---------------------------------------------------------------------------

def check_cidr(request: Request) -> None:
    """Reject requests from IPs outside the configured CIDR allowlist."""
    cidr_list = _env_list("A2A_CIDR_ALLOWLIST")
    if not cidr_list:
        return  # No CIDR restriction configured

    client_ip = request.client.host if request.client else "0.0.0.0"
    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        raise A2AAuthError(
            A2AErrorCode.IP_NOT_ALLOWED,
            f"Cannot parse client IP: {client_ip}",
            http_status=403,
        )

    for cidr in cidr_list:
        try:
            if client_addr in ipaddress.ip_network(cidr, strict=False):
                return  # Allowed
        except ValueError:
            continue  # Skip malformed CIDR entries

    raise A2AAuthError(
        A2AErrorCode.IP_NOT_ALLOWED,
        f"Client IP {client_ip} is not in the allowed CIDR ranges.",
        http_status=403,
    )


# ---------------------------------------------------------------------------
# Layer 3: HMAC envelope verification
# ---------------------------------------------------------------------------

_HMAC_SKEW_SECONDS = 300  # ±5 minutes
_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{16,64}$")
_SIG_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


def verify_hmac(request: Request, raw_body: bytes, db: Session) -> None:
    """Verify HMAC-SHA256 signature, timestamp, and nonce.

    Expected headers:
        X-NPCI-Timestamp: Unix epoch seconds (string)
        X-NPCI-Nonce:     Random unique string per request
        X-NPCI-Signature: HMAC-SHA256(secret, "{ts}.{nonce}.{sha256_hex(body)}")
    """
    ts_header = request.headers.get("X-NPCI-Timestamp")
    nonce_header = request.headers.get("X-NPCI-Nonce")
    sig_header = request.headers.get("X-NPCI-Signature")

    if not ts_header or not nonce_header or not sig_header:
        raise A2AAuthError(
            A2AErrorCode.MISSING_ENVELOPE_HEADERS,
            "Missing one or more required HMAC headers: "
            "X-NPCI-Timestamp, X-NPCI-Nonce, X-NPCI-Signature.",
            http_status=401,
        )

    if not _NONCE_RE.fullmatch(nonce_header):
        raise A2AAuthError(
            A2AErrorCode.MISSING_ENVELOPE_HEADERS,
            "X-NPCI-Nonce must be 16-64 URL-safe characters.",
            http_status=401,
        )

    if not _SIG_RE.fullmatch(sig_header):
        raise A2AAuthError(
            A2AErrorCode.SIGNATURE_MISMATCH,
            "X-NPCI-Signature must be a 64-character hex HMAC-SHA256 digest.",
            http_status=401,
        )

    # --- Timestamp skew ---
    try:
        request_ts = int(ts_header)
    except (ValueError, TypeError):
        raise A2AAuthError(
            A2AErrorCode.TIMESTAMP_SKEW,
            "X-NPCI-Timestamp is not a valid integer.",
            http_status=401,
        )

    now_ts = int(time.time())
    if abs(now_ts - request_ts) > _HMAC_SKEW_SECONDS:
        raise A2AAuthError(
            A2AErrorCode.TIMESTAMP_SKEW,
            f"Timestamp skew exceeds ±{_HMAC_SKEW_SECONDS}s. "
            f"Server={now_ts}, Request={request_ts}.",
            http_status=401,
        )

    # Verify the signature before touching nonce storage. The signature is
    # checked again below after replay detection; this early check prevents
    # unauthenticated bad-signature traffic from filling the nonce table.
    hmac_secret = os.getenv("A2A_HMAC_SECRET", "")
    if not hmac_secret:
        raise A2AAuthError(
            A2AErrorCode.SIGNATURE_MISMATCH,
            "A2A_HMAC_SECRET is not configured on this server.",
            http_status=500,
        )
    body_hash = hashlib.sha256(raw_body).hexdigest()
    signing_string = f"{ts_header}.{nonce_header}.{body_hash}"
    early_expected_sig = hmac.new(
        hmac_secret.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(early_expected_sig, sig_header):
        raise A2AAuthError(
            A2AErrorCode.SIGNATURE_MISMATCH,
            "HMAC-SHA256 signature does not match.",
            http_status=401,
        )

    # --- Nonce replay ---
    # Expired-nonce garbage collection is handled out-of-band by the periodic
    # cleanup task (_cleanup_expired_security_rows in main.py). Doing a DELETE +
    # commit on every inbound request here caused needless write amplification
    # on the hot auth path, so it has been removed. A lingering expired nonce is
    # still safe: reuse is treated as a replay (fail-closed) and the row is
    # reaped on the next cleanup tick.
    nonce_ttl = int(os.getenv("A2A_NONCE_TTL_SECONDS", "600"))
    existing_nonce = db.query(A2ANonce).filter(A2ANonce.nonce == nonce_header).first()
    if existing_nonce:
        raise A2AAuthError(
            A2AErrorCode.REPLAY_DETECTED,
            "Nonce has already been used (replay attack or duplicate request).",
            http_status=401,
        )

    # Store nonce for dedup
    db.add(A2ANonce(
        nonce=nonce_header,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=nonce_ttl),
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        # If we can't store the nonce (e.g., race condition / unique violation),
        # treat it as a replay to be safe.
        raise A2AAuthError(
            A2AErrorCode.REPLAY_DETECTED,
            "Nonce collision during storage — treating as replay.",
            http_status=401,
        )

    # --- HMAC verification ---
    hmac_secret = os.getenv("A2A_HMAC_SECRET", "")
    if not hmac_secret:
        raise A2AAuthError(
            A2AErrorCode.SIGNATURE_MISMATCH,
            "A2A_HMAC_SECRET is not configured on this server.",
            http_status=500,
        )

    body_hash = hashlib.sha256(raw_body).hexdigest()
    signing_string = f"{ts_header}.{nonce_header}.{body_hash}"
    expected_sig = hmac.new(
        hmac_secret.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, sig_header):
        raise A2AAuthError(
            A2AErrorCode.SIGNATURE_MISMATCH,
            "HMAC-SHA256 signature does not match.",
            http_status=401,
        )


# ---------------------------------------------------------------------------
# Layer 4: JWT verification + session check
# ---------------------------------------------------------------------------

def verify_jwt_session(request: Request, db: Session) -> Optional[str]:
    """Verify the JWT Bearer token and return the subject (email/partner_id).

    Returns the JWT ``sub`` claim on success.
    If no Authorization header is present and JWT is not required for the
    /a2a/external endpoint (HMAC is primary), returns None.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # JWT is optional on /a2a/external when HMAC is the primary auth.
        # If you want to enforce JWT as well, make this a hard failure.
        jwt_required = _env_bool("A2A_JWT_REQUIRED_EXTERNAL", default=True)
        if jwt_required:
            raise A2AAuthError(
                A2AErrorCode.INVALID_TOKEN,
                "Bearer token is required on this endpoint.",
                http_status=401,
            )
        return None

    token = auth_header[7:]  # Strip "Bearer "
    try:
        from auth import decode_token
        payload = decode_token(token)
    except Exception:
        raise A2AAuthError(
            A2AErrorCode.INVALID_TOKEN,
            "JWT token is invalid or expired.",
            http_status=401,
        )

    sub = payload.get("sub")
    if not sub:
        raise A2AAuthError(
            A2AErrorCode.INVALID_TOKEN,
            "JWT token is missing 'sub' claim.",
            http_status=401,
        )

    # Session revocation: a leaked or logged-out partner JWT must stop working
    # before its natural expiry.  decode_token() already enforces the exp
    # claim; here we reject any jti that has been added to revoked_sessions
    # (mirrors the browser get_current_user() check).
    jti = payload.get("jti")
    if jti:
        from database import RevokedSession

        revoked = db.query(RevokedSession).filter(RevokedSession.jti == jti).first()
        if revoked:
            raise A2AAuthError(
                A2AErrorCode.SESSION_REVOKED,
                "JWT session has been revoked.",
                http_status=401,
            )

    return sub


# ---------------------------------------------------------------------------
# Layer 5: Partner status check
# ---------------------------------------------------------------------------

def check_partner_status(partner_id: Optional[str], db: Session) -> None:
    """Check whether the partner/agent is active.

    For now, checks against A2A_KNOWN_AGENTS env var.
    Future: query a partners table with active/inactive status.
    """
    if not partner_id:
        return  # No partner_id in the envelope — skip

    known_agents = {a.lower() for a in _env_list("A2A_KNOWN_AGENTS")}
    if not known_agents:
        return  # No agent registry configured — allow all

    if partner_id.lower() not in known_agents:
        raise A2AAuthError(
            A2AErrorCode.PARTNER_INACTIVE,
            f"Partner '{partner_id}' is not recognized or is inactive.",
            http_status=403,
        )


# ---------------------------------------------------------------------------
# Layer 6: Ownership check — change_id / cflow_id
# ---------------------------------------------------------------------------

def check_credential_binding(
    request: Request,
    envelope: A2AEnvelope,
    jwt_sub: Optional[str],
) -> None:
    """Bind the claimed partner identity to JWT and mTLS credentials."""
    partner_id = (envelope.from_ or "").strip().lower()
    agent_id = (envelope.agent_id or "").strip().lower()

    if jwt_sub:
        jwt_map = _env_mapping("A2A_JWT_PARTNER_MAP")
        jwt_key = jwt_sub.strip().lower()
        if jwt_map:
            if jwt_map.get(jwt_key) != partner_id:
                raise A2AAuthError(
                    A2AErrorCode.PARTNER_MISMATCH,
                    "JWT subject is not bound to the claimed A2A partner.",
                    http_status=403,
                )
        elif jwt_key not in {partner_id, agent_id}:
            raise A2AAuthError(
                A2AErrorCode.PARTNER_MISMATCH,
                "JWT subject must match the claimed partner or agent_id.",
                http_status=403,
            )

    mtls_map = _env_fingerprint_mapping("A2A_MTLS_PARTNER_MAP")
    fingerprints = _request_mtls_fingerprints(request)
    if mtls_map and not any(mtls_map.get(fp) == partner_id for fp in fingerprints):
        raise A2AAuthError(
            A2AErrorCode.PARTNER_MISMATCH,
            "mTLS certificate fingerprint is not bound to the claimed A2A partner.",
            http_status=403,
        )


def check_partner_binding_sub(
    envelope: A2AEnvelope,
    jwt_sub: Optional[str],
) -> None:
    """Bind the claimed partner to the JWT subject **without an HTTP request**.

    The Google-A2A (ADK) path terminates inside an ADK agent that only receives
    message parts, never the raw request, so it cannot inspect mTLS fingerprints.
    This enforces the *JWT half* of :func:`check_credential_binding` (spec §4.1
    Layer 5); the mTLS-fingerprint half is enforced at the transport boundary
    (reverse proxy / ``agent/adk/transport_auth.py``) on that path.
    """
    if not jwt_sub:
        return  # No authenticated subject (JWT not required) — nothing to bind.

    partner_id = (envelope.from_ or "").strip().lower()
    agent_id = (envelope.agent_id or "").strip().lower()
    jwt_map = _env_mapping("A2A_JWT_PARTNER_MAP")
    jwt_key = jwt_sub.strip().lower()
    if jwt_map:
        if jwt_map.get(jwt_key) != partner_id:
            raise A2AAuthError(
                A2AErrorCode.PARTNER_MISMATCH,
                "JWT subject is not bound to the claimed A2A partner.",
                http_status=403,
            )
    elif jwt_key not in {partner_id, agent_id}:
        raise A2AAuthError(
            A2AErrorCode.PARTNER_MISMATCH,
            "JWT subject must match the claimed partner or agent_id.",
            http_status=403,
        )


def check_ownership(
    envelope: A2AEnvelope,
    partner_id: Optional[str],
    db: Session,
) -> None:
    """Verify that the change_id and/or cflow_id in the envelope exist
    and belong to the claimed partner.

    Skipped for task types that create new changes (e.g. change_communication).
    """
    # Task types that create new entities — don't check ownership yet
    create_types = {
        A2ATaskType.CHANGE_COMMUNICATION,
        A2ATaskType.ECHO,
        A2ATaskType.STATUS_REQUEST,
        A2ATaskType.NOTIFICATION,
    }
    if envelope.task_type in create_types:
        return

    if envelope.change_id:
        change = db.query(A2AChange).filter(
            A2AChange.change_id == envelope.change_id
        ).first()
        if not change:
            raise A2AAuthError(
                A2AErrorCode.UNKNOWN_ID,
                f"change_id '{envelope.change_id}' is not known.",
                http_status=404,
            )
        if partner_id and change.partner_id and change.partner_id != partner_id:
            raise A2AAuthError(
                A2AErrorCode.PARTNER_MISMATCH,
                f"change_id '{envelope.change_id}' does not belong to partner '{partner_id}'.",
                http_status=403,
            )

    if envelope.cflow_id:
        cflow = db.query(A2ACflow).filter(
            A2ACflow.cflow_id == envelope.cflow_id
        ).first()
        if not cflow:
            raise A2AAuthError(
                A2AErrorCode.UNKNOWN_ID,
                f"cflow_id '{envelope.cflow_id}' is not known.",
                http_status=404,
            )
        if partner_id and cflow.partner_id and cflow.partner_id != partner_id:
            raise A2AAuthError(
                A2AErrorCode.PARTNER_MISMATCH,
                f"cflow_id '{envelope.cflow_id}' does not belong to partner '{partner_id}'.",
                http_status=403,
            )


# ---------------------------------------------------------------------------
# Layer 7: State-machine validity
# ---------------------------------------------------------------------------

def check_state_transition(envelope: A2AEnvelope, db: Session) -> None:
    """Verify the task_type is valid for the current lifecycle state.

    Delegates to a2a_state.validate_transition (created in Phase 3).
    Imported lazily to avoid circular dependency during startup.
    """
    from a2a_state import validate_transition

    # Skip state checks for stateless task types
    stateless_types = {
        A2ATaskType.ECHO,
        A2ATaskType.STATUS_REQUEST,
        A2ATaskType.STATUS_RESPONSE,
        A2ATaskType.NOTIFICATION,
        A2ATaskType.QUERY,
        A2ATaskType.CLARIFICATION_RESPONSE,
    }
    if envelope.task_type in stateless_types:
        return

    try:
        validate_transition(envelope, db)
    except ValueError as exc:
        raise A2AAuthError(
            A2AErrorCode.INVALID_STATE_TRANSITION,
            str(exc),
            http_status=409,
        )


# ---------------------------------------------------------------------------
# Layer 8: Agent identity check
# ---------------------------------------------------------------------------

def check_agent_identity(envelope: A2AEnvelope) -> None:
    """Verify the agent_id is known and authorized for the given task_type."""
    known_agents = {a.lower() for a in _env_list("A2A_KNOWN_AGENTS")}
    if not known_agents:
        return  # No agent registry — allow all

    if envelope.agent_id.lower() not in known_agents:
        raise A2AAuthError(
            A2AErrorCode.UNKNOWN_AGENT,
            f"Agent '{envelope.agent_id}' is not registered.",
            http_status=403,
        )

    # Future: per-agent task_type authorization table
    # For now, any known agent can send any task_type.


# ---------------------------------------------------------------------------
# Combined auth dependency
# ---------------------------------------------------------------------------

async def a2a_external_auth(
    request: Request,
    raw_body: bytes,
    db: Session,
) -> A2AAuthContext:
    """Run all 8 auth layers in order.  Returns A2AAuthContext on success.

    Each layer short-circuits on failure by raising A2AAuthError.
    The caller (route handler) catches A2AAuthError and returns the
    appropriate JSON-RPC error response with the spec error code.
    """
    caller_ip = request.client.host if request.client else "0.0.0.0"

    # Layer 1: mTLS
    check_mtls(request)

    # Layer 2: CIDR
    check_cidr(request)

    # Layer 3: HMAC
    verify_hmac(request, raw_body, db)

    # Layer 4: JWT (optional for external, primary auth is HMAC)
    jwt_sub = verify_jwt_session(request, db)

    # Parse the JSON body to extract the envelope
    try:
        body_json = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise A2AAuthError(
            A2AErrorCode.MISSING_ENVELOPE_HEADERS,
            f"Request body is not valid JSON: {exc}",
            http_status=400,
        )

    # Extract A2A envelope from JSON-RPC params
    params = body_json.get("params") if isinstance(body_json, dict) else None
    if not isinstance(params, dict):
        raise A2AAuthError(
            A2AErrorCode.MISSING_ENVELOPE_HEADERS,
            "JSON-RPC 'params' must be an object containing the A2A envelope.",
            http_status=400,
        )

    try:
        envelope = A2AEnvelope.model_validate(params)
    except Exception as exc:
        raise A2AAuthError(
            A2AErrorCode.MISSING_ENVELOPE_HEADERS,
            f"Failed to parse A2A envelope from params: {exc}",
            http_status=400,
        )

    # Layer 5: Partner status
    partner_id = envelope.from_
    check_credential_binding(request, envelope, jwt_sub)
    check_partner_status(partner_id, db)

    # Layer 6: Ownership
    check_ownership(envelope, partner_id, db)

    # Layer 7: State machine
    check_state_transition(envelope, db)

    # Layer 8: Agent identity
    check_agent_identity(envelope)

    return A2AAuthContext(
        caller_ip=caller_ip,
        jwt_sub=jwt_sub,
        partner_id=partner_id,
        agent_id=envelope.agent_id,
        agent_run_id=envelope.agent_run_id,
        envelope=envelope,
        raw_body=raw_body,
    )
