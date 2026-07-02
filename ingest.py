"""
ingest.py — PDF ingestion pipeline for Mindgate Policy Bot.

Reads PDF files from the data/ folder, splits them into overlapping chunks,
generates embeddings via Google gemini-embedding-001, and stores everything
in PostgreSQL using pgvector.

Multimodal mode (USE_VISION=True): each page is also rendered as an
image and sent to Gemini 2.5 Flash to describe charts, tables, graphs
and diagrams. The description is appended to the page text so visual
content becomes fully searchable.

Usage (standalone):
    python ingest.py

The script can also be imported and called via `run_ingestion()` from
the FastAPI /ingest endpoint or the watchdog file watcher.
"""

import os
import hashlib
import json
import time
import base64
import fitz  # PyMuPDF
from google import genai
from google.genai import types
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import Session

from compliance import require_external_ai_approval
from database import DocumentChunk, SessionLocal, init_db
from embedding_config import embedding_dim, normalize_embedding
from guardrails import sanitize_chunk

# Load environment variables (expects GOOGLE_API_KEY)
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
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
USE_VISION = False              # Set True to enable vision (requires paid quota)
VISION_MODEL = "gemini-2.5-flash"
VISION_DPI = 150
VISION_RATE_LIMIT_SLEEP = 4

VISION_PROMPT = """\
Analyze this PDF page image and extract all non-text visual information.

Describe in detail:
- Tables: all column headers and every cell value
- Charts / graphs: type (bar, pie, line…), title, axis labels, all data values and trends
- Diagrams / flowcharts: every element and relationship
- Infographics: all numbers, labels, and categories
- Any text embedded inside images or figures

If this page contains ONLY regular paragraph text with no tables, charts, graphs,
diagrams, or embedded image-text, respond with exactly the word: NO_VISUALS

Be precise — include every number, percentage, and label you can read."""


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
def _get_client() -> genai.Client:
    """Create a Gemini client using the GOOGLE_API_KEY env var."""
    require_external_ai_approval("PDF ingestion, embeddings, summaries, and optional vision")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("GOOGLE_API_KEY not set. Add it to your .env file.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=API_TIMEOUT_MS),
    )


# ---------------------------------------------------------------------------
# Step 1a: Vision — describe visual elements on a single page
# ---------------------------------------------------------------------------
def _describe_page_visuals(client: genai.Client, page: fitz.Page, page_num: int) -> str:
    """
    Render a PDF page as a PNG image and ask Gemini Vision to describe
    all non-text visual content (charts, tables, graphs, diagrams).
    """
    pix = page.get_pixmap(dpi=VISION_DPI)
    img_bytes = pix.tobytes("png")
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

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
            description = response.text.strip()
            return "" if description == "NO_VISUALS" else description

        except Exception as e:
            err = str(e)
            if any(code in err for code in retryable) and attempt < 4:
                wait = min(2 ** (attempt + 1), 60)
                print(f"    [vision] Transient error on page {page_num}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"    [vision] Failed on page {page_num}: {err[:80]}")
                return ""
    return ""


# ---------------------------------------------------------------------------
# Step 1b: Extract text (+ optional vision descriptions) from PDF
# ---------------------------------------------------------------------------
def extract_text_from_pdf(pdf_path: str, client: genai.Client | None = None) -> str:
    """
    Open the PDF with PyMuPDF and concatenate all page texts.

    When USE_VISION is True, each page is also analyzed for visual content.
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    full_text = ""

    vision_enabled = USE_VISION and client is not None
    print(f"  Vision mode {'ON' if vision_enabled else 'OFF'}.")

    for page_num in range(total_pages):
        page = doc.load_page(page_num)
        text = page.get_text("text")
        page_content = f"\n\n--- Page {page_num + 1} ---\n\n{text}"

        if vision_enabled:
            print(f"  [vision] Analyzing page {page_num + 1}/{total_pages}...")
            vision_desc = _describe_page_visuals(client, page, page_num + 1)
            if vision_desc:
                page_content += f"\n\n[Visual Content - Page {page_num + 1}]:\n{vision_desc}"
            if page_num < total_pages - 1:
                time.sleep(VISION_RATE_LIMIT_SLEEP)

        full_text += page_content

    doc.close()
    return full_text.strip()


# ---------------------------------------------------------------------------
# Step 2: Chunk the text
# ---------------------------------------------------------------------------
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks based on approximate token count.
    Uses word-based approximation: 1 token ≈ 0.75 words.

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
                    print(f"    Transient error. Waiting {wait}s before retry ({attempt + 1}/{max_retries})...")
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
        print(f"  Embedding chunk {i + 1}/{total}...")
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
def upsert_chunks(db: Session, source_file: str, chunks: list[str], embeddings: list[list[float]]) -> int:
    """
    Upsert text chunks and their embeddings into the document_chunks table.

    Clears all existing chunks for this source_file before inserting,
    ensuring a clean re-ingestion when the PDF changes.

    Args:
        db:          SQLAlchemy session.
        source_file: Basename of the PDF file (e.g., "policy.pdf").
        chunks:      List of text chunks.
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
        doc_chunk = DocumentChunk(
            source_file=source_file,
            chunk_index=idx,
            text=chunk,
            embedding=emb,
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

    print(f"[1/4] Extracting text from {source_file}...")
    text = extract_text_from_pdf(target_path, client=client)
    print(f"  Extracted {len(text):,} characters.")

    print("[2/4] Chunking text...")
    chunks = chunk_text(text)
    print(f"  Created {len(chunks)} chunks.")

    print("[3/4] Generating embeddings...")
    embeddings = generate_embeddings(chunks, client)
    print(f"  Generated {len(embeddings)} embeddings (dim={len(embeddings[0]) if embeddings else 0}).")

    print("[4/4] Upserting into PostgreSQL (pgvector)...")
    db = SessionLocal()
    try:
        count = upsert_chunks(db, source_file, chunks, embeddings)
    finally:
        db.close()

    print(f"[OK] Ingestion complete. {count} chunks stored for '{source_file}'.")
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
    # The FastAPI app initializes the schema once during startup. Standalone
    # ingestion still needs to initialize it explicitly before doing work.
    init_db()
    run_ingestion()
