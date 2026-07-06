"""Deterministic ADK agent that terminates the bank's Google-A2A endpoint.

This is intentionally a plain :class:`~google.adk.agents.BaseAgent` — **not** an
``LlmAgent``. Inbound NPCI messages drive a compliance-critical change lifecycle;
there is no LLM in this path. The agent:

    1. reconstructs the ``A2AEnvelope`` from the inbound message parts,
    2. re-runs the preserved business-authz layers (ownership + state-machine,
       i.e. spec §4.1 layers 6-7) via the existing ``a2a_auth`` helpers, then
    3. delegates to the **unchanged** ``agent.external_dispatcher.dispatch_external``
       so idempotency, task_type handlers, state advancement, audit, and async
       ingestion are reused verbatim.

Transport-level auth (mTLS/CIDR/HMAC — layers 1-4) belongs to the legacy custom
endpoint and is not run here; for the bundled mock that is acceptable, and the
mapping to A2A ``securitySchemes`` for the real NPCI agent is a follow-up.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from a2a_auth import A2AAuthContext, A2AAuthError, check_ownership, check_state_transition
from agent.adk.message_mapping import genai_parts_to_envelope, result_to_genai_content
from agent.external_dispatcher import (
    NotImplementedTaskError,
    PayloadValidationError,
    UnknownTaskTypeError,
    dispatch_external,
)
from database import SessionLocal

logger = logging.getLogger(__name__)


class BankA2AProtocolAgent(BaseAgent):
    """Terminates inbound Google-A2A traffic and drives the change lifecycle."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parts = list(ctx.user_content.parts) if ctx.user_content else []

        try:
            envelope = genai_parts_to_envelope(parts)
        except Exception as exc:  # malformed message → structured error, not a 500
            logger.warning("A2A message could not be parsed into an envelope: %s", exc)
            yield self._event(ctx, {"error": "invalid_message", "detail": str(exc)})
            return

        result = await self._dispatch(envelope)
        yield self._event(ctx, result)

    async def _dispatch(self, envelope) -> dict:
        """Run preserved authz, then the shared external dispatcher."""
        db = SessionLocal()
        try:
            # Preserved business-authz (spec §4.1 layers 6-7). Layers 1-5 (mTLS,
            # CIDR, HMAC, JWT, partner status) are transport-side and skipped on
            # the ADK path for the trusted local mock.
            try:
                check_ownership(envelope, envelope.from_, db)
                check_state_transition(envelope, db)
            except A2AAuthError as exc:
                return {"error": exc.error_code, "detail": exc.detail}

            try:
                ctx = A2AAuthContext(
                    caller_ip="a2a-adk",
                    jwt_sub=None,
                    partner_id=envelope.from_,
                    agent_id=envelope.agent_id,
                    agent_run_id=envelope.agent_run_id,
                    envelope=envelope,
                    raw_body=b"",
                )
                return await dispatch_external(envelope, db, ctx)
            except NotImplementedTaskError as exc:
                return {"error": "not_implemented", "detail": str(exc)}
            except UnknownTaskTypeError as exc:
                return {"error": "unknown_task_type", "detail": str(exc)}
            except PayloadValidationError as exc:
                return {"error": "payload_validation_error", "detail": str(exc)}
            except ValueError as exc:
                return {"error": "invalid_request", "detail": str(exc)}
            except Exception as exc:  # noqa: BLE001 — never leak a raw 500 over A2A
                logger.exception("A2A handler failed for %s", envelope.task_type)
                return {"error": "internal_error", "detail": "Internal error"}
        finally:
            db.close()

    def _event(self, ctx: InvocationContext, result: dict) -> Event:
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=result_to_genai_content(result),
        )


def build_root_agent() -> BankA2AProtocolAgent:
    """Construct the bank's root A2A agent (exposed via ``to_a2a``)."""
    return BankA2AProtocolAgent(
        name="mindgate_bank_a2a_agent",
        description=(
            "Bank-side NPCI A2A change-management agent. Receives change "
            "communications, queries, counter-decisions, blockers, and "
            "milestone requests from NPCI and drives the change lifecycle."
        ),
    )
