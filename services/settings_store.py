"""Runtime-editable configuration surfaced in the portal Settings page.

Values are stored in the ``app_settings`` table and override environment
defaults via :func:`get_setting`. Reads are defensive: if the DB is
unavailable the resolver falls back to the environment so the app keeps working.

The SETTINGS_SCHEMA drives both the resolver (env fallback + secret masking) and
the Settings UI (which fields to render, grouped by category).
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from database import AppSetting, SessionLocal

# Ordered category metadata (drives the Settings sub-nav + section headers).
CATEGORY_META: dict[str, dict[str, str]] = {
    "npci_connection": {
        "label": "NPCI Connection",
        "description": "How this partner node reaches the NPCI A2A platform for all outbound messages.",
    },
    "partner_profile": {
        "label": "Partner Profile",
        "description": "Your bank's capability brief (PARTNER.md). The feasibility analyser and the "
                       "design / code / testing agents read this as context when evaluating an incoming "
                       "NPCI change. Upload a .md or edit it inline.",
    },
    "ai_drafting": {
        "label": "AI Drafting",
        "description": "The generative-AI credential used for embeddings, summaries, and grounded answers.",
    },
    "a2a_security": {
        "label": "A2A Security",
        "description": "NPCI-issued secrets used to validate inbound A2A calls. NPCI returns these once at "
                       "partner-create or after rotate — paste them here.",
    },
    "code_repository": {
        "label": "Code Repository",
        "description": "Optional Git repository where generated code artifacts are pushed for review.",
    },
}

# category → list of field descriptors.
#   secret:   render as password + mask on read
#   required: counts toward the "N of 5 required credentials configured" header
#   multiline: render as a textarea (e.g. the Markdown profile)
SETTINGS_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "npci_connection": [
        {"key": "NPCI_AGENT_URL", "label": "NPCI A2A Service URL", "env": "NPCI_AGENT_URL",
         "required": True, "placeholder": "https://a2a.npci.org.in/rpc",
         "help": "Base JSON-RPC endpoint this node calls for every outbound A2A message."},
        {"key": "NPCI_PLATFORM_URL", "label": "NPCI Platform URL", "env": "NPCI_PLATFORM_URL",
         "placeholder": "https://platform.npci.org.in",
         "help": "Web console URL for reference/links. Optional."},
        {"key": "NPCI_API_KEY", "label": "Partner API Key", "env": "NPCI_API_KEY", "secret": True,
         "required": True,
         "help": "Bearer key NPCI issued to this partner; authenticates outbound calls."},
    ],
    "partner_profile": [
        {"key": "PARTNER_NAME", "label": "Partner Name", "env": "A2A_PARTNER_NAME",
         "required": True, "placeholder": "<YOUR ORG NAME>"},
        {"key": "PARTNER_PROFILE_VERSION", "label": "Profile Version", "env": "",
         "placeholder": "0.1"},
        {"key": "PARTNER_PROFILE_MD", "label": "Profile (Markdown)", "env": "", "multiline": True,
         "placeholder": "# Partner Profile — <YOUR ORG NAME>\n\n> Your organisation's authoritative engineering brief.",
         "help": "Loaded as context by the feasibility agent when evaluating any incoming change — "
                 "every statement here is treated as load-bearing fact."},
    ],
    "ai_drafting": [
        {"key": "GOOGLE_API_KEY", "label": "LLM API Key", "env": "GOOGLE_API_KEY", "secret": True,
         "required": True, "placeholder": "AIza… — paste from Google AI Studio",
         "help": "Google Gemini API key used for embeddings, summaries, and grounded RAG answers."},
    ],
    "a2a_security": [
        {"key": "NPCI_JWT_SECRET", "label": "NPCI JWT Signing Secret", "env": "NPCI_JWT_SECRET",
         "secret": True, "required": True,
         "placeholder": "64-char hex — paste from NPCI rotate-jwt-secret response",
         "help": "Validates the Bearer JWT NPCI signs on inbound /a2a/external calls."},
        {"key": "A2A_HMAC_SECRET", "label": "NPCI HMAC Envelope Secret", "env": "A2A_HMAC_SECRET",
         "secret": True, "required": True,
         "placeholder": "64-char hex — paste from NPCI rotate-hmac-secret response",
         "help": "Validates X-NPCI-Signature on inbound A2A requests (replay + tamper protection)."},
    ],
    "code_repository": [
        {"key": "CODE_REPO_URL", "label": "Repository URL", "env": "",
         "placeholder": "https://github.com/your-org/artifacts.git"},
        {"key": "CODE_REPO_BRANCH", "label": "Default Branch", "env": "", "placeholder": "main"},
        {"key": "CODE_REPO_TOKEN", "label": "Access Token", "env": "", "secret": True,
         "help": "Personal access / deploy token with push permission. Optional."},
    ],
}

# key -> descriptor, for O(1) lookup
_FIELDS: dict[str, dict[str, Any]] = {
    f["key"]: {**f, "category": cat}
    for cat, fields in SETTINGS_SCHEMA.items()
    for f in fields
}

_lock = threading.Lock()
_cache: dict[str, Optional[str]] = {}
_cache_loaded = False


def _load_cache() -> None:
    global _cache_loaded
    with _lock:
        if _cache_loaded:
            return
        try:
            db = SessionLocal()
            try:
                _cache.update({row.key: row.value for row in db.query(AppSetting).all()})
            finally:
                db.close()
            _cache_loaded = True
        except Exception:
            # DB unavailable — leave cache empty; get_setting falls back to env.
            pass


def invalidate_cache() -> None:
    global _cache_loaded
    with _lock:
        _cache.clear()
        _cache_loaded = False


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a setting: DB override → environment → default (fail-open)."""
    _load_cache()
    if key in _cache and _cache[key] not in (None, ""):
        return _cache[key]
    env_name = _FIELDS.get(key, {}).get("env") or key
    env_val = os.getenv(env_name)
    if env_val not in (None, ""):
        return env_val
    return default


