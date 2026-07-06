"""
services/retention.py — Data retention and GDPR-compliant deletion.

Scheduled daily by the ingestion worker (worker.py). Purges expired data per
configurable retention windows to comply with data minimization principles and
reduce storage/privacy risk.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from config import env_int
from database import (
    A2ANonce,
    AuditLog,
    ChatMessage,
    ChatSession,
    LoginFailure,
    RevokedSession,
    SessionLocal,
    User,
)

logger = logging.getLogger(__name__)

# Defaults (overridable via env vars)
CHAT_RETENTION_DAYS = env_int("CHAT_RETENTION_DAYS", 90)
AUDIT_RETENTION_DAYS = env_int("AUDIT_RETENTION_DAYS", 365)
REVOKED_SESSION_RETENTION_DAYS = env_int("REVOKED_SESSION_RETENTION_DAYS", 7)


def run_retention(db: Session) -> dict[str, int]:
    """
    Execute all retention policies in a single transaction-safe run.
    Returns a dict with counts of deleted/anonymized rows.
    """
    now = datetime.now(timezone.utc)
    results: dict[str, int] = {}

    # 1. Purge old chat messages (configurable, default 90 days)
    chat_cutoff = now - timedelta(days=CHAT_RETENTION_DAYS)
    deleted_messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at < chat_cutoff)
        .delete(synchronize_session=False)
    )
    results["chat_messages_deleted"] = deleted_messages
    logger.info(f"retention: deleted {deleted_messages} chat_messages older than {CHAT_RETENTION_DAYS} days")

    # 2. Purge orphaned chat sessions (no messages remaining)
    orphaned_sessions = (
        db.query(ChatSession)
        .filter(~ChatSession.messages.any())
        .delete(synchronize_session=False)
    )
    results["chat_sessions_deleted"] = orphaned_sessions

    # 3. Purge AuditLog entries older than retention period
    # NOTE: Regulatory compliance may require a longer period. Never purge hash-chained
    # audit logs if the hash chain is used for tamper detection in immutable exports.
    # Only purge if AUDIT_RETENTION_DAYS is explicitly reduced by compliance team.
    audit_cutoff = now - timedelta(days=AUDIT_RETENTION_DAYS)
    deleted_audit = (
        db.query(AuditLog)
        .filter(AuditLog.created_at < audit_cutoff)
        .delete(synchronize_session=False)
    )
    results["audit_logs_deleted"] = deleted_audit
    logger.info(f"retention: deleted {deleted_audit} audit_logs older than {AUDIT_RETENTION_DAYS} days")

    # 4. Purge expired nonces (they have an explicit expires_at)
    deleted_nonces = (
        db.query(A2ANonce)
        .filter(A2ANonce.expires_at < now)
        .delete(synchronize_session=False)
    )
    results["a2a_nonces_purged"] = deleted_nonces

    # 5. Purge expired revoked session tokens (past their original TTL)
    revoke_cutoff = now - timedelta(days=REVOKED_SESSION_RETENTION_DAYS)
    deleted_revoked = (
        db.query(RevokedSession)
        .filter(RevokedSession.revoked_at < revoke_cutoff)
        .delete(synchronize_session=False)
    )
    results["revoked_sessions_purged"] = deleted_revoked

    # 6. Purge stale login-failure rows (lockout only looks at the last
    # LOGIN_LOCKOUT_WINDOW_SECONDS; anything older than a day is dead weight)
    deleted_login_failures = (
        db.query(LoginFailure)
        .filter(LoginFailure.created_at < now - timedelta(days=1))
        .delete(synchronize_session=False)
    )
    results["login_failures_purged"] = deleted_login_failures

    db.commit()
    logger.info(f"retention: run complete: {results}")
    return results


def anonymize_user_data(db: Session, user_id: int) -> dict[str, int]:
    """
    GDPR-compliant user data anonymization on account deletion request.

    - Anonymizes message content (replaces with '[deleted]')
    - Deletes chat sessions
    - Does NOT delete AuditLog entries (regulatory requirement)
    - Writes a final audit entry recording the deletion
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "user_not_found"}

    # Anonymize all chat messages for this user
    anonymized = 0
    for session in user.sessions:
        for msg in session.messages:
            msg.content = "[deleted - user data removal request]"
            anonymized += 1

    # Write a compliance audit entry BEFORE deleting sessions
    from database import AuditLog
    audit = AuditLog(
        admin_id=None,
        action="user_data_deleted",
        target=f"user:{user.email}",
        detail=f"GDPR deletion request for user_id={user_id}. Messages anonymized: {anonymized}.",
    )
    db.add(audit)

    # Delete sessions (cascades to messages due to ORM relationship)
    deleted_sessions = db.query(ChatSession).filter(
        ChatSession.user_id == user_id
    ).delete(synchronize_session=False)

    # Anonymize the user record itself (don't hard-delete to preserve FK integrity).
    # The account is also deactivated and its username scrubbed so it can no
    # longer log in and is dropped from the admin user list.
    user.email = f"deleted_{user_id}@deleted.invalid"
    user.hashed_password = "[deleted]"
    user.full_name = "[deleted]"
    user.is_admin = False
    user.is_active = False
    user.username = None

    db.commit()
    return {
        "user_id": user_id,
        "sessions_deleted": deleted_sessions,
        "messages_anonymized": anonymized,
    }
