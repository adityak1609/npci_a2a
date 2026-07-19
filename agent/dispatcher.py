"""JSON-RPC 2.0 dispatcher for Mindgate A2A internal admin methods.

This dispatcher serves /a2a/internal only.  /a2a/external uses
agent/external_dispatcher.py which routes on the A2A envelope's
task_type field.
"""

import asyncio
import os
from typing import Optional

from google import genai

from agent import methods
from agent.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    rpc_error,
)
from database import DocumentSummary
from rag.prompt import build_prompt
from rag.retriever import retrieve_with_scores
from security.guardrails import _check_regex, check_input, check_output, sanitize_question
from services.npci_client import handle_escalate

# ---------------------------------------------------------------------------
# Cached Gemini client (avoid re-creating on every A2A RAG call)
# ---------------------------------------------------------------------------
_cached_genai_client: Optional[genai.Client] = None


def _get_genai_client() -> genai.Client:
    """Return a cached Gemini client for the A2A dispatcher."""
    global _cached_genai_client
    if _cached_genai_client is None:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise OSError("GOOGLE_API_KEY not set.")
        _cached_genai_client = genai.Client(api_key=api_key)
    return _cached_genai_client


import logging as _logging

_disp_logger = _logging.getLogger(__name__)


def _confidence(distance: float) -> float:
    """Return a confidence score derived from a pgvector cosine distance.

    Formula: ``confidence = clamp(1.0 - distance / scale, 0.0, 1.0)``

    where ``scale`` (default 1.5) is an empirical tuning parameter.  A
    distance of 0 → confidence 1.0 (perfect match); a distance of ``scale``
    or above → confidence 0.0 (no signal).

    Calibration protocol
    --------------------
    1. Enable ``A2A_LOG_CONFIDENCE_SAMPLES=true`` to write structured
       ``confidence_sample`` log lines for every A2A RAG call.
    2. Collect at least 200 labelled (query, expected_escalation) pairs.
    3. Compute the precision/recall curve for ``suggest_escalation`` across
       threshold values [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60].
    4. Pick the threshold that maximises precision while keeping recall ≥ 0.90.
    5. Set ``A2A_CONFIDENCE_THRESHOLD`` and, if needed, ``A2A_EMBEDDING_SCALE``
       in the environment.  See docs/confidence_calibration.md for details.
    """
    scale = float(os.getenv("A2A_EMBEDDING_SCALE", "1.5"))
    return max(0.0, min(1.0, 1.0 - (distance / scale)))


def _log_confidence_sample(
    distance: float,
    confidence: float,
    threshold: float,
    suggest_escalation: bool,
) -> None:
    """Write a structured log line for offline confidence calibration.

    Controlled by ``A2A_LOG_CONFIDENCE_SAMPLES=true``.  Does NOT log the
    raw query text to avoid PII in logs.
    """
    if os.getenv("A2A_LOG_CONFIDENCE_SAMPLES", "false").strip().lower() in {"1", "true", "yes", "on"}:
        _disp_logger.info(
            "confidence_sample",
            extra={
                "event": "confidence_sample",
                "distance": round(distance, 6),
                "confidence": round(confidence, 4),
                "threshold": threshold,
                "suggest_escalation": suggest_escalation,
            },
        )


def _format_dt(dt) -> str:
    """Return an ISO timestamp string for JSON responses."""
    return dt.isoformat() if dt else ""


def _valid_id(request_id) -> bool:
    """JSON-RPC 2.0 §4: id MUST be String, Number, or Null."""
    return request_id is None or isinstance(request_id, (str, int, float))


def _generate_content_once(contents: list) -> str:
    client = _get_genai_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )
    return response.text or ""


