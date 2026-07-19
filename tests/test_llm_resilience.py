"""Tests for LLM model-fallback configuration and CSP nonce injection."""

import importlib

from core import config


def test_gemini_models_includes_primary_and_fallback(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "primary-model")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "fb-1, fb-2")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.gemini_models() == ["primary-model", "fb-1", "fb-2"]
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_gemini_models_dedupes_and_drops_blanks(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "m1")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "m1, , m2")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.gemini_models() == ["m1", "m2"]
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_is_retryable_llm_error():
    assert config.is_retryable_llm_error("503 UNAVAILABLE")
    assert config.is_retryable_llm_error("429 RESOURCE_EXHAUSTED")
    assert config.is_retryable_llm_error("ReadTimeout while streaming")
    assert not config.is_retryable_llm_error("400 INVALID_ARGUMENT")
    assert not config.is_retryable_llm_error("permission denied")


def test_csp_nonce_injected_into_all_tag_forms():
    from main import _inject_csp_nonce

    html = (
        "<style>a{}</style>"
        '<script type="module" src="a.js"></script>'
        "<SCRIPT>x</SCRIPT>"
        '<script nonce="keep">y</script>'
        "<scripting>notatag</scripting>"
    )
    out = _inject_csp_nonce(html, "N0NCE")
    assert '<style nonce="N0NCE">' in out
    assert '<script nonce="N0NCE" type="module" src="a.js">' in out
    assert '<SCRIPT nonce="N0NCE">' in out
    # Already-nonced tag is left untouched (no double injection).
    assert out.count('nonce="keep"') == 1
    assert 'nonce="N0NCE" nonce="keep"' not in out
    # Non-tag token is not rewritten.
    assert "<scripting>notatag</scripting>" in out


def test_csp_nonce_noop_without_nonce():
    from main import _inject_csp_nonce

    html = "<script>x</script>"
    assert _inject_csp_nonce(html, "") == html
