"""rag/decompose.py — LLM query decomposition for multi-part questions.

A single blended embedding matches neither half of a compound question
("what is the outward FPS timeout AND which response code applies on
expiry?") particularly well. This module splits such questions into 2–4
standalone sub-queries, each retrieved independently through the existing
hybrid pipeline and RRF-fused back into one parent list
(rag.retriever.retrieve_multi_with_scores).

Cost control: a cheap heuristic (looks_multi_part) gates the LLM call so
simple questions — the common case — skip decomposition entirely and pay
zero extra latency. Every failure mode (model error, unparseable output,
compliance block) degrades to single-query retrieval, never to an error.
"""

import json
import logging
import os
import re

from core.metrics import inc_counter
from rag.retriever import _get_client, retrieve_multi_with_scores

logger = logging.getLogger(__name__)

DECOMPOSE_MODEL = os.getenv("A2A_DECOMPOSE_MODEL", "gemini-2.5-flash")
MAX_SUBQUERIES = 4
MAX_SUBQUERY_CHARS = 400

# Conjunction followed by an interrogative, an explicit comparison, or a
# second question mark — deliberately narrow so plain "terms and conditions"
# style phrasing never triggers an LLM call.
_MULTIPART = re.compile(
    r"(?:\band\s+(?:what|how|when|which|who|why|where|is|are|do|does|can|should)\b"
    r"|;"
    r"|\balso\b"
    r"|\bas well as\b"
    r"|\bdifference between\b"
    r"|\bcompare\b"
    r"|\bversus\b"
    r"|\bvs\.?\b)",
    re.IGNORECASE,
)

_DECOMPOSE_PROMPT = """\
You split a user question into standalone search queries for a policy
document retrieval system.

Rules:
- If the question asks a single thing, return it unchanged as a one-element array.
- If it asks multiple distinct things, return 2-{max_n} focused sub-queries.
- Each sub-query must be fully standalone: no pronouns, no references to
  the other sub-queries, no "it"/"that"/"the above".
- Preserve exact identifiers verbatim (response codes, RRN/UTR, circular
  numbers, field names).
- Return ONLY a JSON array of strings. No prose, no markdown.

Question: {question}"""


def _enabled() -> bool:
    return os.getenv("A2A_QUERY_DECOMPOSITION", "true").strip().lower() in {"1", "true", "yes"}


def looks_multi_part(question: str) -> bool:
    """Cheap pre-filter: only plausibly compound questions reach the LLM."""
    return question.count("?") >= 2 or bool(_MULTIPART.search(question))


def _parse_subqueries(raw: str, max_n: int = MAX_SUBQUERIES) -> list[str]:
    """Defensively parse the model's JSON array; [] whenever it is unusable."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    subqueries: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        item = item.strip()[:MAX_SUBQUERY_CHARS]
        if item and item not in subqueries:
            subqueries.append(item)
        if len(subqueries) >= max_n:
            break
    return subqueries


def decompose_query(question: str) -> list[str]:
    """Split a compound question into standalone sub-queries.

    Always returns at least [question] — decomposition is an optimization,
    never a failure point for the chat pipeline.
    """
    question = (question or "").strip()
    if not question:
        return [question]

    if not _enabled():
        inc_counter("rag_query_decomposition_total", {"result": "disabled"})
        return [question]
    if not looks_multi_part(question):
        inc_counter("rag_query_decomposition_total", {"result": "single"})
        return [question]

    try:
        # Compliance gate + Gemini call. A blocked or failed call must not
        # break chat: retrieval falls back to the original question (which
        # re-raises any genuine compliance error at embedding time).
        from security.compliance import require_external_ai_approval

        require_external_ai_approval("query decomposition for retrieval")
        client = _get_client()
        response = client.models.generate_content(
            model=DECOMPOSE_MODEL,
            contents=_DECOMPOSE_PROMPT.format(max_n=MAX_SUBQUERIES, question=question),
        )
        subqueries = _parse_subqueries(response.text or "")
    except Exception as exc:
        inc_counter("rag_query_decomposition_total", {"result": "error"})
        logger.warning("Query decomposition failed, using original question: %s", str(exc)[:120])
        return [question]

    if len(subqueries) <= 1:
        inc_counter("rag_query_decomposition_total", {"result": "single"})
        return [question]

    inc_counter("rag_query_decomposition_total", {"result": "split"})
    logger.info("Decomposed question into %s sub-queries.", len(subqueries))
    return subqueries


def decomposed_retrieve_with_scores(
    query: str,
    top_k: int = 5,
    visibility_tiers: list[str] | None = None,
    change_id: str | None = None,
) -> list[dict]:
    """Decompose the question, retrieve each sub-query, RRF-fuse the results.

    Drop-in replacement for rag.retriever.retrieve_with_scores — identical
    row shape (``text``, ``distance``, ``change_id``, ``citations_json``,
    ``score``), so confidence gating downstream keeps its semantics.
    """
    return retrieve_multi_with_scores(
        decompose_query(query), top_k, visibility_tiers, change_id
    )
