"""Image text extraction for A2A image documents.

Hybrid OCR: local Tesseract runs first behind a 5-factor quality gate
(services/ocr_gate.py). Only when the gate fails — diagrams, flowcharts,
low-quality scans — does the image fall back to Gemini Vision, which can
describe visuals semantically. Gate passes keep the document on-machine
(no external egress, no API cost)."""

import base64
import logging
import os

from google import genai

from security.compliance import require_external_ai_approval
from services.ocr_gate import tesseract_extract

logger = logging.getLogger(__name__)

VISION_PROMPT = """You are a document analysis assistant for a financial compliance system.
Extract ALL visible text from this image verbatim.
Then under a heading '## Visual Elements', describe every table, chart,
diagram, figure, or non-text visual in detail suitable for a knowledge base.
Be exhaustive — this content will be used to answer compliance queries."""


def detect_mime_type(content: bytes) -> str:
    """agent.ingest_document; params: image bytes; returns detected image MIME type."""
    if content.startswith(b"\x89PNG"):
        return "image/png"
    if content.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if content.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def extract_image_text(content: bytes) -> str:
    """agent.ingest_document; params: image bytes; returns extracted image text.

    Tesseract-first: a gate pass returns local OCR text with zero egress.
    Any gate failure falls through to the original Gemini Vision path.
    """
    ocr_text, stats = tesseract_extract(content)
    if ocr_text:
        logger.info("Tesseract gate passed (%s); skipping Gemini Vision.", stats)
        return ocr_text

    require_external_ai_approval("A2A image OCR/vision extraction")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise OSError("GOOGLE_API_KEY not set.")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": detect_mime_type(content),
                        "data": base64.b64encode(content).decode("utf-8"),
                    }
                },
                {"text": VISION_PROMPT},
            ]
        }],
    )
    return (response.text or "").strip()