async def _get_summary(params: dict, db) -> dict:
    """agent.get_summary; params: document_id or filename; returns stored document summary."""
    document_id = params.get("document_id")
    filename = params.get("filename")
    if not document_id and not filename:
        raise ValueError("document_id or filename is required.")
    query = db.query(DocumentSummary)
    if document_id:
        row = query.filter(DocumentSummary.document_id == document_id).first()
    else:
        row = (
            query
            .filter(DocumentSummary.filename == filename)
            .order_by(DocumentSummary.created_at.desc())
            .first()
        )
    if not row:
        raise ValueError("summary not found.")
    return {
        "document_id": row.document_id,
        "filename": row.filename,
        "summary": row.summary,
        "source": row.source,
        "created_at": _format_dt(row.created_at),
    }


# Phrases the grounded prompt (prompt.py) instructs the model to emit when the
# context cannot answer the question. Matched case-insensitively.
_NON_ANSWER_MARKERS = (
    "couldn't find a clear answer",
    "could not find a clear answer",
    "does not contain enough information",
    "doesn't contain enough information",
    "check with your compliance team",
    "check with hr for the authoritative interpretation",
    "sections appear to have different provisions",
)


def _is_non_answer(answer: str | None) -> bool:
    """True when the generated answer is a grounded refusal/ambiguity notice."""
    if not answer:
        return True
    lowered = answer.lower()
    return any(marker in lowered for marker in _NON_ANSWER_MARKERS)


async def _query_rag(params: dict) -> dict:
    """agent.query_rag; params: query/change_id; returns non-streaming RAG result.

    When ``change_id`` is provided, retrieval is scoped to that change's
    documents only (the portal Assistant tab). When suggest_escalation is True,
    also returns ``chunk_change_id`` from the best-matching chunk (if any).  The
    frontend uses this to determine whether to show "Escalate to NPCI"
    (change_id exists) or "Route to internal review" (no associated change).
    """
    query = (params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required.")

    change_id = (params.get("change_id") or "").strip() or None

    regex_guard = _check_regex(query)
    if regex_guard.blocked:
        return {
            "answer": regex_guard.message,
            "confidence": 1.0,
            "suggest_escalation": False,
            "chunk_change_id": None,
            "message": regex_guard.message,
        }

    guard = await asyncio.to_thread(check_input, query)
    if guard.blocked:
        return {
            "answer": guard.message,
            "confidence": 1.0,
            "suggest_escalation": False,
            "chunk_change_id": None,
            "message": guard.message,
        }

    clean_query = sanitize_question(query)
    rows = retrieve_with_scores(clean_query, 5, change_id=change_id)
    if not rows:
        return {
            "answer": None,
            "confidence": 0.0,
            "suggest_escalation": True,
            "chunk_change_id": None,
            "message": "No confident answer found.",
        }

    # Hybrid retrieval orders rows by fused relevance, not raw distance —
    # confidence gating should still reflect the closest dense match found.
    best_distance = min(row["distance"] for row in rows)
    confidence = _confidence(best_distance)
    threshold = float(os.getenv("A2A_CONFIDENCE_THRESHOLD", "0.45"))
    best_change_id = rows[0].get("change_id") if rows else None

    if confidence < threshold:
        _log_confidence_sample(best_distance, confidence, threshold, suggest_escalation=True)
        return {
            "answer": None,
            "confidence": round(confidence, 3),
            "suggest_escalation": True,
            "chunk_change_id": best_change_id,
            "message": "No confident answer found.",
        }

    _log_confidence_sample(best_distance, confidence, threshold, suggest_escalation=False)

    client = _get_genai_client()
    prompt = build_prompt([row["text"] for row in rows])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            {"role": "user", "parts": [{"text": prompt}]},
            {"role": "model", "parts": [{"text": "Understood."}]},
            {"role": "user", "parts": [{"text": clean_query}]},
        ],
    )
    answer = check_output(response.text or "", [row["text"] for row in rows])

    # Retrieval confidence alone can pass the threshold while the LLM still
    # cannot answer from the context (grounding rule §3 in prompt.py makes it
    # say so explicitly). Detect those non-answers so the frontend can offer
    # "Raise query to NPCI" instead of presenting a dead end.
    if _is_non_answer(answer):
        _log_confidence_sample(best_distance, confidence, threshold, suggest_escalation=True)
        return {
            "answer": answer,
            "confidence": round(confidence, 3),
            "suggest_escalation": True,
            "chunk_change_id": best_change_id,
            "message": answer,
        }

    # Collect citations from matched chunks
    citations = []
    for row in rows:
        if row.get("citations_json"):
            try:
                import json
                cites = json.loads(row["citations_json"])
                if isinstance(cites, list):
                    citations.extend(cites)
            except (json.JSONDecodeError, TypeError):
                pass

    return {
        "answer": answer,
        "confidence": round(confidence, 3),
        "suggest_escalation": False,
        "chunk_change_id": best_change_id,
        "citations": citations if citations else None,
    }


