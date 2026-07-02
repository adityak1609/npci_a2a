import math

import pytest

from embedding_config import (
    DEFAULT_EMBEDDING_DIM,
    MAX_GEMINI_EMBEDDING_DIM,
    embedding_dim,
    normalize_embedding,
)


def test_embedding_dim_defaults_to_database_dimension(monkeypatch):
    monkeypatch.delenv("A2A_EMBEDDING_DIM", raising=False)

    assert embedding_dim() == DEFAULT_EMBEDDING_DIM


def test_embedding_dim_uses_env_override(monkeypatch):
    monkeypatch.setenv("A2A_EMBEDDING_DIM", "3072")

    assert embedding_dim() == 3072


def test_embedding_dim_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("A2A_EMBEDDING_DIM", str(MAX_GEMINI_EMBEDDING_DIM + 1))

    with pytest.raises(ValueError, match="A2A_EMBEDDING_DIM"):
        embedding_dim()


def test_normalize_embedding_returns_unit_vector():
    normalized = normalize_embedding([3.0, 4.0])

    assert normalized == [0.6, 0.8]
    assert math.isclose(math.sqrt(sum(value * value for value in normalized)), 1.0)
