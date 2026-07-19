"""RAG pipeline: document ingestion, hybrid retrieval, prompt building,
embedding configuration.

Import submodules directly (e.g. ``from rag.retriever import retrieve``).
Keep this ``__init__`` import-free: ``database.py`` imports
``rag.embedding_config`` while ``rag.ingest`` imports ``database`` — eager
imports here would create a cycle.
"""
