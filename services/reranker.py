"""Optional cross-encoder reranker for hybrid retrieval.

Scores (query, chunk) pairs with a local CPU cross-encoder so the top fused
candidates are re-ordered by actual semantic relevance instead of raw
dense/BM25 rank. Runs fully in-process — no data leaves the machine.

The dependency is optional: if ``sentence-transformers`` is not installed
(or the model cannot be loaded), retrieval degrades gracefully to RRF-only
ordering. The unavailability is logged once and surfaced as a Prometheus
counter so the fallback rate is observable.

Env:
    A2A_RERANK_ENABLED  — "true" (default) / "false"
    A2A_RERANK_MODEL    — default "BAAI/bge-reranker-v2-m3"
"""

from __future__ import annotations

import logging
import math
import os
import threading

from core.metrics import inc_counter

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None
_model_failed = False


def rerank_enabled() -> bool:
    return os.getenv("A2A_RERANK_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def _get_model():
    """Lazily load the cross-encoder once per process; None if unavailable."""
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _lock:
        if _model is not None or _model_failed:
            return _model
        model_name = os.getenv("A2A_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        try:
            from sentence_transformers import CrossEncoder

            _model = CrossEncoder(model_name, max_length=512)
            logger.info("Loaded cross-encoder %r.", model_name)
        except Exception as exc:
            _model_failed = True
            inc_counter("rag_rerank_unavailable_total")
            logger.warning(
                f"Cross-encoder unavailable ({exc.__class__.__name__}: "
                f"{str(exc)[:120]}). Falling back to RRF-only ordering. "
                "Install extras from requirements-rag.txt to enable reranking."
            )
    return _model


def rerank_scores(query: str, texts: list[str]) -> list[float] | None:
    """Return one relevance score per text in [0, 1], or None if unavailable.

    Raw cross-encoder logits are squashed through a sigmoid so downstream
    source-bias arithmetic operates on a stable, bounded scale.
    """
    if not texts or not rerank_enabled():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        logits = model.predict([(query, text) for text in texts])
    except Exception as exc:
        inc_counter("rag_rerank_failed_total")
        logger.warning("Scoring failed, using RRF order: %s", str(exc)[:120])
        return None
    inc_counter("rag_rerank_used_total")
    return [1.0 / (1.0 + math.exp(-float(score))) for score in logits]
