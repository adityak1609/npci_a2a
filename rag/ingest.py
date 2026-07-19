"""
rag/ingest.py — PDF ingestion pipeline for Mindgate Policy Bot.

Reads PDF files from the data/ folder, splits them into overlapping chunks,
generates embeddings via Google gemini-embedding-001, and stores everything
in PostgreSQL using pgvector.

Tables: every page is run through PyMuPDF's table finder
(A2A_INGEST_EXTRACT_TABLES, default on). Detected tables are rendered as
GitHub-flavored markdown and appended to the page text, so the row/column
structure that get_text("text") flattens into ambiguous whitespace is
preserved and a query about one cell can retrieve its whole row intact.

Multimodal mode (A2A_INGEST_USE_VISION=true): embedded page images are
OCR'd locally with Tesseract behind the 5-factor quality gate
(services/ocr_gate.py). Only images the gate rejects — charts, diagrams,
low-quality scans — are sent to Gemini 2.5 Flash for semantic description.
The extracted text is appended to the page text so visual content becomes
fully searchable, while text-dominant images never leave the machine.

Usage (standalone):
    python -m rag.ingest

The script can also be imported and called via `run_ingestion()` from
the FastAPI /ingest endpoint or the watchdog file watcher.
"""

import base64
import hashlib
import logging
import os
import time

import fitz  # PyMuPDF
from dotenv import load_dotenv
from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from core.metrics import inc_counter
from database import DocumentChunk, SessionLocal, init_db
from rag.embedding_config import embedding_dim, normalize_embedding
from security.compliance import require_external_ai_approval
from security.guardrails import sanitize_chunk
from services.ocr_gate import tesseract_extract

# Load environment variables (expects GOOGLE_API_KEY)
load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Project root is one level up from this package (rag/ingest.py → repo root).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
PDF_PATH = os.path.join(DATA_DIR, "policy.pdf")   # default legacy path

CHUNK_SIZE = 800        # Approximate token count per chunk
CHUNK_OVERLAP = 100     # Overlap in tokens between consecutive chunks
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = embedding_dim()
API_TIMEOUT_MS = 30_000
MAX_EMBEDDING_RETRIES = 2

# ---------------------------------------------------------------------------
# Vision configuration
# ---------------------------------------------------------------------------
def _vision_enabled() -> bool:
    """Hybrid OCR/vision mode for embedded page images (A2A_INGEST_USE_VISION)."""
    return os.getenv("A2A_INGEST_USE_VISION", "false").strip().lower() in {"1", "true", "yes"}


def _tables_enabled() -> bool:
    """Structured table extraction via PyMuPDF's table finder (A2A_INGEST_EXTRACT_TABLES)."""
    return os.getenv("A2A_INGEST_EXTRACT_TABLES", "true").strip().lower() in {"1", "true", "yes"}


# A detected region is only treated as a real table when it has at least this
# many rows and columns — a 1xN or Nx1 "table" is almost always a mis-detected
# text column or a single label, and rendering it as markdown adds noise.
MIN_TABLE_ROWS = int(os.getenv("A2A_INGEST_MIN_TABLE_ROWS", "2"))
MIN_TABLE_COLS = int(os.getenv("A2A_INGEST_MIN_TABLE_COLS", "2"))


VISION_MODEL = "gemini-2.5-flash"
VISION_DPI = 150
VISION_RATE_LIMIT_SLEEP = 4

# Pages whose text layer is shorter than this are treated as scans (no
# extractable text) and their full-page render goes through the hybrid
# OCR path even when they carry no embedded image objects.
MIN_PAGE_TEXT_CHARS = int(os.getenv("A2A_INGEST_MIN_PAGE_TEXT", "40"))

VISION_PROMPT = """\
Analyze this image from a PDF document and extract all information it contains.

Describe in detail:
- Tables: all column headers and every cell value
- Charts / graphs: type (bar, pie, line…), title, axis labels, all data values and trends
- Diagrams / flowcharts: every element and relationship
- Infographics: all numbers, labels, and categories
- Any text embedded inside the image

If this image contains no readable text or meaningful visual content,
respond with exactly the word: NO_VISUALS

Be precise — include every number, percentage, and label you can read."""


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
def _get_client() -> genai.Client:
    """Create a Gemini client using the GOOGLE_API_KEY env var."""
    require_external_ai_approval("PDF ingestion, embeddings, summaries, and optional vision")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise OSError("GOOGLE_API_KEY not set. Add it to your .env file.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=API_TIMEOUT_MS),
    )