async def _dispatch_single(payload: dict, db) -> dict | None:
    """Dispatch a single JSON-RPC 2.0 request.

    Returns a JSON-RPC response dict, or None for notifications
    (requests without an ``id`` field per §4.1).
    """
    # §4: id MUST be String, Number, or Null when present.
    request_id = payload.get("id") if isinstance(payload, dict) else None
    is_notification = isinstance(payload, dict) and "id" not in payload

    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        # Notifications with invalid structure still get an error
        # because we cannot determine intent without a valid envelope.
        return rpc_error(request_id, INVALID_REQUEST, "Invalid Request")

    if not _valid_id(payload.get("id")):
        return rpc_error(None, INVALID_REQUEST, "Invalid Request: id must be String, Number, or Null")

    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        if is_notification:
            return None  # §4.1: no response for malformed notifications
        return rpc_error(request_id, INVALID_REQUEST, "Invalid Request")

    try:
        if method == methods.GET_SUMMARY:
            result = await _get_summary(params, db)
        elif method == methods.QUERY_RAG:
            result = await _query_rag(params)
        elif method == methods.ESCALATE_TO_NPCI:
            result = await handle_escalate(params)
        elif method == methods.SUBMIT_COUNTER_PROPOSAL:
            from services.npci_outbound import submit_counter_proposal
            result = await submit_counter_proposal(params)
        elif method == methods.REPORT_MILESTONE:
            from services.npci_outbound import report_milestone
            result = await report_milestone(params)
        elif method == methods.DECLARE_READINESS:
            from services.npci_outbound import declare_readiness
            result = await declare_readiness(params)
        elif method == methods.RAISE_BLOCKER:
            from services.npci_outbound import raise_blocker
            result = await raise_blocker(params)
        else:
            if is_notification:
                return None
            return rpc_error(request_id, METHOD_NOT_FOUND, "Method not found")
    except ValueError as exc:
        if is_notification:
            return None
        return rpc_error(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:
        if is_notification:
            return None
        return rpc_error(request_id, INTERNAL_ERROR, "Internal error")

    # §4.1: Notifications MUST NOT receive a response.
    if is_notification:
        return None

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def dispatch_rpc(payload, db) -> dict | list | None:
    """Dispatch a JSON-RPC 2.0 request or batch of requests.

    Args:
        payload: A single request dict OR a list of request dicts (batch).
        db: SQLAlchemy session.

    Returns:
        - A single response dict for individual requests.
        - A list of response dicts for batch requests (empty batches
          get an INVALID_REQUEST error).
        - None if every request in a batch was a notification.
    """
    # §6: Batch — if payload is a list, dispatch each independently.
    if isinstance(payload, list):
        if not payload:
            return rpc_error(None, INVALID_REQUEST, "Invalid Request: empty batch")

        responses = []
        for item in payload:
            result = await _dispatch_single(item, db)
            if result is not None:  # Skip notification (None) responses
                responses.append(result)

        # §6: If all requests were notifications, return nothing.
        return responses if responses else None

    # Single request
    return await _dispatch_single(payload, db)
