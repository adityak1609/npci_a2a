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

Transport-level auth (mTLS/CIDR/HMAC/JWT — layers 1-4) is enforced at the ASGI
boundary by ``agent/adk/transport_auth.py`` when ``A2A_ADK_TRANSPORT_AUTH`` is
on (production); the spec stacks the NPCI HMAC envelope + JWT bearer on top of
the Google A2A SDK transport, same as the legacy endpoint.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from a2a_auth import (
    A2AAuthContext,
    A2AAuthError,
    check_agent_identity,
    check_ownership,
    check_partner_binding_sub,
    check_partner_status,
    check_state_transition,
)
from agent.adk.message_mapping import genai_parts_to_envelope, result_to_genai_content
from agent.adk.transport_auth import adk_principal
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
            # Identity established at the transport boundary (transport_auth.py:
            # spec §4.1 layers 1-4). None when A2A_ADK_TRANSPORT_AUTH is off (the
            # trusted local-mock default), in which case the sub-based binding is
            # a no-op.
            principal = adk_principal.get()
            jwt_sub = principal.jwt_sub if principal else None
            caller_ip = principal.caller_ip if principal else "a2a-adk"

            # Message-level authz (spec §4.1 layers 5-8). Layers 1-4 (mTLS, CIDR,
            # HMAC envelope, JWT) are enforced at the transport boundary by
            # agent/adk/transport_auth.py — the NPCI protocol layers ride on top
            # of the Google A2A SDK transport, per the spec's stack.
            try:
                check_partner_status(envelope.from_, db)        # L5a
                check_partner_binding_sub(envelope, jwt_sub)     # L5b
                check_ownership(envelope, envelope.from_, db)    # L6
                check_state_transition(envelope, db)             # L7
                check_agent_identity(envelope)                   # L8
            except A2AAuthError as exc:
                self._audit_rejection(db, caller_ip, exc)
                return {"error": exc.error_code, "detail": exc.detail}

            try:
                ctx = A2AAuthContext(
                    caller_ip=caller_ip,
                    jwt_sub=jwt_sub,
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

    @staticmethod
    def _audit_rejection(db, caller_ip: str, exc: A2AAuthError) -> None:
        """Log an L5-L8 authz rejection to a2a_messages (parity with the legacy
        endpoint). Auditing must never mask the real rejection."""
        try:
            from a2a_audit import log_a2a_rejection
            from metrics import inc_counter

            inc_counter("a2a_auth_rejections_total", {"error_code": exc.error_code})
            log_a2a_rejection(
                db,
                raw_body=b"",
                caller_ip=caller_ip,
                error_code=exc.error_code,
                detail=exc.detail,
                http_status=exc.http_status,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to audit ADK message-authz rejection")

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