# ---------------------------------------------------------------------------
# Step 1a: Hybrid OCR — Tesseract gate first, Gemini Vision on gate failure
# ---------------------------------------------------------------------------
def _gemini_describe_png(client: genai.Client, png_bytes: bytes, page_num: int) -> str:
    """Ask Gemini Vision to extract text and describe visuals in one image."""
    img_b64 = base64.b64encode(png_bytes).decode("utf-8")
    retryable = ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL"]

    for attempt in range(5):
        try:
            response = client.models.generate_content(
                model=VISION_MODEL,
                contents=[{
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": img_b64}},
                        {"text": VISION_PROMPT},
                    ]
                }],
            )
            description = (response.text or "").strip()
            return "" if description == "NO_VISUALS" else description

        except Exception as e:
            err = str(e)
            if any(code in err for code in retryable) and attempt < 4:
                wait = min(2 ** (attempt + 1), 60)
                logger.warning("Vision: transient error on page %s, retrying in %ss", page_num, wait)
                time.sleep(wait)
            else:
                logger.error("Vision failed on page %s: %s", page_num, err[:80])
                return ""
    return ""


def _hybrid_image_text(client: genai.Client, png_bytes: bytes, page_num: int) -> tuple[str, bool]:
    """Extract text from one image: Tesseract gate first, Gemini on failure.

    Returns (text, used_gemini). A gate pass keeps the image on-machine —
    no egress, no API cost. Tiny images (logos, icons) are skipped outright
    rather than burning a Gemini call on them.
    """
    ocr_text, stats = tesseract_extract(png_bytes)
    if ocr_text:
        inc_counter("rag_ingest_image_route_total", {"route": "tesseract"})
        logger.info("Page %s: Tesseract gate passed (%s), staying local.", page_num, stats)
        return ocr_text, False

    if stats.get("reason") == "image_too_small":
        inc_counter("rag_ingest_image_route_total", {"route": "skipped_small"})
        return "", False

    inc_counter("rag_ingest_image_route_total", {"route": "gemini"})
    return _gemini_describe_png(client, png_bytes, page_num), True


def _page_image_pngs(doc: fitz.Document, page: fitz.Page) -> list[bytes]:
    """Extract the page's embedded images, normalized to PNG bytes."""
    pngs: list[bytes] = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.colorspace is None or pix.n - pix.alpha > 3:  # CMYK / stencil → RGB
                pix = fitz.Pixmap(fitz.csRGB, pix)
            pngs.append(pix.tobytes("png"))
        except Exception as exc:
            logger.warning("Could not extract image xref %s: %s", xref, str(exc)[:80])
    return pngs


def _page_visual_texts(
    client: genai.Client, doc: fitz.Document, page: fitz.Page,
    page_num: int, page_text: str,
) -> tuple[list[str], bool]:
    """Run the hybrid OCR path over one page's visual content.

    Embedded images are processed individually so a text-heavy scan stays
    local while the flowchart next to it goes to Gemini. Pages with no
    embedded images but an empty/short text layer (vector-drawn scans)
    fall back to a full-page render through the same gate.

    Returns (extracted texts, any_gemini_call_made).
    """
    pngs = _page_image_pngs(doc, page)
    if not pngs and len(page_text.strip()) < MIN_PAGE_TEXT_CHARS:
        pngs = [page.get_pixmap(dpi=VISION_DPI).tobytes("png")]

    texts: list[str] = []
    used_gemini = False
    for png in pngs:
        text, called_gemini = _hybrid_image_text(client, png, page_num)
        used_gemini = used_gemini or called_gemini
        if text:
            texts.append(text)
    return texts, used_gemini


# ---------------------------------------------------------------------------
# Step 1b: Structured table extraction
# ---------------------------------------------------------------------------
def _page_tables_markdown(page: fitz.Page) -> list[str]:
    """Reconstruct a page's tables as GitHub-flavored markdown.

    ``page.get_text("text")`` reads cells in geometric order and collapses
    the grid into whitespace, so headers drift away from their values and a
    multi-column rate/fee table becomes unretrievable. PyMuPDF's table finder
    recovers the row/column structure; rendering each table as markdown keeps
    every header aligned with its cell so the whole row survives chunking.

    Failures are non-fatal: table detection is best-effort and never blocks
    the plain-text extraction that always runs alongside it.
    """
    if not _tables_enabled():
        return []
    try:
        finder = page.find_tables()
    except Exception as exc:  # pragma: no cover - defensive; PyMuPDF internals
        logger.warning("Table detection failed on page %s: %s", page.number + 1, str(exc)[:80])
        return []

    tables_md: list[str] = []
    for table in getattr(finder, "tables", []):
        try:
            if table.row_count < MIN_TABLE_ROWS or table.col_count < MIN_TABLE_COLS:
                continue
            markdown = (table.to_markdown() or "").strip()
        except Exception as exc:
            logger.warning("Table render failed on page %s: %s", page.number + 1, str(exc)[:80])
            continue
        if markdown:
            tables_md.append(markdown)
    return tables_md


