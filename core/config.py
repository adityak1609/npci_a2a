"""Shared configuration and logging helpers."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default="-",
)


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class JsonFormatter(logging.Formatter):
    """Format logs as compact JSON for container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


_logging_configured = False


def configure_logging() -> None:
    """Configure process-wide logging once.

    LOG_FORMAT=json (default) — compact JSON lines for container log
    collectors. LOG_FORMAT=text — human-readable lines for local debugging:

        2026-07-13 14:02:11 WARNING rag.retriever [req-abc123] BM25 unavailable...

    LOG_LEVEL (default INFO) controls the root level either way.
    """
    global _logging_configured
    if _logging_configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").strip().lower() == "text":
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    else:
        handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    _logging_configured = True


# ---------------------------------------------------------------------------
# Global Constants
# ---------------------------------------------------------------------------

# Gemini model for chat/generation
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Fallback models, tried in order when the primary model persistently returns a
# transient/unavailable error.  A single provider outage on one model no longer
# takes the whole chat surface down.  Configure via GEMINI_FALLBACK_MODELS
# (comma-separated); empty string disables fallback.
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.0-flash").split(",")
    if m.strip()
]


def gemini_models() -> list[str]:
    """Primary model followed by configured fallbacks, de-duplicated in order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for model in (GEMINI_MODEL, *GEMINI_FALLBACK_MODELS):
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


# Transient error codes worth retrying
_RETRYABLE = ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL"]
_RETRYABLE_LLM = (
    "429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
    "500", "INTERNAL", "ReadTimeout", "DeadlineExceeded",
)


def is_retryable_llm_error(err: str) -> bool:
    """True if an LLM error string looks transient and worth retry/fallback."""
    return any(code in err for code in _RETRYABLE_LLM)


MAX_RETRIES = 2

# Number of past messages to include in context window
CHAT_MEMORY_SIZE = 5  # Last N user+assistant message PAIRS = 10 messages total
VERIFIED_STREAM_CHUNK_SIZE = 96
