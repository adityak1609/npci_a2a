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

from config import env_int
from metrics import inc_counter


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
def record_login_failure(email: str) -> None:
    """Record a login failure for the given email to enforce lockout."""
    # We leverage the same rate limiter mechanics, but with a longer window
    # 5 failures in 15 minutes = locked out for the remainder of the window.
    try:
        check_rate_limit("login_lockout", email.lower(), limit=4, window_seconds=900)
    except HTTPException:
        pass  # Just recording, check_login_lockout will enforce

def check_login_lockout(email: str) -> None:
    """Check if the account is currently locked due to too many failed attempts."""
    # Attempting to 'check' with limit=5 effectively enforces the lockout
    try:
        check_rate_limit("login_lockout", email.lower(), limit=5, window_seconds=900)
    except HTTPException as e:
        if e.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            # Mask the rate limit error as an auth error to not leak information,
            # or keep it as 429. Security best practice for lockout is usually a generic message
            # or explicit locked message. We'll throw 403 Forbidden or 429.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account temporarily locked due to multiple failed login attempts. Please try again later.",
                headers=e.headers,
            )
        raise
