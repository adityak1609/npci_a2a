"""ADK (Google Agent Development Kit) adapter for the bank A2A agent.

This package bridges Google's A2A protocol (as spoken by ADK) to the bank's
existing deterministic change-management pipeline. It is a *transport/interop*
layer only — the envelope, idempotency, state machine, audit, and ingestion
logic in ``agent/external_dispatcher.py`` and ``services/a2a_ingestor.py`` are
reused unchanged.

See ``docs/TECHNICAL_DEEP_DIVE.md`` and the ``google-a2a-migration`` plan for
the design rationale (why a deterministic ``BaseAgent`` rather than an
``LlmAgent`` handles the protocol path).
"""
