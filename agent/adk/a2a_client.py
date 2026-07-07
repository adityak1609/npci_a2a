"""Reusable Google-A2A client for sending an A2A envelope to a remote agent.

Used by both the bank (Bank→NPCI outbound: proposal_acknowledged, queries, …)
and the mock NPCI agent (NPCI→Bank pushes). Uses the ``a2a-sdk`` client rather
than ADK's ``RemoteA2aAgent`` — the latter is designed for *LLM sub-agent
delegation*, an awkward fit for deterministic, file-bearing protocol sends.

Per the spec's transport stack, the NPCI protocol layers ride ON TOP of the
Google A2A SDK: every send carries the **HMAC envelope headers** (spec §4.1
L3) and the **JWT bearer** (L4), exactly like ``services.npci_client`` does on
the legacy wire. :class:`NpciEnvelopeAuth` signs each request over the exact
bytes httpx puts on the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory

from agent.adk.message_mapping import envelope_to_a2a_message, extract_result_dict

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_AGENT_CARD_WELL_KNOWN_PATH = "/.well-known/agent-card.json"


def _outbound_setting(name: str) -> str:
    """Runtime setting with env fallback; tolerates an unreachable settings DB."""
    try:
        from services.settings_store import get_setting

        return (get_setting(name, os.getenv(name)) or "").strip()
    except Exception:  # noqa: BLE001 — settings store needs the DB; env still works
        return (os.getenv(name) or "").strip()


class NpciEnvelopeAuth(httpx.Auth):
    """Signs outbound requests with the NPCI protocol headers (spec §4.1 L3+L4).

    Adds ``Authorization: Bearer <NPCI_API_KEY>`` and the HMAC envelope
    (``X-NPCI-Timestamp`` / ``X-NPCI-Nonce`` / ``X-NPCI-Signature``, signature =
    ``HMAC-SHA256(secret, "{ts}.{nonce}.{sha256_hex(body)}")``) — the same
    contract as ``services.npci_client._signed_headers``. Each credential is
    added only when configured, so the bundled mock keeps working unsigned in
    local dev (where the receiving side's transport auth is off anyway).
    """

    requires_request_body = True  # ensure request.content is materialized

    def auth_flow(self, request: httpx.Request):
        api_key = _outbound_setting("NPCI_API_KEY")
        if api_key:
            request.headers["Authorization"] = f"Bearer {api_key}"

        hmac_secret = _outbound_setting("A2A_HMAC_SECRET")
        if hmac_secret:
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex  # 32 URL-safe chars — satisfies the nonce regex
            body_hash = hashlib.sha256(request.content).hexdigest()
            signing_string = f"{timestamp}.{nonce}.{body_hash}"
            request.headers["X-NPCI-Timestamp"] = timestamp
            request.headers["X-NPCI-Nonce"] = nonce
            request.headers["X-NPCI-Signature"] = hmac.new(
                hmac_secret.encode("utf-8"),
                signing_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        yield request


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
    async with httpx.AsyncClient(timeout=timeout, auth=NpciEnvelopeAuth()) as httpx_client:
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
