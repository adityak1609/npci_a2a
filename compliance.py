"""
compliance.py — Hardened AI compliance gates.

Provides per-call, data-classification-aware enforcement of the
EXTERNAL_AI_DATA_PROCESSING_APPROVED policy gate.

Any code path that sends data to an external AI provider (Gemini) MUST
call the appropriate check below. Failing closed is the default behavior.
"""
from __future__ import annotations

import logging
from enum import Enum

from config import env_bool

logger = logging.getLogger(__name__)


class DataClassification(str, Enum):
    """Classification level of the data being sent to external AI."""
    PUBLIC = "public"                # Publicly available policy text. Lower risk.
    INTERNAL = "internal"            # Internal policy documents. Requires approval.
    NPCI_PII = "npci_pii"            # NPCI user/payment identifiers. Highest risk.
    REDACTED = "redacted"            # PII has been stripped by guardrails. Acceptable.


class ComplianceBlockedError(RuntimeError):
    """Raised when sensitive data would be sent to an unapproved external AI API."""


def external_ai_processing_approved() -> bool:
    """Return whether policy/NPCI data may be sent to external AI providers.

    This flag must only be set to `true` after:
    1. Written legal approval from the NPCI data governance team.
    2. Review of the ai_compliance_docs/AI_COMPLIANCE_AGREEMENT.md.
    3. Verification that Google Gemini's data processing terms cover this use case.
    4. Confirmation of geographic data residency requirements.
    """
    return env_bool("EXTERNAL_AI_DATA_PROCESSING_APPROVED", default=False)


def require_external_ai_approval(
    purpose: str,
    *,
    classification: DataClassification = DataClassification.INTERNAL,
) -> None:
    """Fail closed until compliance approves external AI processing.

    Args:
        purpose: Human-readable description of WHY the data is being sent.
        classification: The data sensitivity level. NPCI_PII always fails closed
                        regardless of the approval flag.
    """
    # NPCI PII is NEVER sent to external AI, even if the flag is set.
    if classification == DataClassification.NPCI_PII:
        raise ComplianceBlockedError(
            f"NPCI PII data MUST NOT be sent to external AI providers. Purpose: {purpose}. "
            "Redact all PII before calling external AI. See ai_compliance_docs/AI_COMPLIANCE_AGREEMENT.md."
        )

    if external_ai_processing_approved():
        # Log every external AI call for audit traceability
        logger.info(
            "external_ai_call_approved",
            extra={
                "purpose": purpose,
                "classification": classification.value,
            }
        )
        return

    raise ComplianceBlockedError(
        "External AI processing of policy/NPCI data is disabled. "
        "Set EXTERNAL_AI_DATA_PROCESSING_APPROVED=true only after NPCI data "
        f"handling, residency, and vendor terms are approved. "
        f"Purpose: {purpose}. Classification: {classification.value}. "
        "See ai_compliance_docs/AI_COMPLIANCE_AGREEMENT.md for prerequisites."
    )


def require_chat_compliance(user_message: str) -> None:
    """Enforce compliance gate for chat/RAG pipeline calls."""
    # Public policy lookups are lower risk but still require the global gate.
    require_external_ai_approval("rag_chat", classification=DataClassification.INTERNAL)


def require_embedding_compliance(text: str) -> None:
    """Enforce compliance gate before generating embeddings via Gemini."""
    require_external_ai_approval("embedding_generation", classification=DataClassification.INTERNAL)


def require_summarization_compliance() -> None:
    """Enforce compliance gate before summarizing documents via Gemini."""
    require_external_ai_approval("document_summarization", classification=DataClassification.INTERNAL)
