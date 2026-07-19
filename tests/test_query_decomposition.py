"""Unit tests for query decomposition and multi-query RRF fusion.

Covers the pure parts — the multi-part heuristic, defensive JSON parsing,
graceful fallback on model failure, and parent-level fusion across
sub-queries — without needing Postgres or Gemini.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from rag.decompose import (
    _parse_subqueries,
    decompose_query,
    looks_multi_part,
)

# ---------------------------------------------------------------------------
# Multi-part heuristic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What is the FPS outward timeout and which response code applies on expiry?",
    "What is RC 05? What is RC 91?",
    "Explain the difference between outward and inward settlement.",
    "Dispute TAT for UPI vs IMPS?",
    "How are chargebacks raised; how are they closed?",
])
def test_multi_part_questions_trigger(question):
    assert looks_multi_part(question)


@pytest.mark.parametrize("question", [
    "What is the timeout for an FPS outward transaction?",
    "Summarize the terms and conditions for merchant onboarding.",
    "What does response code 05 mean?",
])
def test_single_part_questions_skip_the_llm(question):
    assert not looks_multi_part(question)


# ---------------------------------------------------------------------------
# Defensive JSON parsing
# ---------------------------------------------------------------------------

def test_parse_plain_json_array():
    assert _parse_subqueries('["query one", "query two"]') == ["query one", "query two"]


def test_parse_strips_markdown_fences():
    raw = '```json\n["FPS outward timeout", "response code on expiry"]\n```'
    assert _parse_subqueries(raw) == ["FPS outward timeout", "response code on expiry"]


def test_parse_clamps_and_dedupes():
    raw = '["a", "a", "b", "c", "d", "e", "f"]'
    assert _parse_subqueries(raw) == ["a", "b", "c", "d"]


@pytest.mark.parametrize("raw", [
    "", "not json", '{"query": "x"}', "[1, 2, 3]", '["   "]',
])
def test_parse_rejects_unusable_output(raw):
    assert _parse_subqueries(raw) == []


# ---------------------------------------------------------------------------
# decompose_query — fallback behavior
# ---------------------------------------------------------------------------

_COMPOUND = "What is the FPS outward timeout and which response code applies on expiry?"


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=response_text)
    return client


def test_decompose_splits_compound_question():
    with patch("rag.decompose._get_client", return_value=_mock_client(
        '["FPS outward transaction timeout", "FPS response code on timeout expiry"]'
    )), patch("security.compliance.require_external_ai_approval"):
        subqueries = decompose_query(_COMPOUND)
    assert len(subqueries) == 2


def test_decompose_falls_back_on_model_error():
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("503 UNAVAILABLE")
    with patch("rag.decompose._get_client", return_value=client), \
         patch("security.compliance.require_external_ai_approval"):
        assert decompose_query(_COMPOUND) == [_COMPOUND]


def test_decompose_falls_back_on_unparseable_output():
    with patch("rag.decompose._get_client", return_value=_mock_client("Sure! Here are…")), \
         patch("security.compliance.require_external_ai_approval"):
        assert decompose_query(_COMPOUND) == [_COMPOUND]


def test_decompose_skips_simple_questions_without_llm_call():
    with patch("rag.decompose._get_client") as get_client:
        result = decompose_query("What does response code 05 mean?")
    assert result == ["What does response code 05 mean?"]
    get_client.assert_not_called()


def test_decompose_respects_kill_switch():
    with patch.dict(os.environ, {"A2A_QUERY_DECOMPOSITION": "false"}), \
         patch("rag.decompose._get_client") as get_client:
        assert decompose_query(_COMPOUND) == [_COMPOUND]
    get_client.assert_not_called()


# ---------------------------------------------------------------------------
# retrieve_multi_with_scores — parent-level RRF fusion
# ---------------------------------------------------------------------------

def _parent(text: str, distance: float) -> dict:
    return {
        "text": text,
        "distance": distance,
        "change_id": None,
        "citations_json": None,
        "score": 0.9,
    }


def test_multi_query_fusion_rewards_consensus():
    from rag.retriever import retrieve_multi_with_scores

    per_query = {
        "q1": [_parent("shared", 0.30), _parent("only-q1", 0.20)],
        "q2": [_parent("only-q2", 0.25), _parent("shared", 0.35)],
    }
    with patch(
        "rag.retriever._hybrid_retrieve",
        side_effect=lambda q, *a, **kw: per_query[q],
    ):
        rows = retrieve_multi_with_scores(["q1", "q2"], top_k=3)

    assert [r["text"] for r in rows][0] == "shared"  # rank1 + rank2 beats any single hit
    shared = rows[0]
    assert shared["score"] == pytest.approx(round(1 / 61 + 1 / 62, 4))
    # Best (lowest) dense distance across sub-queries is preserved.
    assert shared["distance"] == 0.30


def test_single_query_bypasses_fusion():
    from rag.retriever import retrieve_multi_with_scores

    sentinel = [_parent("only", 0.1)]
    with patch("rag.retriever._hybrid_retrieve", return_value=sentinel) as hybrid:
        rows = retrieve_multi_with_scores(["  one query  "], top_k=5)
    assert rows == sentinel
    hybrid.assert_called_once()


def test_empty_queries_return_no_rows():
    from rag.retriever import retrieve_multi_with_scores

    assert retrieve_multi_with_scores([]) == []
    assert retrieve_multi_with_scores(["", "   "]) == []
