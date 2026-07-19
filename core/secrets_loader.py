"""Load sensitive environment variables from *_FILE paths.

This supports production secret managers that mount files instead of exposing
secret values directly in process env. If FOO is already set, FOO_FILE will not
override it.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRET_ENV_NAMES = (
    "SECRET_KEY",
    "A2A_HMAC_SECRET",
    "NPCI_API_KEY",
    "GOOGLE_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "SMTP_PASS",
    "METRICS_BEARER_TOKEN",
)


# Guard so repeated calls from multiple modules at import time are no-ops.
_loaded: bool = False


def load_secret_files() -> None:
    """Load sensitive env vars from *_FILE paths exactly once per process.

    Each ``FOO_FILE`` env var points to a file containing the value for ``FOO``.
    If ``FOO`` is already set in the environment it is left untouched.
    Subsequent calls after the first are cheap no-ops.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    for name in SECRET_ENV_NAMES:
        if os.getenv(name):
            continue
        file_path = os.getenv(f"{name}_FILE")
        if not file_path:
            continue
        value = Path(file_path).read_text(encoding="utf-8").strip()
        if value:
            os.environ[name] = value
