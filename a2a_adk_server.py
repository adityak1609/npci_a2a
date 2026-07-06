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

import os

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from secrets_loader import load_secret_files

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


# Build the deterministic root agent and expose it as an A2A ASGI app. to_a2a
# auto-generates the agent card (skills/capabilities) from the agent metadata.
from agent.adk.bank_agent import build_root_agent  # noqa: E402  (after env bootstrap)

root_agent = build_root_agent()
a2a_app = to_a2a(root_agent, port=_bank_a2a_port())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(a2a_app, host="0.0.0.0", port=_bank_a2a_port())
