"""Mock NPCI agent as a Google-A2A (ADK) counterpart for local end-to-end tests.

This is the Google-A2A sibling of ``mock_npci_agent/main.py`` (which speaks the
legacy custom NPCI protocol). It lets you exercise the *bank's* ADK/A2A path
end-to-end without the real NPCI platform:

    * runs as an ADK A2A server (``to_a2a``) at MOCK_NPCI_PORT (default 8001),
      serving its card at /.well-known/agent-card.json — so the bank's outbound
      messages (proposal_acknowledged, change_acknowledgement, query) arrive here;
    * on receiving a ``query`` it auto-replies with a ``clarification_response``
      sent back to the bank's A2A card;
    * :func:`build_change_communication` builds a sample change push that a driver
      script sends to the bank (see ``tools`` / the E2E verification).

Run:
    uvicorn mock_npci_agent.adk_agent:a2a_app --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from agent.adk.a2a_client import send_a2a_envelope
from agent.adk.message_mapping import genai_parts_to_envelope, result_to_genai_content

logger = logging.getLogger("mock_npci_adk")

AGENT_ID = os.getenv("MOCK_NPCI_AGENT_ID", "npci_central")
BANK_CARD_URL = os.getenv(
    "BANK_A2A_AGENT_CARD_URL", "http://localhost:8002/.well-known/agent-card.json"
)
AUTO_REPLY_QUERY = os.getenv("MOCK_AUTO_REPLY", "true").strip().lower() in {"1", "true", "yes"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope(task_type: str, change_id: str | None, payload: dict, correlation_id: str | None = None) -> dict:
    return {
        "task_type": task_type,
        "change_id": change_id,
        "from": AGENT_ID,
        "agent_id": AGENT_ID,
        "agent_run_id": uuid.uuid4().hex,
        "correlation_id": correlation_id or uuid.uuid4().hex,
        "timestamp": _now_iso(),
        "message_id": f"{task_type}_{uuid.uuid4().hex[:12]}",
        "protocol_version": "1.0",
        "payload": payload,
    }


def _sample_pdf(text: str) -> bytes:
    """Render a minimal but valid one-page PDF (PyMuPDF) for demo documents."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


def build_change_communication(
    change_id: str = "CHG-A2A-DEMO",
    version: int = 1,
    pdf_bytes: bytes | None = None,
) -> dict:
    """Build a sample ``change_communication`` envelope (NPCI → Bank)."""
    pdf = pdf_bytes or _sample_pdf(
        f"Mock NPCI change communication {change_id} v{version}: "
        "settlement window update per circular."
    )
    return _envelope(
        "change_communication",
        change_id,
        {
            "change_id": change_id,
            "version": version,
            "title": "Settlement window update",
            "description": "Mock NPCI change for A2A/ADK end-to-end testing.",
            "product_kit": [
                {
                    "doc_type": "circular",
                    "filename": f"{change_id}_circular.pdf",
                    "content_b64": base64.b64encode(pdf).decode("ascii"),
                    "version": version,
                    "description": "Settlement window circular.",
                }
            ],
            "notify_list": [],
        },
    )


class MockNpciAgent(BaseAgent):
    """Deterministic mock NPCI agent: logs inbound, auto-replies to queries."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parts = list(ctx.user_content.parts) if ctx.user_content else []
        try:
            envelope = genai_parts_to_envelope(parts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mock NPCI could not parse inbound message: %s", exc)
            yield self._event(ctx, {"status": "error", "detail": str(exc)})
            return

        logger.info(
            "Mock NPCI received task_type=%s change_id=%s from=%s",
            envelope.task_type, envelope.change_id, envelope.from_,
        )

        if envelope.task_type == "query" and AUTO_REPLY_QUERY:
            # Reply out-of-band so this request returns promptly.
            asyncio.create_task(self._auto_reply_clarification(envelope))
            yield self._event(ctx, {"status": "received", "task_type": "query", "will_reply": True})
            return

        yield self._event(
            ctx,
            {"status": "received", "task_type": envelope.task_type, "change_id": envelope.change_id},
        )

    async def _auto_reply_clarification(self, query_env) -> None:
        payload = query_env.payload or {}
        response_env = _envelope(
            "clarification_response",
            query_env.change_id,
            {
                "change_id": query_env.change_id,
                "query_id": payload.get("query_id"),
                "ticket_id": payload.get("ticket_id"),
                "response": (
                    "Mock NPCI clarification: the bank's interpretation is confirmed; "
                    "proceed per the settlement circular."
                ),
                "interpretation_confirmed": True,
                "amends_change": False,
                "generation_mode": "manual",
            },
            correlation_id=query_env.correlation_id,
        )
        try:
            await send_a2a_envelope(BANK_CARD_URL, response_env)
            logger.info("Mock NPCI sent clarification_response for change_id=%s", query_env.change_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mock NPCI clarification_response send failed: %s", exc)

    def _event(self, ctx: InvocationContext, result: dict) -> Event:
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=result_to_genai_content(result),
        )


def build_mock_agent() -> MockNpciAgent:
    return MockNpciAgent(
        name="mock_npci_agent",
        description=(
            "Mock NPCI Google-A2A counterpart for local end-to-end testing of the "
            "bank's change lifecycle: receives Bank→NPCI messages and auto-replies "
            "to queries with a clarification_response."
        ),
    )


mock_agent = build_mock_agent()
a2a_app = to_a2a(mock_agent, port=int(os.getenv("MOCK_NPCI_PORT", "8001")))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(a2a_app, host="0.0.0.0", port=int(os.getenv("MOCK_NPCI_PORT", "8001")))