# ---------------------------------------------------------------------------
# Step 1c: Extract text (+ tables, + optional vision descriptions) from PDF
# ---------------------------------------------------------------------------
def _extract_doc_content(doc: fitz.Document, client: genai.Client | None) -> str:
    """Concatenate every page's text, structured tables, and visual content.

    Shared by the file-path and in-memory-bytes entry points so the local
    admin ingest and the A2A inbound pipeline extract PDFs identically.

    Tables (A2A_INGEST_EXTRACT_TABLES) are always attempted locally — no API
    call, no egress. When A2A_INGEST_USE_VISION is true and a client is
    supplied, embedded images (and scan-like pages without a text layer) also
    run through the hybrid OCR path: local Tesseract behind the 5-factor
    quality gate, with Gemini Vision reserved for images the gate rejects. The
    rate-limit sleep applies only after pages that actually called Gemini.
    """
    total_pages = len(doc)
    full_text = ""

    vision_enabled = _vision_enabled() and client is not None
    logger.info(
        "PDF extraction: tables %s, hybrid OCR/vision %s.",
        "ON" if _tables_enabled() else "OFF",
        "ON" if vision_enabled else "OFF",
    )

    for page_num in range(total_pages):
        page = doc.load_page(page_num)
        text = page.get_text("text")
        page_content = f"\n\n--- Page {page_num + 1} ---\n\n{text}"

        tables = _page_tables_markdown(page)
        if tables:
            joined = "\n\n".join(tables)
            page_content += f"\n\n[Tables - Page {page_num + 1}]:\n{joined}"
            inc_counter("rag_ingest_tables_total", {"result": "extracted"})

        if vision_enabled:
            visual_texts, used_gemini = _page_visual_texts(
                client, doc, page, page_num + 1, text
            )
            if visual_texts:
                joined = "\n\n".join(visual_texts)
                page_content += f"\n\n[Visual Content - Page {page_num + 1}]:\n{joined}"
            if used_gemini and page_num < total_pages - 1:
                time.sleep(VISION_RATE_LIMIT_SLEEP)

        full_text += page_content

    return full_text.strip()


def extract_text_from_pdf(pdf_path: str, client: genai.Client | None = None) -> str:
    """Open a PDF file on disk and extract its text, tables, and visuals.

    See :func:`_extract_doc_content` for the table/vision behavior.
    """
    doc = fitz.open(pdf_path)
    try:
        return _extract_doc_content(doc, client)
    finally:
        doc.close()


def extract_text_from_pdf_bytes(content: bytes, client: genai.Client | None = None) -> str:
    """Extract text, tables, and visuals from in-memory PDF bytes.

    Used by the A2A inbound ingestion path, which holds received documents in
    memory rather than on a stable disk path. Behavior is identical to
    :func:`extract_text_from_pdf`.
    """
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        return _extract_doc_content(doc, client)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Step 2: Chunk the text — parent-child chunking
# ---------------------------------------------------------------------------
# Children (~350 chars) are embedded for precise matching; the parent passage
# (~1100 chars, hard cap 1500) is what the LLM ultimately receives, so answers
# keep their surrounding clauses and table rows intact.
PARENT_TARGET_CHARS = int(os.getenv("A2A_PARENT_TARGET_CHARS", "1100"))
PARENT_HARD_CAP_CHARS = int(os.getenv("A2A_PARENT_HARD_CAP_CHARS", "1500"))
CHILD_TARGET_CHARS = int(os.getenv("A2A_CHILD_TARGET_CHARS", "350"))
CHILD_OVERLAP_CHARS = int(os.getenv("A2A_CHILD_OVERLAP_CHARS", "100"))
CHILD_MIN_CHARS = int(os.getenv("A2A_CHILD_MIN_CHARS", "120"))


