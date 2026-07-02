"""
a2a_audit.py — Centralized A2A message audit logging and idempotency checks.

Every accepted AND rejected A2A message gets a row in the ``a2a_messages``
table (spec §10).  This module also provides transport-level and business-key
idempotency lookups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from a2a_models import A2AEnvelope, A2ATaskType
from database import A2AIdempotencyKey, A2AMessage


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def log_a2a_message(
    db: Session,
    *,
    envelope: Optional[A2AEnvelope] = None,
    message_id: str,
    task_type: str,
    direction: str,  # "inbound" | "outbound"
    change_id: Optional[str] = None,
    cflow_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    agent_run_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    caller_ip: Optional[str] = None,
    jwt_sub: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    payload_ref: Optional[str] = None,
    result_payload: Optional[str] = None,
    http_status: Optional[int] = None,
) -> A2AMessage:
    """Create one a2a_messages audit row.

    Called for BOTH successful and rejected messages.  For rejections,
    ``error_code`` is set to the spec error code string.

    If an ``envelope`` is passed, its fields are used as defaults for
    any parameters not explicitly provided.
    """
    if envelope:
        message_id = message_id or envelope.message_id
        task_type = task_type or envelope.task_type
        change_id = change_id or envelope.change_id
        cflow_id = cflow_id or envelope.cflow_id
        agent_id = agent_id or envelope.agent_id
        agent_run_id = agent_run_id or envelope.agent_run_id
        correlation_id = correlation_id or envelope.correlation_id

    row = A2AMessage(
        message_id=message_id,
        protocol_version="1.0",
        task_type=task_type,
        direction=direction,
        change_id=change_id,
        cflow_id=cflow_id,
        agent_id=agent_id,
        agent_run_id=agent_run_id,
        correlation_id=correlation_id,
        caller_ip=caller_ip,
        jwt_sub=jwt_sub,
        latency_ms=latency_ms,
        error_code=error_code,
        payload_ref=payload_ref,
        result_payload=result_payload,
        http_status=http_status,
    )

    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception as exc:
        db.rollback()
        print(f"[audit] Failed to log a2a_message (message_id={message_id})")
        raise RuntimeError(f"Failed to write A2A audit message {message_id}") from exc

    return row


def log_a2a_rejection(
    db: Session,
    *,
    raw_body: bytes,
    caller_ip: str,
    error_code: str,
    detail: str,
    http_status: int = 403,
) -> None:
    """Log a rejected A2A request that failed before envelope parsing.

    Uses a synthetic message_id derived from the body hash + timestamp
    to avoid unique constraint issues.
    """
    import hashlib

    body_hash = hashlib.sha256(raw_body).hexdigest()[:16]
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    synthetic_id = f"reject_{body_hash}_{now_ts}"

    try:
        # Try to extract task_type from body for better audit context
        body_json = json.loads(raw_body)
        params = body_json.get("params", {}) if isinstance(body_json, dict) else {}
        task_type = params.get("task_type", "unknown") if isinstance(params, dict) else "unknown"
    except Exception:
        task_type = "unparseable"

    log_a2a_message(
        db,
        message_id=synthetic_id,
        task_type=task_type,
        direction="inbound",
        caller_ip=caller_ip,
        error_code=error_code,
        payload_ref=detail[:2000],  # Truncate detail for storage
        http_status=http_status,
    )


# ---------------------------------------------------------------------------
# Idempotency — transport-level (message_id)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdempotencyClaim:
    key_type: str
    key_value: str


def _cached_idempotency_result(row: A2AIdempotencyKey) -> dict:
    if row.status == "succeeded" and row.result_payload:
        try:
            payload = json.loads(row.result_payload)
            if isinstance(payload, dict):
                payload.setdefault("idempotent_replay", True)
                return payload
        except (json.JSONDecodeError, TypeError):
            pass

    if row.status == "failed":
        return {
            "idempotent_replay": True,
            "status": "failed",
            "original_error_code": row.error_code or "handler_error",
        }

    return {
        "idempotent_replay": True,
        "status": "processing",
        "message": "A matching A2A request is already being processed.",
    }


def claim_idempotency_key(
    db: Session,
    *,
    key_type: str,
    key_value: str,
    message_id: str,
    task_type: str,
) -> tuple[IdempotencyClaim | None, dict | None]:
    """Atomically claim an inbound A2A transport or business idempotency key."""
    row = A2AIdempotencyKey(
        key_type=key_type,
        key_value=key_value,
        message_id=message_id,
        task_type=task_type,
        status="processing",
    )
    try:
        db.add(row)
        db.commit()
        return IdempotencyClaim(key_type=key_type, key_value=key_value), None
    except IntegrityError:
        db.rollback()
        existing = db.query(A2AIdempotencyKey).filter(
            A2AIdempotencyKey.key_type == key_type,
            A2AIdempotencyKey.key_value == key_value,
        ).first()
        if existing:
            return None, _cached_idempotency_result(existing)
        return None, {
            "idempotent_replay": True,
            "status": "processing",
            "message": "A matching A2A request is already being processed.",
        }


def complete_idempotency_claims(
    db: Session,
    claims: list[IdempotencyClaim],
    *,
    status: str,
    result: dict | None = None,
    error_code: str | None = None,
    http_status: int | None = None,
) -> None:
    """Mark claimed idempotency keys with their final replay payload."""
    if not claims:
        return

    result_payload = json.dumps(result, default=str) if result is not None else None
    now = datetime.now(timezone.utc)
    for claim in claims:
        db.query(A2AIdempotencyKey).filter(
            A2AIdempotencyKey.key_type == claim.key_type,
            A2AIdempotencyKey.key_value == claim.key_value,
        ).update(
            {
                "status": status,
                "result_payload": result_payload,
                "error_code": error_code,
                "http_status": http_status,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    db.commit()


# ---------------------------------------------------------------------------
# Idempotency — business-level (per-type composite key)
# ---------------------------------------------------------------------------

# Business keys per task_type.  Each returns a tuple of field values that
# constitute the unique business identity of the message.  If a new
# message_id arrives with the same business key, it's a legitimate retry
# that should be deduped.

def _biz_key_change_communication(params: dict) -> Optional[tuple]:
    change_id = params.get("change_id")
    version = params.get("version")
    if change_id and version is not None:
        return ("change_communication", change_id, str(version))
    return None


def _biz_key_cert_case_result(params: dict) -> Optional[tuple]:
    case_id = params.get("case_id")
    attempt = params.get("attempt") or params.get("cert_attempt")
    if case_id and attempt is not None:
        return ("cert_case_result", case_id, str(attempt))
    return None


def _biz_key_query(params: dict) -> Optional[tuple]:
    change_id = params.get("change_id")
    query_id = params.get("query_id")
    if change_id and query_id:
        return ("query", change_id, query_id)
    return None


def _biz_key_proposal_acknowledged(params: dict) -> Optional[tuple]:
    change_id = params.get("change_id")
    version = params.get("version_received") or params.get("version")
    if change_id and version is not None:
        return ("proposal_acknowledged", change_id, str(version))
    return None


_BUSINESS_KEY_EXTRACTORS: dict[str, callable] = {
    A2ATaskType.CHANGE_COMMUNICATION: _biz_key_change_communication,
    A2ATaskType.CERT_CASE_RESULT: _biz_key_cert_case_result,
    A2ATaskType.QUERY: _biz_key_query,
    A2ATaskType.PROPOSAL_ACKNOWLEDGED: _biz_key_proposal_acknowledged,
}


def check_business_key_idempotency(
    db: Session,
    task_type: str,
    payload: dict,
) -> Optional[dict]:
    """Check if a message with the same business key was already processed.

    Returns the cached result if found, else None.
    """
    extractor = _BUSINESS_KEY_EXTRACTORS.get(task_type)
    if not extractor:
        return None  # No business key defined for this task_type

    biz_key = extractor(payload)
    if not biz_key:
        return None  # Fields missing — can't compute business key

    biz_key_str = "|".join(biz_key)
    existing = db.query(A2AIdempotencyKey).filter(
        A2AIdempotencyKey.key_type == "business",
        A2AIdempotencyKey.key_value == biz_key_str,
    ).first()
    if not existing:
        return None
    return _cached_idempotency_result(existing)


def compute_business_key_marker(task_type: str, payload: dict) -> Optional[str]:
    """Compute the business key marker string for storage in payload_ref.

    This is stored alongside the payload so business-key idempotency lookups
    can find it via ``LIKE`` or ``contains``.
    """
    extractor = _BUSINESS_KEY_EXTRACTORS.get(task_type)
    if not extractor:
        return None
    biz_key = extractor(payload)
    if not biz_key:
        return None
    return "|".join(biz_key)
