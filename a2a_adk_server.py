"""ADK/Google-A2A server entrypoint for the bank agent (parallel to main.py).

Runs the bank's deterministic A2A agent as a standalone Google-A2A server so the
real (or mock) NPCI agent can talk to it over the A2A protocol. It shares the
same database and service layer as the main FastAPI app but runs as its own
process/port — mirroring how ``mock_npci_agent`` runs separately — to avoid
nesting ASGI lifespans with main.py's heavy startup (migrations, recovery).

Run:
    uvicorn a2a_adk_server:a2a_app --host 0.0.0.0 --port 8002

The agent card is auto-generated and served at:
    http://<host>:<port>/.well-known/agent-card.json

The legacy custom endpoint (POST /a2a/external, 8-layer HMAC/mTLS auth) in
main.py is untouched — this is the additive, flag-selectable A2A transport.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from core.secrets_loader import load_secret_files

logger = logging.getLogger(__name__)

load_dotenv()
load_secret_files()

# Dev convenience: this server runs as its own process (separate from main.py),
# so ensure the schema exists when not running under Alembic-managed production.
if os.getenv("APP_ENV", "development").strip().lower() != "production":
    from database import init_db

    init_db()


def _bank_a2a_port() -> int:
    try:
        return int(os.getenv("BANK_A2A_PORT", "8002"))
    except (TypeError, ValueError):
        return 8002


def _bank_a2a_host() -> str:
    return os.getenv("BANK_A2A_HOST", "localhost")


def _build_secured_card(agent, host: str, port: int, protocol: str = "http"):
    """Build the agent card advertising the bearer `securityScheme`.

    Per the NPCI spec's transport stack, the protocol layers ride on top of the
    Google A2A SDK: callers present the JWT bearer (spec §4.1 L4) **and** the
    HMAC envelope headers (L3, X-NPCI-Timestamp/Nonce/Signature — expressible
    only in prose here, as A2A securitySchemes has no HMAC-header type).
    Enforcement lives in ``agent/adk/transport_auth.py``; the card only tells
    clients which credentials to present. Best-effort: if ADK's (experimental)
    card builder API shifts, fall back to the auto-generated card so the server
    still boots (transport auth is unaffected — it does not depend on the card).
    """
    from a2a.types import HTTPAuthSecurityScheme, SecurityScheme
    from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder

    scheme = SecurityScheme(
        root=HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="JWT",
            description=(
                "Bank-issued partner JWT, presented as `Authorization: Bearer "
                "<token>`. Requests must ALSO carry the NPCI HMAC envelope "
                "headers (X-NPCI-Timestamp, X-NPCI-Nonce, X-NPCI-Signature; "
                "signature = HMAC-SHA256(secret, '{ts}.{nonce}.{sha256(body)}')) "
                "per Unified AgentNxt A2A spec §4.1."
            ),
        )
    )
    builder = AgentCardBuilder(
        agent=agent,
        rpc_url=f"{protocol}://{host}:{port}/",
        security_schemes={"bearerAuth": scheme},
    )
    card = asyncio.run(builder.build())
    card.security = [{"bearerAuth": []}]
    return card


# Build the deterministic root agent and expose it as an A2A ASGI app. The card
# advertises the bearer securityScheme; transport auth (L1-L4) is applied by the
# middleware below.
from agent.adk.bank_agent import build_root_agent  # noqa: E402  (after env bootstrap)
from agent.adk.transport_auth import add_transport_auth  # noqa: E402

root_agent = build_root_agent()

try:
    _agent_card = _build_secured_card(root_agent, _bank_a2a_host(), _bank_a2a_port())
except Exception:  # noqa: BLE001
    logger.exception(
        "Could not build a secured agent card; falling back to the default "
        "(no securityScheme advertised). Transport auth is unaffected."
    )
    _agent_card = None

a2a_app = to_a2a(root_agent, port=_bank_a2a_port(), agent_card=_agent_card)
add_transport_auth(a2a_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(a2a_app, host="0.0.0.0", port=_bank_a2a_port())