def _split_units(text: str) -> list[str]:
    """Split text into paragraph units, falling back to sentence-ish pieces
    so a single oversized paragraph can still be packed under the hard cap."""
    import re as _re

    units: list[str] = []
    for para in _re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= PARENT_HARD_CAP_CHARS:
            units.append(para)
            continue
        # Oversized paragraph: split on sentence boundaries, then hard-wrap.
        for sent in _re.split(r"(?<=[.!?])\s+", para):
            sent = sent.strip()
            if not sent:
                continue
            while len(sent) > PARENT_HARD_CAP_CHARS:
                units.append(sent[:PARENT_HARD_CAP_CHARS])
                sent = sent[PARENT_HARD_CAP_CHARS:].strip()
            if sent:
                units.append(sent)
    return units


def _split_children(parent: str) -> list[str]:
    """Word-boundary sliding window over one parent passage."""
    if len(parent) <= CHILD_TARGET_CHARS:
        return [parent]

    words = parent.split()
    children: list[str] = []
    current: list[str] = []
    current_len = 0
    for i, word in enumerate(words):
        current.append(word)
        current_len += len(word) + 1
        if current_len >= CHILD_TARGET_CHARS:
            children.append(" ".join(current))
            # Walk back enough words to give ~CHILD_OVERLAP_CHARS of overlap.
            overlap_words: list[str] = []
            overlap_len = 0
            for w in reversed(current):
                overlap_len += len(w) + 1
                overlap_words.insert(0, w)
                if overlap_len >= CHILD_OVERLAP_CHARS:
                    break
            current = overlap_words
            current_len = sum(len(w) + 1 for w in current)
    tail = " ".join(current).strip()
    if tail:
        if children and len(tail) < CHILD_MIN_CHARS:
            # Too small to stand alone — merge into the previous child.
            children[-1] = f"{children[-1]} {tail}"
        else:
            children.append(tail)
    return children


def chunk_parent_child(text: str) -> list[dict]:
    """Parent-child chunking: pack paragraphs into ~PARENT_TARGET_CHARS
    parents, then window each parent into overlapping children.

    Returns one dict per CHILD (the embedded retrieval unit):
        {"text": child, "parent_key": "p<idx>", "parent_text": parent}

    Both levels are sanitized to neutralize prompt-injection patterns
    embedded in the source document.
    """
    units = _split_units(text)

    parents: list[str] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        unit_len = len(unit) + 2
        if current and current_len + unit_len > PARENT_TARGET_CHARS:
            parents.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(unit)
        current_len += unit_len
    if current:
        parents.append("\n\n".join(current))

    children: list[dict] = []
    for p_idx, parent in enumerate(parents):
        clean_parent = sanitize_chunk(parent.strip())
        if not clean_parent:
            continue
        for child in _split_children(clean_parent):
            child = child.strip()
            if not child:
                continue
            children.append({
                "text": sanitize_chunk(child),
                "parent_key": f"p{p_idx}",
                "parent_text": clean_parent,
            })
    return children


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Legacy flat chunker (word-window, 1 token ≈ 0.75 words). Retained for
    backward compatibility; new ingestion paths use chunk_parent_child().

    Each chunk is sanitized to neutralize any prompt-injection patterns
    that may have been embedded in the PDF text.
    """
    words = text.split()
    chunk_words = int(chunk_size * 0.75)
    overlap_words = int(overlap * 0.75)
    step = max(chunk_words - overlap_words, 1)

    chunks = []
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_words])
        if chunk.strip():
            # Sanitize each chunk to neutralize embedded instructions
            chunks.append(sanitize_chunk(chunk.strip()))

    return chunks


# ---------------------------------------------------------------------------
# Step 3: Generate embeddings
# ---------------------------------------------------------------------------
def _is_retryable(error_str: str) -> bool:
    retryable = ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL"]
    return any(code in error_str for code in retryable)


def _embed_single(
    client: genai.Client,
    text: str,
    max_retries: int = MAX_EMBEDDING_RETRIES,
) -> list[float]:
    """Embed a single text string with retry + exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            result = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
            )
            return normalize_embedding(result.embeddings[0].values)
        except Exception as e:
            error_str = str(e)
            if _is_retryable(error_str):
                if attempt < max_retries:
                    wait = min(2 ** (attempt + 1), 60)
                    logger.warning(
                        "Embedding: transient error, waiting %ss before retry (%s/%s)",
                        wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"API error after {max_retries} retries: {error_str}."
                    ) from e
            else:
                raise


