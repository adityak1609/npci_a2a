"""Unit tests for the hybrid retrieval building blocks.

Covers the pure functions — parent-child chunking, RRF fusion, source-bias
weighting, and the OCR quality gate — without needing Postgres or Gemini.
"""

import os
from unittest.mock import patch

import pytest

from rag.ingest import (
    CHILD_MIN_CHARS,
    CHILD_TARGET_CHARS,
    PARENT_HARD_CAP_CHARS,
    chunk_parent_child,
)
from rag.retriever import _source_bias, rrf_merge
from services.ocr_gate import _is_garbage_token, evaluate_ocr_quality

# ---------------------------------------------------------------------------
# Parent-child chunking
# ---------------------------------------------------------------------------

def _sample_text(paragraphs: int = 12, sentences: int = 6) -> str:
    para = " ".join(
        f"Clause {i} requires settlement within the prescribed timeout window."
        for i in range(sentences)
    )
    return "\n\n".join(f"Section {p}. {para}" for p in range(paragraphs))


def test_chunk_parent_child_structure():
    children = chunk_parent_child(_sample_text())
    assert children, "expected at least one child chunk"
    for child in children:
        assert set(child) == {"text", "parent_key", "parent_text"}
        assert child["text"].strip()
        assert child["parent_key"].startswith("p")
        # Every parent respects the hard cap.
        assert len(child["parent_text"]) <= PARENT_HARD_CAP_CHARS + 50


def test_children_are_smaller_than_parents():
    children = chunk_parent_child(_sample_text())
    multi = [c for c in children if len(c["parent_text"]) > CHILD_TARGET_CHARS]
    assert multi, "sample text should produce parents larger than one child"
    for child in multi:
        assert len(child["text"]) <= len(child["parent_text"])


def test_no_tiny_orphan_children():
    children = chunk_parent_child(_sample_text())
    by_parent: dict = {}
    for c in children:
        by_parent.setdefault((c["parent_key"], c["parent_text"]), []).append(c["text"])
    for texts in by_parent.values():
        if len(texts) > 1:
            assert all(len(t) >= CHILD_MIN_CHARS for t in texts[1:]), (
                "trailing children below CHILD_MIN_CHARS should be merged"
            )


def test_oversized_single_paragraph_is_split():
    text = "word " * 2000  # one giant paragraph, no sentence breaks
    children = chunk_parent_child(text)
    assert len({c["parent_key"] for c in children}) > 1


def test_empty_text_yields_no_chunks():
    assert chunk_parent_child("") == []
    assert chunk_parent_child("\n\n  \n") == []


# ---------------------------------------------------------------------------
# RRF fusion — worked example from the design doc
# ---------------------------------------------------------------------------

def _row(chunk_id: str) -> tuple:
    return (chunk_id, f"text-{chunk_id}")


