"""File validation before OCR, parsing, embedding, or storage."""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

import fitz


class FileValidationError(ValueError):
    """Raised when an uploaded or inbound document fails security validation."""


PDF_MAGIC = b"%PDF-"
IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",  # WEBP is RIFF....WEBP; checked below.
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def max_admin_upload_bytes() -> int:
    return _env_int("ADMIN_UPLOAD_MAX_BYTES", 25 * 1024 * 1024)


def max_pdf_pages() -> int:
    return _env_int("PDF_MAX_PAGES", 500)


def validate_pdf_bytes(content: bytes, *, filename: str, max_bytes: int | None = None) -> None:
    """Validate PDF size, magic bytes, parseability, encryption, and page count."""
    limit = max_bytes or max_admin_upload_bytes()
    if not content:
        raise FileValidationError("Uploaded PDF is empty.")
    if len(content) > limit:
        raise FileValidationError(f"PDF exceeds the maximum allowed size of {limit} bytes.")
    if not filename.lower().endswith(".pdf"):
        raise FileValidationError("Only .pdf files are accepted.")
    if not content.startswith(PDF_MAGIC):
        raise FileValidationError("File content is not a valid PDF.")

    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise FileValidationError(f"Malformed PDF rejected: {exc}") from exc

    try:
        if doc.is_encrypted:
            raise FileValidationError("Encrypted PDFs are not accepted.")
        if doc.page_count <= 0:
            raise FileValidationError("PDF has no pages.")
        if doc.page_count > max_pdf_pages():
            raise FileValidationError(
                f"PDF has {doc.page_count} pages; maximum is {max_pdf_pages()}."
            )
    finally:
        doc.close()


def validate_image_bytes(content: bytes, *, max_bytes: int) -> None:
    if not content:
        raise FileValidationError("Image content is empty.")
    if len(content) > max_bytes:
        raise FileValidationError(f"Image exceeds the maximum allowed size of {max_bytes} bytes.")
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return
    if not any(content.startswith(sig) for sig in IMAGE_SIGNATURES if sig != b"RIFF"):
        raise FileValidationError("Unsupported or malformed image content.")


def validate_text_bytes(content: bytes, *, max_bytes: int) -> None:
    if not content:
        raise FileValidationError("Text content is empty.")
    if len(content) > max_bytes:
        raise FileValidationError(f"Text document exceeds the maximum allowed size of {max_bytes} bytes.")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileValidationError("Text document must be valid UTF-8.") from exc


def decode_base64_limited(content_b64: str, *, max_bytes: int) -> bytes:
    """Validate base64 and decoded size before returning bytes."""
    if len(content_b64) > int(max_bytes * 1.4):
        raise FileValidationError(f"Base64 content exceeds the maximum allowed decoded size of {max_bytes} bytes.")
    try:
        content = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FileValidationError("content_b64 is not valid base64.") from exc
    if len(content) > max_bytes:
        raise FileValidationError(f"Decoded content exceeds the maximum allowed size of {max_bytes} bytes.")
    return content


def validate_ingest_bytes(file_type: str, filename: str, content: bytes, *, max_bytes: int) -> None:
    safe_name = Path(filename).name
    if file_type == "pdf":
        validate_pdf_bytes(content, filename=safe_name, max_bytes=max_bytes)
    elif file_type == "image":
        validate_image_bytes(content, max_bytes=max_bytes)
    elif file_type == "text":
        validate_text_bytes(content, max_bytes=max_bytes)
    else:
        raise FileValidationError(f"Unsupported file_type '{file_type}'.")

