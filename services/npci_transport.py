"""Outbound Bank→NPCI transport selector.

Chooses the wire transport for every Bank→NPCI message based on the
``NPCI_TRANSPORT`` setting (env or runtime settings store):

    ``legacy`` (default) → custom NPCI JSON-RPC + HMAC (``services.npci_client``)
    ``a2a``              → Google A2A via ADK       (``services.npci_a2a_client``)

Callers import ``post_npci_rpc`` from here instead of from a concrete client, so
the cutover is a single config flip with no code change — the parallel-run
strategy from the migration plan.
"""

from __future__ import annotations

import os
from typing import Any


def _transport() -> str:
    from services.settings_store import get_setting

    return (get_setting("NPCI_TRANSPORT", os.getenv("NPCI_TRANSPORT", "legacy")) or "legacy").strip().lower()


async def post_npci_rpc(rpc_payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an outbound Bank→NPCI JSON-RPC payload over the selected transport."""
    if _transport() == "a2a":
        from services.npci_a2a_client import post_npci_rpc as _post
    else:
        from services.npci_client import post_npci_rpc as _post
    return await _post(rpc_payload)
