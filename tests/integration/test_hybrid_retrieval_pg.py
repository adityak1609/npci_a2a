"""Integration tests for hybrid retrieval against a real pgvector Postgres.

Exercises the pieces the unit tests cannot: the generated text_search
tsvector column, the GIN-backed BM25 candidate query, dense cosine search,
RRF fusion end-to-end, source-bias ordering, and parent deduplication.

The Gemini query embedder is stubbed — no external calls.
Requires Docker (testcontainers), like the other tests in this package.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

from database import Base, DocumentChunk
from rag import retriever
from rag.embedding_config import embedding_dim

EMBEDDING_DIM = embedding_dim()


@pytest.fixture(scope="module")
def db_setup():
    with PostgresContainer("ankane/pgvector:latest") as postgres:
        engine = create_engine(postgres.get_connection_url())
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(engine)
        # Same DDL as Alembic 20260710_0012 / init_db.
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE document_chunks "
                "ADD COLUMN IF NOT EXISTS text_search tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_text_search "
                "ON document_chunks USING gin (text_search)"
            ))
        TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        yield engine, TestSession
        Base.metadata.drop_all(engine)


def _vec(seed: float) -> list[float]:
    """Unit-ish vector distinguishable by cosine distance."""
    v = [seed] + [0.01] * (EMBEDDING_DIM - 1)
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


QUERY_VEC = _vec(1.0)


@pytest.fixture()
def seeded_retriever(db_setup, monkeypatch):
    engine, TestSession = db_setup
    session = TestSession()
    session.query(DocumentChunk).delete()
    session.add_all([
        # Semantically close to the query vector; generic wording.
        DocumentChunk(
            source_file="new_circular.pdf", chunk_index=0,
            text="Transactions must settle promptly under the revised process.",
            parent_key="p0",
            parent_text="PARENT-A: Transactions must settle promptly under the revised process. Additional clauses follow.",
            embedding=_vec(0.98), source="npci_official",
        ),
        # Dense-far but contains the exact rare token the query asks about.
        DocumentChunk(
            source_file="new_circular.pdf", chunk_index=1,
            text="Response code RC91 indicates issuer timeout during settlement.",
            parent_key="p1",
            parent_text="PARENT-B: Response code RC91 indicates issuer timeout during settlement. Escalate after two retries.",
            embedding=_vec(-0.5), source="npci_official",
        ),
        # Same parent as chunk 0 — must dedup to one row.
        DocumentChunk(
            source_file="new_circular.pdf", chunk_index=2,
            text="The revised process applies to all member banks immediately.",
            parent_key="p0",
            parent_text="PARENT-A: Transactions must settle promptly under the revised process. Additional clauses follow.",
            embedding=_vec(0.95), source="npci_official",
        ),
        # Superseded policy, dense-identical to the best chunk.
        DocumentChunk(
            source_file="old_policy.pdf", chunk_index=0,
            text="Transactions must settle promptly under the former process.",
            parent_key="p0",
            parent_text="PARENT-OLD: Transactions must settle promptly under the former process.",
            embedding=_vec(0.98), source="old_policy",
        ),
        # Provisional chunk — must never be retrieved.
        DocumentChunk(
            source_file="provisional.pdf", chunk_index=0,
            text="Provisional interpretation of settlement timeout RC91 rules.",
            parent_key="p0",
            parent_text="PARENT-PROV",
            embedding=_vec(0.99), source="npci_agent", provisional=True,
        ),
    ])
    session.commit()
    session.close()

    monkeypatch.setattr(retriever, "SessionLocal", TestSession)
    monkeypatch.setattr(retriever, "_embed_query", lambda q: QUERY_VEC)
    monkeypatch.setenv("A2A_RERANK_ENABLED", "false")  # deterministic: RRF order
    return retriever


def test_bm25_rescues_exact_token_match(seeded_retriever):
    """A dense-far chunk holding the exact rare token must reach the results."""
    rows = seeded_retriever.retrieve_with_scores("what does response code RC91 mean", top_k=5)
    texts = " ".join(r["text"] for r in rows)
    assert "RC91" in texts, "BM25 leg failed to surface the exact-token chunk"


def test_parent_dedup_returns_parent_text_once(seeded_retriever):
    rows = seeded_retriever.retrieve_with_scores("revised settlement process", top_k=5)
    parent_a = [r for r in rows if r["text"].startswith("PARENT-A")]
    assert len(parent_a) == 1, "children of the same parent must collapse to one row"


def test_provisional_chunks_are_excluded(seeded_retriever):
    rows = seeded_retriever.retrieve_with_scores("settlement timeout rules", top_k=5)
    assert all("PARENT-PROV" not in r["text"] for r in rows)


def test_source_bias_prefers_current_over_superseded(seeded_retriever):
    """Dense-identical chunks: npci_official must outrank old_policy."""
    rows = seeded_retriever.retrieve_with_scores("transactions settle promptly", top_k=5)
    positions = {r["text"].split(":")[0]: i for i, r in enumerate(rows)}
    assert positions.get("PARENT-A", 99) < positions.get("PARENT-OLD", 99)


def test_distance_is_true_dense_distance(seeded_retriever):
    rows = seeded_retriever.retrieve_with_scores("revised settlement process", top_k=5)
    for row in rows:
        assert 0.0 <= row["distance"] <= 2.0
    # The dense-closest chunk (0.98 seed vs query 1.0) should be near-zero.
    assert min(r["distance"] for r in rows) < 0.1