def test_rrf_worked_example():
    """Chunk A: dense#1 + bm25#3; B: dense#2 + bm25#8; C: dense#5 + bm25#1."""
    a, b, c = _row("A"), _row("B"), _row("C")
    dense = [a, b, None, None, c]
    dense[2], dense[3] = _row("X1"), _row("X2")
    bm25 = [c, _row("Y1"), a, _row("Y2"), _row("Y3"), _row("Y4"), _row("Y5"), b]

    fused = rrf_merge([dense, bm25], k=60)
    scores = {row[0]: score for row, score in fused}

    assert scores["A"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["B"] == pytest.approx(1 / 62 + 1 / 68)
    assert scores["C"] == pytest.approx(1 / 65 + 1 / 61)
    # Consensus ordering: A > C > B.
    ordering = [row[0] for row, _ in fused[:3]]
    assert ordering == ["A", "C", "B"]


def test_rrf_single_list_hit_scores_lower_than_consensus():
    both = _row("both")
    only_top = _row("only_top")
    fused = rrf_merge([[only_top, both], [both]], k=60)
    scores = {row[0]: score for row, score in fused}
    assert scores["both"] > scores["only_top"]


def test_rrf_empty_lists():
    assert rrf_merge([[], []]) == []


# ---------------------------------------------------------------------------
# Source-authority bias
# ---------------------------------------------------------------------------

def test_source_bias_prefers_authoritative_sources():
    with patch.dict(os.environ, {}, clear=False):
        assert _source_bias("npci_official") < _source_bias("npci_agent")
        assert _source_bias("npci_agent") < _source_bias("admin_upload")
        assert _source_bias("admin_upload") < _source_bias("old_policy")
        assert _source_bias(None) == _source_bias("unknown_source")


def test_source_bias_env_override():
    with patch.dict(os.environ, {"A2A_SOURCE_BIAS_OLD_POLICY": "0.5"}):
        assert _source_bias("old_policy") == 0.5


# ---------------------------------------------------------------------------
# OCR quality gate
# ---------------------------------------------------------------------------

def _tokens(words_confs: list[tuple[str, float]]):
    return [w for w, _ in words_confs], [c for _, c in words_confs]


def test_gate_passes_clean_text():
    words, confs = _tokens([
        ("Settlement", 92), ("must", 90), ("complete", 88), ("within", 91),
        ("fifteen", 89), ("seconds", 93), ("per", 87), ("circular", 90),
        ("NPCI2024", 85), ("issued", 88), ("today", 91), ("morning", 90),
    ])
    passed, stats = evaluate_ocr_quality(words, confs)
    assert passed, stats


def test_gate_fails_on_word_count():
    words, confs = _tokens([("logo", 95), ("brand", 90)])
    passed, stats = evaluate_ocr_quality(words, confs)
    assert not passed and stats["fail"] == "word_count"


def test_gate_fails_on_mean_confidence():
    words, confs = _tokens([(f"word{i}", 40) for i in range(10)])
    passed, stats = evaluate_ocr_quality(words, confs)
    assert not passed and stats["fail"] == "mean_conf"


def test_gate_p10_catches_localized_garbage():
    """Healthy mean, but the weakest 10% of tokens are near-zero (watermark)."""
    good = [(f"clause{i}", 95) for i in range(18)]
    bad = [("~~", 5), ("###", 4)]
    words, confs = _tokens(good + bad)
    passed, stats = evaluate_ocr_quality(words, confs)
    assert not passed and stats["fail"] == "p10_conf"
    assert stats["mean_conf"] > 60  # the mean alone would have passed


def test_gate_fails_on_symbol_soup():
    words, confs = _tokens([(">>", 80), ("[|]", 82), ("-->", 79), ("==", 85),
                            ("::", 80), ("**", 82), ("||", 81), ("##", 83)])
    passed, stats = evaluate_ocr_quality(words, confs)
    assert not passed
    assert stats["fail"] in {"letter_ratio", "garbage_ratio"}


def test_gate_ignores_layout_artifacts():
    """Tesseract's conf=-1 rows (whitespace/layout) must not count as words."""
    words = ["", " ", "real", "words", "in", "the", "actual", "content", "here", "now"]
    confs = [-1, -1, 90, 88, 91, 87, 92, 90, 89, 91]
    passed, stats = evaluate_ocr_quality(words, confs)
    assert stats["word_count"] == 8
    assert passed


def test_garbage_token_battery():
    assert _is_garbage_token("x" * 21)          # too long
    assert _is_garbage_token("ttt")             # repeated run
    assert _is_garbage_token("aeiou")           # vowel run
    assert _is_garbage_token("xkvcmplr")        # consonant run
    assert _is_garbage_token(">>>")             # no alphanumerics — wait, repeats
    assert _is_garbage_token("[|]")             # no alphanumerics
    assert not _is_garbage_token("settlement")
    assert not _is_garbage_token("MTI0200")
    assert not _is_garbage_token("15s")


# ---------------------------------------------------------------------------
# PDF ingest — hybrid image routing (Tesseract gate → Gemini fallback)
# ---------------------------------------------------------------------------

def test_ingest_gate_pass_stays_local():
    from rag.ingest import _hybrid_image_text

    with patch("rag.ingest.tesseract_extract", return_value=("settlement text", {"mean_conf": 91})), \
         patch("rag.ingest._gemini_describe_png") as gemini:
        text, used_gemini = _hybrid_image_text(None, b"png-bytes", page_num=1)

    assert text == "settlement text"
    assert used_gemini is False
    gemini.assert_not_called()


def test_ingest_gate_failure_falls_back_to_gemini():
    from rag.ingest import _hybrid_image_text

    with patch("rag.ingest.tesseract_extract", return_value=(None, {"fail": "garbage_ratio"})), \
         patch("rag.ingest._gemini_describe_png", return_value="a flowchart of dispute states") as gemini:
        text, used_gemini = _hybrid_image_text(None, b"png-bytes", page_num=2)

    assert text == "a flowchart of dispute states"
    assert used_gemini is True
    gemini.assert_called_once()


def test_ingest_tiny_images_skip_gemini_entirely():
    """Logos/icons are not worth a vision API call even when OCR declines them."""
    from rag.ingest import _hybrid_image_text

    with patch("rag.ingest.tesseract_extract", return_value=(None, {"reason": "image_too_small"})), \
         patch("rag.ingest._gemini_describe_png") as gemini:
        text, used_gemini = _hybrid_image_text(None, b"png-bytes", page_num=3)

    assert text == ""
    assert used_gemini is False
    gemini.assert_not_called()


def test_ingest_tesseract_unavailable_preserves_gemini_path():
    """Without pytesseract installed, behavior degrades to the original Vision path."""
    from rag.ingest import _hybrid_image_text

    with patch("rag.ingest.tesseract_extract", return_value=(None, {"reason": "tesseract_unavailable"})), \
         patch("rag.ingest._gemini_describe_png", return_value="described") as gemini:
        text, used_gemini = _hybrid_image_text(None, b"png-bytes", page_num=4)

    assert text == "described"
    assert used_gemini is True
    gemini.assert_called_once()


# ---------------------------------------------------------------------------
# PDF ingest — structured table extraction
# ---------------------------------------------------------------------------

def _ruled_table_pdf() -> bytes:
    """A one-page PDF with a ruled 4x3 fee table PyMuPDF can detect."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Interchange Fee Schedule", fontsize=14)
    rows = [
        ["Category", "MDR %", "Cap"],
        ["UPI P2M", "0.30", "2000"],
        ["Debit Card", "0.90", "1000"],
        ["Credit Card", "1.80", "None"],
    ]
    x0, y0, cw, rh = 72, 90, 130, 24
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = fitz.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            page.draw_rect(rect, color=(0, 0, 0), width=0.7)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), val, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def test_ingest_reconstructs_table_as_markdown():
    """Tables survive extraction as aligned markdown, not flattened cell soup."""
    from rag.ingest import extract_text_from_pdf_bytes

    out = extract_text_from_pdf_bytes(_ruled_table_pdf(), client=None)

    assert "[Tables - Page 1]:" in out
    # A markdown row keeps the header aligned with its values in one line.
    assert "|UPIP2M|0.30|2000|" in out.replace(" ", "")


def test_ingest_table_extraction_can_be_disabled(monkeypatch):
    from rag.ingest import extract_text_from_pdf_bytes

    monkeypatch.setenv("A2A_INGEST_EXTRACT_TABLES", "false")
    out = extract_text_from_pdf_bytes(_ruled_table_pdf(), client=None)

    assert "[Tables - Page 1]:" not in out
    # Plain text extraction still runs.
    assert "Interchange Fee Schedule" in out
