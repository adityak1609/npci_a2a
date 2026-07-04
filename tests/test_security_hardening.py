import os
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from a2a_models import A2AEnvelope
from a2a_models import A2ATaskType
from agent.external_dispatcher import (
    EXTERNAL_HANDLERS,
    NotImplementedTaskError,
    _not_implemented_handler,
)
from a2a_auth import (
    A2AAuthContext,
    A2AAuthError,
    check_credential_binding,
    verify_hmac,
)
from compliance import ComplianceBlockedError, require_external_ai_approval
from file_validation import FileValidationError, decode_base64_limited, validate_pdf_bytes
from rate_limit import check_rate_limit
from auth import create_access_token, decode_token


def test_external_ai_processing_fails_closed_by_default(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AI_DATA_PROCESSING_APPROVED", raising=False)
    with pytest.raises(ComplianceBlockedError):
        require_external_ai_approval("test")


def test_external_ai_processing_can_be_explicitly_approved(monkeypatch):
    monkeypatch.setenv("EXTERNAL_AI_DATA_PROCESSING_APPROVED", "true")
    require_external_ai_approval("test")


def test_access_tokens_include_revocable_session_id():
    token = create_access_token({"sub": "admin@example.com"})
    payload = decode_token(token)

    assert payload["sub"] == "admin@example.com"
    assert payload["jti"]
    assert len(payload["jti"]) >= 32


def test_base64_validation_rejects_malformed_content():
    with pytest.raises(FileValidationError):
        decode_base64_limited("not valid base64", max_bytes=1024)


def test_pdf_validation_rejects_non_pdf_bytes():
    with pytest.raises(FileValidationError):
        validate_pdf_bytes(b"hello", filename="policy.pdf", max_bytes=1024)


def test_rate_limiter_rejects_after_limit():
    scope = f"test-{os.urandom(4).hex()}"
    check_rate_limit(scope, "user", limit=1, window_seconds=60)
    with pytest.raises(HTTPException) as exc:
        check_rate_limit(scope, "user", limit=1, window_seconds=60)
    assert exc.value.status_code == 429


def test_unimplemented_npci_task_is_rejected_not_acknowledged():
    envelope = A2AEnvelope(
        task_type="cert_config_request",
        cflow_id="CFLOW-1",
        **{
            "from": "npci_central",
            "agent_id": "npci_central",
            "agent_run_id": "run-1",
            "correlation_id": "corr-1",
            "timestamp": "2026-06-29T00:00:00Z",
            "payload": {},
            "message_id": "msg-1",
        },
    )
    with pytest.raises(NotImplementedTaskError):
        asyncio.run(_not_implemented_handler(envelope, None, A2AAuthContext()))


def test_external_dispatcher_has_explicit_handler_for_every_task_type():
    missing = sorted(task.value for task in A2ATaskType if task.value not in EXTERNAL_HANDLERS)
    assert missing == []


class _FakeQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def delete(self, *_args, **_kwargs):
        return 0

    def first(self):
        return None


class _FakeDb:
    def __init__(self):
        self.added = 0
        self.commits = 0

    def query(self, *_args, **_kwargs):
        return _FakeQuery()

    def add(self, *_args, **_kwargs):
        self.added += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_invalid_hmac_does_not_store_nonce(monkeypatch):
    monkeypatch.setenv("A2A_HMAC_SECRET", "h" * 64)
    monkeypatch.setenv("A2A_NONCE_TTL_SECONDS", "600")
    raw_body = b'{"jsonrpc":"2.0","id":"1"}'
    request = SimpleNamespace(headers={
        "X-NPCI-Timestamp": str(int(time.time())),
        "X-NPCI-Nonce": "nonce-1234567890",
        "X-NPCI-Signature": "0" * 64,
    })
    db = _FakeDb()

    with pytest.raises(A2AAuthError):
        verify_hmac(request, raw_body, db)

    assert db.added == 0
    assert db.commits == 0


def test_credential_binding_rejects_partner_impersonation(monkeypatch):
    monkeypatch.setenv("A2A_JWT_PARTNER_MAP", "npci-token=npci_central")
    envelope = A2AEnvelope(
        task_type="echo",
        **{
            "from": "other_partner",
            "agent_id": "other_partner",
            "agent_run_id": "run-1",
            "correlation_id": "corr-1",
            "timestamp": "2026-06-29T00:00:00Z",
            "payload": {},
            "message_id": "msg-2",
        },
    )
    request = SimpleNamespace(headers={})

    with pytest.raises(A2AAuthError):
        check_credential_binding(request, envelope, "npci-token")


def test_credential_binding_accepts_normalized_mtls_fingerprint(monkeypatch):
    monkeypatch.delenv("A2A_JWT_PARTNER_MAP", raising=False)
    monkeypatch.setenv("A2A_MTLS_PARTNER_MAP", "AA:BB:CC=npci_central")
    envelope = A2AEnvelope(
        task_type="echo",
        **{
            "from": "npci_central",
            "agent_id": "npci_central",
            "agent_run_id": "run-1",
            "correlation_id": "corr-1",
            "timestamp": "2026-06-29T00:00:00Z",
            "payload": {},
            "message_id": "msg-3",
        },
    )
    request = SimpleNamespace(headers={"X-SSL-Client-Fingerprint": "aabbcc"})

    check_credential_binding(request, envelope, None)


def test_change_communication_validates_all_documents_before_storage(monkeypatch):
    from services import a2a_ingestor

    def fake_decode(value, *, max_bytes):
        if value == "bad":
            raise FileValidationError("bad file")
        return b"pdf"

    def fake_validate(*_args, **_kwargs):
        return None

    def fail_if_called(_prepared):
        raise AssertionError("storage should not run after validation failure")

    monkeypatch.setattr(a2a_ingestor, "decode_base64_limited", fake_decode)
    monkeypatch.setattr(a2a_ingestor, "validate_ingest_bytes", fake_validate)
    monkeypatch.setattr(a2a_ingestor, "_store_prepared_change_documents", fail_if_called)

    envelope = SimpleNamespace(payload={
        "change_id": "CHG-1",
        "version": 1,
        "product_kit": [
            {"doc_type": "brd", "filename": "one.pdf", "content_b64": "ok"},
            {"doc_type": "brd", "filename": "two.pdf", "content_b64": "bad"},
        ],
    })

    with pytest.raises(ValueError, match="bad file"):
        asyncio.run(a2a_ingestor.handle_change_communication_ingest(envelope, None))
