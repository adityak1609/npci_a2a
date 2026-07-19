"""Knowledge Base paste-and-index ingestion.

Chunks + embeds free-text (specs, circulars, internal standards) pasted in the
portal and stores it as retrievable ``document_chunks`` so the RAG / Design /
Code / Test agents can use it. Reuses the same chunking + embedding pipeline as
PDF ingestion.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from database import DocumentChunk, DocumentSummary, SessionLocal
from rag.ingest import _get_client, chunk_parent_child, generate_embeddings
from rag.retriever import invalidate_cache

KB_SOURCE = "kb_paste"

# A2A-ingested docs are stored as source_file="a2a_{document_id}_{filename}".
_A2A_SOURCE_FILE = re.compile(r"^a2a_\d+_(.+)$")

# Friendly labels for the source badge shown in the KB list.
SOURCE_LABELS = {
    "kb_paste": "Pasted",
    "npci_official": "NPCI (received)",
    "npci_agent": "NPCI (received)",
    "admin_upload": "Admin upload",
}


def _display_label(source_file: str) -> str:
    """Strip the internal ``a2a_{id}_`` prefix for display."""
    m = _A2A_SOURCE_FILE.match(source_file)
    return m.group(1) if m else source_file


def ingest_text_document(title: str, content: str, source: str | None = None) -> dict:
    """Chunk, embed and index a pasted document. Returns a summary dict.

    Runs synchronously (blocking on the embedding API) — call via
    ``asyncio.to_thread`` from request handlers.
    """
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise ValueError("title is required.")
    if not content:
        raise ValueError("content is required.")

    chunks = chunk_parent_child(content)
    if not chunks:
        raise ValueError("content produced no indexable text.")

    client = _get_client()
    embeddings = generate_embeddings([c["text"] for c in chunks], client)

    db = SessionLocal()
    try:
        # Replace any existing KB doc with the same title (re-index).
        db.query(DocumentChunk).filter(DocumentChunk.source_file == title).delete()
        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            db.add(DocumentChunk(
                source_file=title,
                chunk_index=idx,
                text=chunk["text"],
                embedding=embedding,
                parent_key=chunk.get("parent_key"),
                parent_text=chunk.get("parent_text"),
                source=KB_SOURCE,
                visibility_tier="admin",
                source_doc=source,
            ))
        summary_text = content[:500] + ("…" if len(content) > 500 else "")
        existing = db.query(DocumentSummary).filter(DocumentSummary.filename == title).first()
        if existing:
            existing.source = KB_SOURCE
            existing.summary = summary_text
        else:
            db.add(DocumentSummary(filename=title, source=KB_SOURCE, summary=summary_text))
        db.commit()
    finally:
        db.close()

    invalidate_cache()
    return {"title": title, "chunks_indexed": len(chunks), "indexed_at": datetime.now(timezone.utc).isoformat()}


def list_kb_documents() -> list[dict]:
    """List the whole indexed RAG corpus, grouped by source_file.

    Covers every retrievable document — pasted (``kb_paste``), A2A-received
    (``npci_official``/``npci_agent``), and admin uploads — not just pasted docs,
    since the Knowledge Base page represents everything the RAG / Design / Code /
    Test agents can retrieve. ``title`` is the internal delete key (source_file);
    ``label`` is the human-friendly name.
    """
    from sqlalchemy import func

    db = SessionLocal()
    try:
        rows = (
            db.query(
                DocumentChunk.source_file,
                func.count(DocumentChunk.id).label("chunks"),
                func.max(DocumentChunk.created_at).label("created_at"),
                func.max(DocumentChunk.source).label("source"),
                func.max(DocumentChunk.document_id).label("document_id"),
            )
            .group_by(DocumentChunk.source_file)
            .all()
        )
        # Summaries are keyed by filename (kb_paste, where filename==source_file)
        # or by document_id (A2A ingests). Index both ways.
        summ_by_file: dict[str, str] = {}
        summ_by_docid: dict[int, str] = {}
        for s in db.query(DocumentSummary).all():
            if s.filename:
                summ_by_file[s.filename] = s.summary
            if s.document_id is not None:
                summ_by_docid[s.document_id] = s.summary

        out = []
        for r in rows:
            summary = summ_by_file.get(r.source_file)
            if summary is None and r.document_id is not None:
                summary = summ_by_docid.get(r.document_id)
            out.append({
                "title": r.source_file,
                "label": _display_label(r.source_file),
                "source": r.source or "unknown",
                "source_label": SOURCE_LABELS.get(r.source or "", r.source or "unknown"),
                "chunks": r.chunks,
                "summary": summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        out.sort(key=lambda d: d["created_at"] or "", reverse=True)
        return out
    finally:
        db.close()


def delete_kb_document(title: str) -> int:
    """Delete a document and its chunks from the KB by source_file. Returns chunks removed.

    Removes the document from the retrievable corpus regardless of its source.
    A2A summaries are keyed by ``document_id``, so resolve those before deleting.
    """
    db = SessionLocal()
    try:
        doc_ids = [
            row[0]
            for row in db.query(DocumentChunk.document_id)
            .filter(DocumentChunk.source_file == title)
            .distinct()
            .all()
            if row[0] is not None
        ]
        removed = db.query(DocumentChunk).filter(DocumentChunk.source_file == title).delete()
        db.query(DocumentSummary).filter(DocumentSummary.filename == title).delete()
        if doc_ids:
            db.query(DocumentSummary).filter(DocumentSummary.document_id.in_(doc_ids)).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()
    invalidate_cache()
    return removed
