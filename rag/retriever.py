"""
rag/retriever.py — hybrid chunk retrieval for Mindgate Policy Bot.

Pipeline (per query):
    1. Dense search    — gemini-embedding-001 query embedding, pgvector cosine
                         distance over child chunks (top A2A_DENSE_TOP_N).
    2. BM25 search     — Postgres full-text (tsvector/ts_rank_cd) lexical
                         matching over the same chunks (top A2A_BM25_TOP_N).
                         Catches exact tokens dense search fumbles: response
                         codes, RRN/UTR, circular numbers, field names.
    3. RRF merge       — Reciprocal Rank Fusion (k=A2A_RRF_K, default 60)
                         combines both rank lists; top A2A_RRF_TOP_N survive.
    4. Rerank          — optional local cross-encoder (services/reranker.py)
                         re-scores the fused candidates; top A2A_RERANK_TOP_N.
    5. Source bias     — the bank-grade authority weighting (npci_official
                         boosted, old_policy penalized) is applied to the
                         FINAL relevance score, after fusion/rerank, so an
                         eloquent but superseded circular cannot outrank the
                         authoritative one.
    6. Parent dedup    — children grouped by parent; the best child wins and
                         the LLM receives the full parent passage.

Every returned row carries the true dense cosine ``distance`` of its best
child (computed even for BM25-discovered candidates), so downstream
confidence thresholds (A2A_CONFIDENCE_THRESHOLD) keep their semantics.

BM25 degrades gracefully to dense-only if the tsvector column is missing
(pre-migration database) — the failure is counted, not fatal.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from core.config import env_int
from core.metrics import inc_counter
from database import DocumentChunk, SessionLocal
from rag.embedding_config import embedding_dim, normalize_embedding
from security.compliance import require_external_ai_approval
from services.reranker import rerank_scores

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = embedding_dim()
TOP_K = 5  # Default number of final results to return


# ---------------------------------------------------------------------------
# Gemini client cache
# ---------------------------------------------------------------------------
_cached_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    """Return a cached Gemini client (created once per process)."""
    global _cached_client
    if _cached_client is None:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise OSError("GOOGLE_API_KEY not set. Add it to your .env file.")
        _cached_client = genai.Client(api_key=api_key)
    return _cached_client


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------
def _embed_query(query: str) -> list[float]:
    """
    Embed a single query string using gemini-embedding-001.

    Args:
        query: The user's natural-language question.

    Returns:
        List of floats (embedding vector).
    """
    require_external_ai_approval("query embedding for policy retrieval")
    client = _get_client()
    retryable = ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL"]

    for attempt in range(3):
        try:
            result = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=query,
                config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
            )
            return normalize_embedding(result.embeddings[0].values)
        except Exception as e:
            err = str(e)
            if any(code in err for code in retryable) and attempt < 2:
                wait = min(attempt + 1, 2)  # 1s, 2s — fast retries
                logger.warning("Query embedding: transient error, retrying in %ss...", wait)
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def index_exists() -> bool:
    """
    Check whether the document_chunks table contains at least one row.
    Used by the /chat endpoint to guard against missing ingestion.
    """
    try:
        db = SessionLocal()
        try:
            count = db.query(DocumentChunk).count()
            return count > 0
        finally:
            db.close()
    except Exception:
        return False


def invalidate_cache() -> None:
    """
    No-op for pgvector (no in-memory index to invalidate).
    Kept for API compatibility with the old FAISS retriever.
    """
    pass


# ---------------------------------------------------------------------------
# Candidate queries (dense + BM25)
# ---------------------------------------------------------------------------
_CANDIDATE_COLUMNS = (
    "id, text, parent_key, parent_text, source_file, change_id, "
    "citations_json, source, embedding <=> CAST(:vec AS vector) AS distance"
)


def _visibility_filter_sql(visibility_tiers: list[str] | None) -> str:
    if not visibility_tiers:
        return ""
    return "AND visibility_tier IN :visibility_tiers"


def _change_filter_sql(change_id: str | None) -> str:
    """Optional filter that scopes retrieval to a single change's documents."""
    if not change_id:
        return ""
    return "AND change_id = :change_id"


def _execute_visibility_query(db: Session, sql: str, params: dict, visibility_tiers: list[str] | None):
    stmt = text(sql)
    if visibility_tiers:
        stmt = stmt.bindparams(bindparam("visibility_tiers", expanding=True))
        params = dict(params)
        params["visibility_tiers"] = visibility_tiers
    return db.execute(stmt, params).fetchall()


