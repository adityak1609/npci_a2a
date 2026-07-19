"""Rate limiting for security-sensitive endpoints.

Production uses Redis when REDIS_URL is configured. Local development falls
back to the in-process limiter so tests and demos do not need external
infrastructure.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status

from core.config import env_int
from core.metrics import inc_counter

_lock = threading.Lock()
_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_redis_client = None


def _env_int(name: str, default: int) -> int:
    return env_int(name, default)


def _get_redis_client():
    """Return a cached Redis client when REDIS_URL is configured."""
    global _redis_client
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    if _redis_client is not None:
        return _redis_client

    try:
        import redis
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis rate limiter is configured but the redis package is unavailable.",
        ) from exc

    try:
        _redis_client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        _redis_client = None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis rate limiter is unavailable.",
        ) from exc


def _check_redis_rate_limit(
    scope: str,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    client = _get_redis_client()
    if client is None:
        return

    bucket_key = f"rate:{scope}:{key or 'unknown'}"
    try:
        # Atomic pipeline: INCR and EXPIRE run in a single round-trip.
        # expire(..., nx=True) only sets the TTL when the key is brand-new,
        # so a pre-existing TTL is never reset mid-window.  This eliminates
        # the race condition where a crash between a bare INCR and a separate
        # EXPIRE call would leave the key without an expiry, permanently
        # locking out the user.
        pipe = client.pipeline()
        pipe.incr(bucket_key)
        pipe.expire(bucket_key, window_seconds, nx=True)
        count, _ = pipe.execute()
        ttl = client.ttl(bucket_key)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis rate limiter is unavailable.",
        ) from exc

    if count > limit:
        inc_counter("rate_limit_rejections_total", {"scope": scope})
        retry_after = max(1, int(ttl if ttl and ttl > 0 else window_seconds))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


def check_rate_limit(scope: str, key: str, *, limit: int, window_seconds: int = 60) -> None:
    """Reject when ``key`` exceeds ``limit`` events in ``window_seconds``."""
    if os.getenv("REDIS_URL", "").strip():
        try:
            _check_redis_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
            return
        except HTTPException as e:
            if e.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
                # Circuit breaker: Redis is down. Log warning and fall through to in-memory fallback.
                import logging
                logging.getLogger(__name__).warning(
                    "Redis rate limiter unavailable; falling back to in-memory limits. Reason: %s", e.detail
                )
            else:
                raise

    now = time.monotonic()
    bucket_key = (scope, key or "unknown")

    with _lock:
        bucket = _buckets[bucket_key]
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            inc_counter("rate_limit_rejections_total", {"scope": scope})
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please retry later.",
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)


def check_chat_rate_limit(user_key: str) -> None:
    check_rate_limit(
        "chat",
        user_key,
        limit=_env_int("CHAT_RATE_LIMIT_PER_MINUTE", 30),
        window_seconds=60,
    )


def check_a2a_external_rate_limit(scope: str) -> None:
    check_rate_limit(scope, scope, limit=env_int("A2A_EXTERNAL_RATE_LIMIT_PER_MINUTE", 100))

# --- Account Lockout Logic ---
#
# Lockout state lives in Postgres (login_failures table), NOT in the
# Redis/in-memory limiter above.  The limiter fails open to per-process
# memory when Redis is down, which would multiply the allowed attempts by
# the number of Gunicorn workers — unacceptable for a security control.
# Postgres is already a hard dependency of the login path, so a DB outage
# cannot weaken the lockout (login itself is down).

def record_login_failure(email: str) -> None:
    """Persist a failed login attempt for lockout accounting.

    Never raises: this runs inside the auth-failure handler and must not
    convert a 401 into a 500 if the insert fails.
    """
    from database import LoginFailure, SessionLocal

    db = SessionLocal()
    try:
        db.add(LoginFailure(email=email.strip().lower()))
        db.commit()
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Failed to record login failure")
    finally:
        db.close()


def check_login_lockout(email: str) -> None:
    """Raise 403 when the account has too many recent failed logins.

    Policy: LOGIN_LOCKOUT_THRESHOLD failures (default 5) within
    LOGIN_LOCKOUT_WINDOW_SECONDS (default 900) lock the account for the
    remainder of the window, measured from the oldest failure in it.
    """
    from datetime import datetime, timedelta, timezone

    from database import LoginFailure, SessionLocal

    threshold = _env_int("LOGIN_LOCKOUT_THRESHOLD", 5)
    window_seconds = _env_int("LOGIN_LOCKOUT_WINDOW_SECONDS", 900)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)

    db = SessionLocal()
    try:
        recent = (
            db.query(LoginFailure.created_at)
            .filter(
                LoginFailure.email == email.strip().lower(),
                LoginFailure.created_at >= cutoff,
            )
            .order_by(LoginFailure.created_at)
            .limit(threshold)
            .all()
        )
    finally:
        db.close()

    if len(recent) >= threshold:
        oldest = recent[0][0]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        retry_after = max(1, int((oldest + timedelta(seconds=window_seconds) - now).total_seconds()))
        inc_counter("rate_limit_rejections_total", {"scope": "login_lockout"})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account temporarily locked due to multiple failed login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )
