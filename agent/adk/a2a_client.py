"""Reusable Google-A2A client for sending an A2A envelope to a remote agent.

Used by both the bank (Bank→NPCI outbound: proposal_acknowledged, queries, …)
and the mock NPCI agent (NPCI→Bank pushes). Uses the ``a2a-sdk`` client rather
than ADK's ``RemoteA2aAgent`` — the latter is designed for *LLM sub-agent
delegation*, an awkward fit for deterministic, file-bearing protocol sends.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory

from agent.adk.message_mapping import envelope_to_a2a_message, extract_result_dict

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_AGENT_CARD_WELL_KNOWN_PATH = "/.well-known/agent-card.json"


def _split_card_url(agent_card_url: str) -> tuple[str, str]:
    """Split a full agent-card URL into (base_url, agent_card_path)."""
    parts = urlsplit(agent_card_url)
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    path = parts.path or _AGENT_CARD_WELL_KNOWN_PATH
    return base, path


async def send_a2a_envelope(
    agent_card_url: str,
    envelope_wire: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Resolve the remote agent card, send the envelope as an A2A message, and
    return the parsed JSON result dict from the response."""
    base_url, card_path = _split_card_url(agent_card_url)
    async with httpx.AsyncClient(timeout=timeout) as httpx_client:
        resolver = A2ACardResolver(
            httpx_client, base_url=base_url, agent_card_path=card_path
        )
        card = await resolver.get_agent_card()
        factory = ClientFactory(ClientConfig(httpx_client=httpx_client, streaming=False))
        client = factory.create(card)

        message = envelope_to_a2a_message(envelope_wire)
        events: list[Any] = []
        async for event in client.send_message(message):
            events.append(event)
        return extract_result_dict(events)