def _dense_candidates(
    db: Session,
    vec_str: str,
    visibility_tiers: list[str] | None,
    change_id: str | None,
    limit: int,
) -> list:
    params: dict = {"vec": vec_str, "k": limit}
    if change_id:
        params["change_id"] = change_id
    return _execute_visibility_query(
        db,
        f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM document_chunks
            WHERE (provisional IS NULL OR provisional = FALSE)
              {_visibility_filter_sql(visibility_tiers)}
              {_change_filter_sql(change_id)}
            ORDER BY embedding <=> CAST(:vec AS vector)
            LIMIT :k
        """,
        params,
        visibility_tiers,
    )


def _bm25_candidates(
    db: Session,
    query: str,
    vec_str: str,
    visibility_tiers: list[str] | None,
    change_id: str | None,
    limit: int,
) -> list:
    """Lexical candidates via Postgres full-text search.

    websearch_to_tsquery is injection-safe for arbitrary user text. The dense
    cosine distance is also computed per returned row so BM25-only hits carry
    a meaningful ``distance`` for downstream confidence scoring.

    Returns [] when full-text search is unavailable (e.g. the text_search
    column has not been migrated yet) — retrieval continues dense-only.
    """
    params: dict = {"vec": vec_str, "q": query, "k": limit}
    if change_id:
        params["change_id"] = change_id
    try:
        return _execute_visibility_query(
            db,
            f"""
                SELECT {_CANDIDATE_COLUMNS}
                FROM document_chunks
                WHERE text_search @@ websearch_to_tsquery('english', :q)
                  AND (provisional IS NULL OR provisional = FALSE)
                  {_visibility_filter_sql(visibility_tiers)}
                  {_change_filter_sql(change_id)}
                ORDER BY ts_rank_cd(text_search, websearch_to_tsquery('english', :q)) DESC
                LIMIT :k
            """,
            params,
            visibility_tiers,
        )
    except Exception as exc:
        db.rollback()
        inc_counter("rag_bm25_unavailable_total")
        logger.warning("BM25 search unavailable, dense-only: %s", str(exc)[:120])
        return []


# ---------------------------------------------------------------------------
# Fusion, rerank, bias, parent dedup
# ---------------------------------------------------------------------------
def rrf_merge(ranked_lists: list[list], k: int = 60) -> list[tuple]:
    """Reciprocal Rank Fusion over candidate rows keyed by chunk id.

    RRF_score(c) = Σ_i 1 / (k + rank_i(c))  over every list containing c.
    Rank-based, so the incomparable dense-distance and ts_rank scales never
    need to be reconciled; k=60 damps the winner-takes-all effect and rewards
    consensus across both lists.

    Returns (row, rrf_score) tuples sorted by fused score, best first.
    """
    scores: dict = {}
    rows: dict = {}
    for ranked in ranked_lists:
        for rank, row in enumerate(ranked, start=1):
            chunk_id = row[0]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            rows.setdefault(chunk_id, row)
    return sorted(
        ((rows[cid], score) for cid, score in scores.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def _source_bias(source: str | None) -> float:
    """Authority weighting (positive = penalty), same env knobs as before."""
    biases = {
        "npci_official": float(os.getenv("A2A_SOURCE_BIAS_NPCI_OFFICIAL", "-0.06")),
        "npci_agent": float(os.getenv("A2A_SOURCE_BIAS_NPCI_AGENT", "-0.03")),
        "admin_upload": float(os.getenv("A2A_SOURCE_BIAS_ADMIN_UPLOAD", "0.00")),
        "old_policy": float(os.getenv("A2A_SOURCE_BIAS_OLD_POLICY", "0.04")),
    }
    default = float(os.getenv("A2A_SOURCE_BIAS_DEFAULT", "0.02"))
    return biases.get(source or "", default)


def _hybrid_retrieve(
    query: str,
    top_k: int,
    visibility_tiers: list[str] | None,
    change_id: str | None,
) -> list[dict]:
    """Run the full hybrid pipeline and return final parent-level rows."""
    if not index_exists():
        raise FileNotFoundError(
            "No document chunks found. Run ingest.py or POST /ingest first."
        )

    dense_top_n = env_int("A2A_DENSE_TOP_N", 20)
    bm25_top_n = env_int("A2A_BM25_TOP_N", 20)
    rrf_k = env_int("A2A_RRF_K", 60)
    rrf_top_n = env_int("A2A_RRF_TOP_N", 8)
    rerank_top_n = env_int("A2A_RERANK_TOP_N", 6)

    query_vector = _embed_query(query)
    vec_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    db = SessionLocal()
    try:
        dense = _dense_candidates(db, vec_str, visibility_tiers, change_id, dense_top_n)
        lexical = _bm25_candidates(db, query, vec_str, visibility_tiers, change_id, bm25_top_n)
    finally:
        db.close()

    fused = rrf_merge([list(dense), list(lexical)], k=rrf_k)[:rrf_top_n]
    if not fused:
        return []

    # Optional cross-encoder rerank on the fused candidates. Scores land in
    # [0, 1]; when unavailable, normalize RRF to the same scale (max possible
    # fused score with two lists is 2/(k+1)) so source bias stays meaningful.
    candidate_rows = [row for row, _ in fused]
    rerank = rerank_scores(query, [row[1] for row in candidate_rows])
    if rerank is not None:
        scored = sorted(
            zip(candidate_rows, rerank), key=lambda item: item[1], reverse=True
        )[:rerank_top_n]
    else:
        max_rrf = 2.0 / (rrf_k + 1)
        scored = [(row, score / max_rrf) for row, score in fused[:rerank_top_n]]

    # Final ranking: relevance minus source-authority bias, then parent dedup
    # (best child per parent wins; the LLM gets the full parent passage).
    best_per_parent: dict = {}
    for row, relevance in scored:
        final_score = relevance - _source_bias(row[7])
        parent_id = (row[4], row[2]) if row[2] else ("chunk", row[0])
        current = best_per_parent.get(parent_id)
        if current is None or final_score > current[1]:
            best_per_parent[parent_id] = (row, final_score)

    final = sorted(best_per_parent.values(), key=lambda item: item[1], reverse=True)
    return [
        {
            "text": row[3] or row[1],  # full parent passage, else child text
            "distance": float(row[8]),
            "change_id": row[5],
            "citations_json": row[6],
            "score": round(score, 4),
        }
        for row, score in final[:top_k]
    ]


def retrieve(
    query: str,
    top_k: int = TOP_K,
    visibility_tiers: list[str] | None = None,
    change_id: str | None = None,
) -> list[str]:
    """
    Retrieve the most relevant policy passages for a given query.

    Runs the hybrid pipeline (dense + BM25 → RRF → rerank → source bias →
    parent dedup) and returns the final passage texts.

    Args:
        query:  Natural-language question from the user.
        top_k:  Number of passages to return (default: 5).
        change_id: When set, restrict retrieval to that change's documents.

    Returns:
        List of text strings — the most relevant policy passages.

    Raises:
        FileNotFoundError: If no chunks have been ingested yet.
    """
    return [row["text"] for row in _hybrid_retrieve(query, top_k, visibility_tiers, change_id)]


def retrieve_with_scores(
    query: str,
    top_k: int = TOP_K,
    visibility_tiers: list[str] | None = None,
    change_id: str | None = None,
) -> list[dict]:
    """
    Retrieve passages with metadata for confidence scoring.

    Args:
        query: Natural-language question from the user.
        top_k: Number of passages to return.
        change_id: When set, restrict retrieval to that change's documents
            (change-scoped assistant).

    Returns:
        Dicts with ``text``, ``distance`` (dense cosine distance of the best
        child chunk — preserved for A2A_CONFIDENCE_THRESHOLD semantics),
        ``change_id``, ``citations_json`` and the final hybrid ``score``,
        ordered by best match first.
    """
    return _hybrid_retrieve(query, top_k, visibility_tiers, change_id)


def retrieve_multi_with_scores(
    queries: list[str],
    top_k: int = TOP_K,
    visibility_tiers: list[str] | None = None,
    change_id: str | None = None,
) -> list[dict]:
    """Hybrid retrieval over decomposed sub-queries, RRF-fused at parent level.

    Each sub-query runs the full hybrid pipeline in parallel (threads: the
    per-query work is embedding API + DB I/O). The per-query parent lists are
    then fused with Reciprocal Rank Fusion keyed on passage text, so a parent
    that ranks well for several sub-queries beats one that tops only one —
    the same consensus-over-dominance rule rrf_merge applies to dense/BM25.

    Row shape matches retrieve_with_scores; ``distance`` is the best (lowest)
    dense distance seen for that parent across all sub-queries, preserving
    A2A_CONFIDENCE_THRESHOLD semantics downstream.
    """
    cleaned = [q.strip() for q in queries if q and q.strip()]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return _hybrid_retrieve(cleaned[0], top_k, visibility_tiers, change_id)

    with ThreadPoolExecutor(max_workers=min(len(cleaned), 4)) as pool:
        per_query = list(
            pool.map(
                lambda q: _hybrid_retrieve(q, top_k, visibility_tiers, change_id),
                cleaned,
            )
        )

    rrf_k = env_int("A2A_RRF_K", 60)
    fused: dict[str, float] = {}
    best: dict[str, dict] = {}
    for ranked in per_query:
        for rank, row in enumerate(ranked, start=1):
            key = row["text"]
            fused[key] = fused.get(key, 0.0) + 1.0 / (rrf_k + rank)
            current = best.get(key)
            if current is None or row["distance"] < current["distance"]:
                best[key] = row

    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [{**best[key], "score": round(score, 4)} for key, score in ordered]
