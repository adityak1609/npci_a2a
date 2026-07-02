"""Shared embedding dimensionality configuration."""

from __future__ import annotations

import os
import math

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

DEFAULT_EMBEDDING_DIM = 768
MAX_GEMINI_EMBEDDING_DIM = 3072


def embedding_dim() -> int:
    """Return the configured pgvector/Gemini embedding dimensionality."""
    raw_value = os.getenv("A2A_EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))
    try:
        dim = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("A2A_EMBEDDING_DIM must be an integer.") from exc

    if dim <= 0 or dim > MAX_GEMINI_EMBEDDING_DIM:
        raise ValueError(
            f"A2A_EMBEDDING_DIM must be between 1 and {MAX_GEMINI_EMBEDDING_DIM}."
        )
    return dim


def normalize_embedding(values: list[float]) -> list[float]:
    """Normalize truncated Gemini embeddings for cosine search."""
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]
