"""Local Tesseract OCR with a 5-factor quality gate.

Cost/egress guard for image ingestion: try local Tesseract first and only
fall back to Gemini Vision when the OCR output fails ANY of five orthogonal
quality checks. A page that passes the gate never leaves the machine.

The five factors (all must pass — each catches a failure mode the others
miss; mean confidence alone is fooled by confident-but-wrong output):

    word count       ≥ 6     enough real tokens (conf > 0) to judge at all
    mean confidence  ≥ 60    systematic uncertainty (bad DPI, odd font)
    p10 confidence   ≥ 35    localized garbage (watermark corner) hiding
                             behind a healthy mean
    letter-word ratio ≥ 0.25 fraction of tokens with a [A-Za-z]{3,} run —
                             filters symbol soup from diagrams
    garbage ratio    < 0.30  linguistically implausible tokens (>20 chars,
                             3+ repeats, 4+ vowels, 6+ consonants, no alnum)

All thresholds are env-tunable (A2A_OCR_*). Dependencies (pytesseract,
Pillow, plus the tesseract binary) are optional: when missing, the gate
reports "unavailable" and callers use Gemini Vision exactly as before.

NOTE — threshold caveat for NPCI corpora: pages dominated by long reference
IDs (RRN/UTR) and dense numeric settlement tables can trip the letter-ratio
and garbage checks. Watch rag_ocr_gate_total{result="fail"} and tune the
A2A_OCR_* thresholds against real documents before trusting the gate widely.
"""

from __future__ import annotations

import io
import logging
import os
import re

from core.metrics import inc_counter

logger = logging.getLogger(__name__)

_LETTER_RUN = re.compile(r"[A-Za-z]{3,}")
_REPEAT_RUN = re.compile(r"(.)\1\1")
_VOWEL_RUN = re.compile(r"[aeiouAEIOU]{4,}")
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ]{6,}")
_ALNUM = re.compile(r"[A-Za-z0-9]")

MIN_IMAGE_SIDE = 100          # skip tiny images (logos, icons) entirely
UPSCALE_LONGEST_SIDE = 1000   # normalize resolution before OCR


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _is_garbage_token(token: str) -> bool:
    """Linguistic implausibility battery — classic Tesseract-on-a-flowchart output."""
    if len(token) > 20:
        return True
    if _REPEAT_RUN.search(token):
        return True
    if _VOWEL_RUN.search(token):
        return True
    if _CONSONANT_RUN.search(token):
        return True
    if not _ALNUM.search(token):
        return True
    return False


def evaluate_ocr_quality(words: list[str], confidences: list[float]) -> tuple[bool, dict]:
    """Apply the 5-factor gate to Tesseract token output.

    Args:
        words:        recognized tokens (parallel to confidences).
        confidences:  per-token confidence 0–100; Tesseract uses -1 for
                      layout artifacts, which are excluded up front.

    Returns:
        (passed, stats) — stats carries every factor for logging/tuning.
    """
    valid = [
        (w.strip(), c)
        for w, c in zip(words, confidences)
        if c is not None and c > 0 and w and w.strip()
    ]
    stats: dict = {"word_count": len(valid)}

    min_words = int(_env_float("A2A_OCR_MIN_WORDS", 6))
    if len(valid) < min_words:
        stats["fail"] = "word_count"
        return False, stats

    confs = sorted(c for _, c in valid)
    tokens = [w for w, _ in valid]

    mean_conf = sum(confs) / len(confs)
    p10_conf = confs[max(0, int(len(confs) * 0.10) - 1)] if len(confs) >= 10 else confs[0]
    letter_ratio = sum(1 for t in tokens if _LETTER_RUN.search(t)) / len(tokens)
    garbage_ratio = sum(1 for t in tokens if _is_garbage_token(t)) / len(tokens)

    stats.update({
        "mean_conf": round(mean_conf, 1),
        "p10_conf": round(p10_conf, 1),
        "letter_ratio": round(letter_ratio, 3),
        "garbage_ratio": round(garbage_ratio, 3),
    })

    if mean_conf < _env_float("A2A_OCR_MIN_MEAN_CONF", 60):
        stats["fail"] = "mean_conf"
        return False, stats
    if p10_conf < _env_float("A2A_OCR_MIN_P10_CONF", 35):
        stats["fail"] = "p10_conf"
        return False, stats
    if letter_ratio < _env_float("A2A_OCR_MIN_LETTER_RATIO", 0.25):
        stats["fail"] = "letter_ratio"
        return False, stats
    if garbage_ratio >= _env_float("A2A_OCR_MAX_GARBAGE_RATIO", 0.30):
        stats["fail"] = "garbage_ratio"
        return False, stats

    return True, stats


def ocr_enabled() -> bool:
    return os.getenv("A2A_OCR_TESSERACT_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def tesseract_extract(content: bytes) -> tuple[str | None, dict]:
    """OCR image bytes with Tesseract and apply the quality gate.

    Returns (text, stats). ``text`` is None whenever the caller should fall
    back to Gemini Vision — gate failure, tiny image, or Tesseract/Pillow
    not installed (stats["reason"] says which).
    """
    if not ocr_enabled():
        return None, {"reason": "disabled"}
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        inc_counter("rag_ocr_gate_total", {"result": "unavailable"})
        return None, {"reason": "tesseract_unavailable"}

    try:
        image = Image.open(io.BytesIO(content))
        image.load()
        if min(image.size) < MIN_IMAGE_SIDE:
            inc_counter("rag_ocr_gate_total", {"result": "skipped_small"})
            return None, {"reason": "image_too_small", "size": list(image.size)}

        # Pre-process: grayscale + upscale so small print OCRs reliably.
        image = image.convert("L")
        longest = max(image.size)
        if longest < UPSCALE_LONGEST_SIDE:
            scale = UPSCALE_LONGEST_SIDE / longest
            image = image.resize(
                (int(image.width * scale), int(image.height * scale))
            )

        data = pytesseract.image_to_data(
            image, lang="eng", config="--psm 3", output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        inc_counter("rag_ocr_gate_total", {"result": "error"})
        return None, {"reason": "tesseract_error", "error": str(exc)[:120]}

    confidences = [float(c) for c in data.get("conf", [])]
    words = [str(w) for w in data.get("text", [])]
    passed, stats = evaluate_ocr_quality(words, confidences)
    if not passed:
        inc_counter("rag_ocr_gate_total", {"result": "fail"})
        logger.info("Gate failed (%s), falling back to Vision: %s", stats.get("fail"), stats)
        return None, stats

    inc_counter("rag_ocr_gate_total", {"result": "pass"})
    text = " ".join(
        w.strip() for w, c in zip(words, confidences) if c > 0 and w.strip()
    )
    return text, stats
