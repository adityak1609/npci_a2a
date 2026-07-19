"""Outbound Bank→NPCI over Google A2A (ADK transport).

Drop-in replacement for ``services.npci_client.post_npci_rpc`` with the same
signature, so the auto-fire helpers in ``services.a2a_ingestor`` and the senders
in ``services.npci_outbound`` can route through it when ``NPCI_TRANSPORT=a2a``
(see ``services.npci_transport``).

The legacy JSON-RPC ``params`` object *is* the A2A envelope, so we forward it
straight to the remote agent's A2A endpoint and wrap the reply in a JSON-RPC-
shaped dict so existing callers keep working unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from agent.adk.a2a_client import send_a2a_envelope


def _npci_card_url() -> str:
    from services.settings_store import get_setting

    url = get_setting("NPCI_A2A_AGENT_CARD_URL", os.getenv("NPCI_A2A_AGENT_CARD_URL"))
    if not url:
        raise OSError(
            "NPCI_A2A_AGENT_CARD_URL must be set to the NPCI agent's A2A card URL "
            "(e.g. http://localhost:8001/.well-known/agent-card.json) when "
            "NPCI_TRANSPORT=a2a."
        )
    return url


async def post_npci_rpc(rpc_payload: dict[str, Any]) -> dict[str, Any]:
    """Send a legacy JSON-RPC payload to NPCI over A2A and return a JSON-RPC reply."""
    envelope_wire = rpc_payload.get("params") or {}
    result = await send_a2a_envelope(_npci_card_url(), envelope_wire)
    return {"jsonrpc": "2.0", "id": rpc_payload.get("id"), "result": result}
