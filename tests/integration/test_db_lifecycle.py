"""Integration tests using Testcontainers for a real PostgreSQL database with pgvector."""

import pytest
from testcontainers.postgres import PostgresContainer
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database import Base, DocumentChunk
from embedding_config import embedding_dim

# Match the ORM's configured pgvector dimension so inserts are schema-valid.
EMBEDDING_DIM = embedding_dim()


@pytest.fixture(scope="module")
def postgres_container():
    # pgvector-enabled image so CREATE EXTENSION vector succeeds.
    with PostgresContainer("ankane/pgvector:latest") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="module")
def db_engine(postgres_container):
    engine = create_engine(postgres_container)
    # Ensure pgvector extension exists (SQLAlchemy 2.0 requires text() + commit).
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def db_session(db_engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = SessionLocal()
    yield session
    session.close()


def test_document_chunk_vector_insert_and_search(db_session):
    """Test that we can insert vectors and perform cosine similarity search."""
    # Given: Two document chunks with vector embeddings at the schema dimension.
    embedding1 = [0.1] * EMBEDDING_DIM
    embedding2 = [0.9] * EMBEDDING_DIM

    chunk1 = DocumentChunk(
        source_file="test1.pdf",
        chunk_index=0,
        text="This is a test chunk related to A2A policies.",
        embedding=embedding1,
        page_number=1,
    )
    chunk2 = DocumentChunk(
        source_file="test2.pdf",
        chunk_index=0,
        text="This is unrelated data.",
        embedding=embedding2,
        page_number=2,
    )
    db_session.add_all([chunk1, chunk2])
    db_session.commit()

    # When: We search using vector cosine distance for embedding1
    results = (
        db_session.query(
            DocumentChunk,
            DocumentChunk.embedding.cosine_distance(embedding1).label("distance"),
        )
        .order_by("distance")
        .limit(1)
        .all()
    )

    # Then: It should return chunk1 as the closest match with near 0 distance
    assert len(results) == 1
    match, distance = results[0]
    assert match.source_file == "test1.pdf"
    assert distance < 0.001
