"""
retriever.py — pgvector-based chunk retrieval for Mindgate Policy Bot.

Embeds the user query using Google gemini-embedding-001, then performs
a cosine-similarity search against the document_chunks table in PostgreSQL.
"""

import os
import time
from typing import Optional
from google import genai
from google.genai import types
from dotenv import load_dotenv
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from compliance import require_external_ai_approval
from database import DocumentChunk, SessionLocal, init_db
from embedding_config import embedding_dim, normalize_embedding

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = embedding_dim()
TOP_K = 5  # Default number of chunks to retrieve

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
            raise EnvironmentError("GOOGLE_API_KEY not set. Add it to your .env file.")
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
                print(f"[retriever] Transient error, retrying in {wait}s...")
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


def _visibility_filter_sql(visibility_tiers: list[str] | None) -> str:
    if not visibility_tiers:
        return ""
    return "AND visibility_tier IN :visibility_tiers"


def _execute_visibility_query(db: Session, sql: str, params: dict, visibility_tiers: list[str] | None):
    stmt = text(sql)
    if visibility_tiers:
        stmt = stmt.bindparams(bindparam("visibility_tiers", expanding=True))
        params["visibility_tiers"] = visibility_tiers
    return db.execute(stmt, params).fetchall()


def retrieve(query: str, top_k: int = TOP_K, visibility_tiers: list[str] | None = None) -> list[str]:
    """
    Retrieve the most relevant policy chunks for a given query.

    Steps:
        1. Embed the query using gemini-embedding-001.
        2. Run a cosine similarity search against document_chunks in Postgres.
        3. Return the top-k text chunks.

    Args:
        query:  Natural-language question from the user.
        top_k:  Number of chunks to return (default: 5).

    Returns:
        List of text strings — the most relevant policy chunks.

    Raises:
        FileNotFoundError: If no chunks have been ingested yet.
    """
    if not index_exists():
        raise FileNotFoundError(
            "No document chunks found. Run ingest.py or POST /ingest first."
        )

    # Embed the query
    query_vector = _embed_query(query)

    # Format as Postgres vector literal: '[0.1, 0.2, ...]'
    vec_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    bias_npci_official = float(os.getenv("A2A_SOURCE_BIAS_NPCI_OFFICIAL", "-0.06"))
    bias_npci_agent = float(os.getenv("A2A_SOURCE_BIAS_NPCI_AGENT", "-0.03"))
    bias_admin_upload = float(os.getenv("A2A_SOURCE_BIAS_ADMIN_UPLOAD", "0.00"))
    bias_old_policy = float(os.getenv("A2A_SOURCE_BIAS_OLD_POLICY", "0.04"))
    bias_default = float(os.getenv("A2A_SOURCE_BIAS_DEFAULT", "0.02"))

    db = SessionLocal()
    try:
        # pgvector cosine distance operator: <=> 
        # Lower distance = more similar
        # Filter out provisional chunks (amends_change responses not yet authoritative)
        rows = _execute_visibility_query(
            db,
            f"""
                SELECT text
                FROM document_chunks
                WHERE (provisional IS NULL OR provisional = FALSE)
                  {_visibility_filter_sql(visibility_tiers)}
                ORDER BY
                    (embedding <=> CAST(:vec AS vector)) +
                    CASE source
                        WHEN 'npci_official' THEN :bias_npci_official
                        WHEN 'npci_agent' THEN :bias_npci_agent
                        WHEN 'admin_upload' THEN :bias_admin_upload
                        WHEN 'old_policy' THEN :bias_old_policy
                        ELSE :bias_default
                    END
                LIMIT :k
            """,
            {
                "vec": vec_str,
                "k": top_k,
                "bias_npci_official": bias_npci_official,
                "bias_npci_agent": bias_npci_agent,
                "bias_admin_upload": bias_admin_upload,
                "bias_old_policy": bias_old_policy,
                "bias_default": bias_default
            },
            visibility_tiers,
        )
    finally:
        db.close()

    return [row[0] for row in rows]


def retrieve_with_scores(
    query: str,
    top_k: int = TOP_K,
    visibility_tiers: list[str] | None = None,
) -> list[dict]:
    """
    Retrieve chunks with pgvector cosine distance for confidence scoring.

    Args:
        query: Natural-language question from the user.
        top_k: Number of chunks to return.

    Returns:
        Dicts with ``text`` and ``distance`` keys, ordered by best match first.
    """
    if not index_exists():
        raise FileNotFoundError(
            "No document chunks found. Run ingest.py or POST /ingest first."
        )

    query_vector = _embed_query(query)
    vec_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    bias_npci_official = float(os.getenv("A2A_SOURCE_BIAS_NPCI_OFFICIAL", "-0.06"))
    bias_npci_agent = float(os.getenv("A2A_SOURCE_BIAS_NPCI_AGENT", "-0.03"))
    bias_admin_upload = float(os.getenv("A2A_SOURCE_BIAS_ADMIN_UPLOAD", "0.00"))
    bias_old_policy = float(os.getenv("A2A_SOURCE_BIAS_OLD_POLICY", "0.04"))
    bias_default = float(os.getenv("A2A_SOURCE_BIAS_DEFAULT", "0.02"))

    db = SessionLocal()
    try:
        # Filter out provisional chunks and return change_id + citations
        rows = _execute_visibility_query(
            db,
            f"""
                SELECT text,
                       embedding <=> CAST(:vec AS vector) AS distance,
                       change_id,
                       citations_json
                FROM document_chunks
                WHERE (provisional IS NULL OR provisional = FALSE)
                  {_visibility_filter_sql(visibility_tiers)}
                ORDER BY
                    (embedding <=> CAST(:vec AS vector)) +
                    CASE source
                        WHEN 'npci_official' THEN :bias_npci_official
                        WHEN 'npci_agent' THEN :bias_npci_agent
                        WHEN 'admin_upload' THEN :bias_admin_upload
                        WHEN 'old_policy' THEN :bias_old_policy
                        ELSE :bias_default
                    END
                LIMIT :k
            """,
            {
                "vec": vec_str,
                "k": top_k,
                "bias_npci_official": bias_npci_official,
                "bias_npci_agent": bias_npci_agent,
                "bias_admin_upload": bias_admin_upload,
                "bias_old_policy": bias_old_policy,
                "bias_default": bias_default
            },
            visibility_tiers,
        )
    finally:
        db.close()

    return [
        {
            "text": row[0],
            "distance": float(row[1]),
            "change_id": row[2],
            "citations_json": row[3],
        }
        for row in rows
    ]
