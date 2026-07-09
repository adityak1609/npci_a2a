"""Driver: push a change_communication to the bank over Google A2A (ADK).

The ADK mock NPCI agent (``mock_npci_agent/adk_agent.py``) is a message
*receiver* — unlike the legacy mock (``mock_npci_agent/main.py``) it has no HTTP
trigger UI. This script performs the NPCI→Bank "document push" (spec §2.1) over
Google A2A: it builds a ``change_communication`` envelope and sends it to the
bank's ADK agent card, then prints the bank's reply.

Prerequisites (all three started by ``start_a2a.ps1``):
    * bank portal          — uvicorn main:app                     (:8000)
    * bank ADK A2A server  — uvicorn a2a_adk_server:a2a_app        (:8002)
    * mock NPCI ADK agent  — uvicorn mock_npci_agent.adk_agent     (:8001)

Usage:
    python -m mock_npci_agent.push_change_a2a
    python -m mock_npci_agent.push_change_a2a --change-id CHG-UPI-2026-001 --file spec.pdf

Env:
    BANK_A2A_AGENT_CARD_URL  bank ADK card
                             (default http://localhost:8002/.well-known/agent-card.json)
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

# Imported after load_dotenv so the ADK modules see the same env the servers use.
from agent.adk.a2a_client import send_a2a_envelope  # noqa: E402
from mock_npci_agent.adk_agent import build_change_communication  # noqa: E402

_DEFAULT_BANK_CARD = "http://localhost:8002/.well-known/agent-card.json"


def _bank_card_url() -> str:
    return os.getenv("BANK_A2A_AGENT_CARD_URL", _DEFAULT_BANK_CARD).strip() or _DEFAULT_BANK_CARD


async def _push(change_id: str, version: int, file_path: str | None) -> None:
    pdf_bytes: bytes | None = None
    if file_path:
        with open(file_path, "rb") as fh:
            pdf_bytes = fh.read()
        if not pdf_bytes:
            raise SystemExit(f"File is empty: {file_path}")

    envelope = build_change_communication(
        change_id=change_id, version=version, pdf_bytes=pdf_bytes
    )
    card = _bank_card_url()
    print(f"-> Pushing change_communication {change_id} v{version} to {card}")
    result = await send_a2a_envelope(card, envelope)
    print(f"<- Bank replied: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push a change_communication to the bank over Google A2A (ADK)."
    )
    parser.add_argument("--change-id", default="CHG-A2A-DEMO", help="Change identifier.")
    parser.add_argument("--version", type=int, default=1, help="Document version.")
    parser.add_argument(
        "--file",
        default=None,
        help="Optional PDF/document to attach; a sample PDF is generated when omitted.",
    )
    args = parser.parse_args()
    asyncio.run(_push(args.change_id, args.version, args.file))


if __name__ == "__main__":
    main()