def generate_embeddings(chunks: list[str], client: genai.Client) -> list[list[float]]:
    """
    Generate embeddings for a list of text chunks.
    Returns a list of float lists (one per chunk).
    """
    all_embeddings: list[list[float]] = []
    total = len(chunks)

    for i, chunk in enumerate(chunks):
        logger.info("Embedding chunk %s/%s...", i + 1, total)
        embedding = _embed_single(client, chunk)
        all_embeddings.append(embedding)
        if i < total - 1:
            time.sleep(1)  # Rate limit

    return all_embeddings


# ---------------------------------------------------------------------------
# Step 4: Compute file hash for change detection
# ---------------------------------------------------------------------------
def _file_sha256(path: str) -> str:
    """Compute SHA-256 hash of a file for change detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Step 5: Persist to PostgreSQL via pgvector
# ---------------------------------------------------------------------------
def upsert_chunks(db: Session, source_file: str, chunks: list, embeddings: list[list[float]]) -> int:
    """
    Upsert text chunks and their embeddings into the document_chunks table.

    Clears all existing chunks for this source_file before inserting,
    ensuring a clean re-ingestion when the PDF changes.

    Args:
        db:          SQLAlchemy session.
        source_file: Basename of the PDF file (e.g., "policy.pdf").
        chunks:      Child-chunk dicts from chunk_parent_child()
                     ({"text", "parent_key", "parent_text"}) or plain strings
                     from the legacy chunk_text() path.
        embeddings:  List of embedding vectors (same length as chunks).

    Returns:
        Number of chunks inserted.
    """
    if len(chunks) != len(embeddings):
        raise ValueError("Chunk and embedding counts do not match.")
    for emb in embeddings:
        if len(emb) != EMBEDDING_DIM:
            raise ValueError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIM}, got {len(emb)}."
            )

    # Delete and insert in one transaction. If insertion fails, the caller's
    # session close rolls back and the old chunks remain intact.
    db.query(DocumentChunk).filter(DocumentChunk.source_file == source_file).delete()

    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        if isinstance(chunk, dict):
            text_value = chunk["text"]
            parent_key = chunk.get("parent_key")
            parent_text = chunk.get("parent_text")
        else:
            text_value = chunk
            parent_key = None
            parent_text = None
        doc_chunk = DocumentChunk(
            source_file=source_file,
            chunk_index=idx,
            text=text_value,
            embedding=emb,
            parent_key=parent_key,
            parent_text=parent_text,
        )
        db.add(doc_chunk)

    db.commit()
    return len(chunks)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_ingestion(pdf_path: str | None = None) -> dict:
    """
    Full ingestion pipeline: PDF → chunks → embeddings → pgvector.

    Args:
        pdf_path: Path to the PDF to ingest. Defaults to data/policy.pdf.

    Returns:
        Summary dict with chunk count and source file name.

    Raises:
        FileNotFoundError: If the PDF does not exist.
    """
    target_path = pdf_path or PDF_PATH

    if not os.path.isfile(target_path):
        raise FileNotFoundError(
            f"PDF not found at {target_path}. "
            "Please place your PDF in the data/ directory."
        )

    source_file = os.path.basename(target_path)

    client = _get_client()

    logger.info("[1/4] Extracting text from %s...", source_file)
    text = extract_text_from_pdf(target_path, client=client)
    logger.info("Extracted %s characters.", f"{len(text):,}")

    logger.info("[2/4] Chunking text (parent-child)...")
    chunks = chunk_parent_child(text)
    parent_count = len({c["parent_key"] for c in chunks})
    logger.info("Created %s child chunks across %s parents.", len(chunks), parent_count)

    logger.info("[3/4] Generating embeddings...")
    embeddings = generate_embeddings([c["text"] for c in chunks], client)
    logger.info("Generated %s embeddings (dim=%s).", len(embeddings), len(embeddings[0]) if embeddings else 0)

    logger.info("[4/4] Upserting into PostgreSQL (pgvector)...")
    db = SessionLocal()
    try:
        count = upsert_chunks(db, source_file, chunks, embeddings)
    finally:
        db.close()

    logger.info("Ingestion complete. %s chunks stored for '%s'.", count, source_file)
    return {
        "status": "success",
        "source_file": source_file,
        "chunks": count,
        "embedding_dim": len(embeddings[0]) if embeddings else 0,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from core.config import configure_logging

    configure_logging()
    # The FastAPI app initializes the schema once during startup. Standalone
    # ingestion still needs to initialize it explicitly before doing work.
    init_db()
    run_ingestion()