def get_grouped(*, mask_secrets: bool = True) -> dict[str, Any]:
    """Return settings as an ordered list of categories for the Settings UI.

    Each category carries its label/description, a ``configured`` flag (all
    required fields set), and its fields. The top level also reports how many
    categories have their required credentials configured (``N of total``).
    """
    _load_cache()
    categories: list[dict[str, Any]] = []
    configured_count = 0
    for category, fields in SETTINGS_SCHEMA.items():
        items = []
        required_all_set = True
        has_required = False
        for f in fields:
            is_secret = bool(f.get("secret"))
            raw = get_setting(f["key"])
            is_set = bool(raw)
            if f.get("required"):
                has_required = True
                required_all_set = required_all_set and is_set
            if is_secret and mask_secrets:
                value = "" if not raw else "********"
            else:
                value = raw or ""
            items.append({
                "key": f["key"],
                "label": f["label"],
                "type": f.get("type", "text"),
                "secret": is_secret,
                "required": bool(f.get("required")),
                "multiline": bool(f.get("multiline")),
                "placeholder": f.get("placeholder", ""),
                "help": f.get("help", ""),
                "set": is_set,
                "value": value,
            })
        configured = has_required and required_all_set
        if configured:
            configured_count += 1
        meta = CATEGORY_META.get(category, {})
        categories.append({
            "key": category,
            "label": meta.get("label", category),
            "description": meta.get("description", ""),
            "configured": configured,
            "fields": items,
        })
    return {
        "categories": categories,
        "required_configured": configured_count,
        "required_total": len(SETTINGS_SCHEMA),
    }


def set_settings(updates: dict[str, str]) -> list[str]:
    """Upsert settings. Unknown keys and unchanged secret placeholders are skipped.

    Returns the list of keys actually written.
    """
    written: list[str] = []
    db = SessionLocal()
    try:
        for key, value in updates.items():
            field = _FIELDS.get(key)
            if not field:
                continue  # ignore unknown keys
            # Do not overwrite a secret with the masked placeholder.
            if field.get("secret") and value == "********":
                continue
            row = db.query(AppSetting).filter(AppSetting.key == key).first()
            if row:
                row.value = value
            else:
                db.add(AppSetting(
                    key=key,
                    value=value,
                    category=field["category"],
                    is_secret=bool(field.get("secret")),
                ))
            written.append(key)
        db.commit()
    finally:
        db.close()
    invalidate_cache()
    return written
