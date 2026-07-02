import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


def _set_valid_security_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("A2A_HMAC_SECRET", "h" * 64)
    monkeypatch.setenv("NPCI_API_KEY", "n" * 32)
    monkeypatch.setenv("A2A_MTLS_REQUIRED", "true")
    monkeypatch.setenv("A2A_JWT_REQUIRED_EXTERNAL", "true")
    monkeypatch.setenv("A2A_NONCE_TTL_SECONDS", "600")


def test_security_config_validation_accepts_real_secrets(monkeypatch):
    _set_valid_security_env(monkeypatch)
    main._validate_security_config()


def test_security_config_validation_rejects_placeholders(monkeypatch):
    _set_valid_security_env(monkeypatch)
    monkeypatch.setenv("A2A_HMAC_SECRET", "<shared-secret-with-npci>")

    with pytest.raises(EnvironmentError) as exc:
        main._validate_security_config()

    assert "A2A_HMAC_SECRET" in str(exc.value)


def test_production_security_config_requires_deployment_controls(monkeypatch):
    _set_valid_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("A2A_INGESTION_MODE", "inline")
    monkeypatch.delenv("A2A_CIDR_ALLOWLIST", raising=False)
    monkeypatch.delenv("A2A_MTLS_FINGERPRINTS", raising=False)

    with pytest.raises(EnvironmentError) as exc:
        main._validate_security_config()

    detail = str(exc.value)
    assert "AUTH_COOKIE_SECURE" in detail
    assert "REDIS_URL" in detail
    assert "A2A_INGESTION_MODE" in detail
    assert "A2A_CIDR_ALLOWLIST" in detail
    assert "A2A_MTLS_FINGERPRINTS" in detail
    assert "NPCI_CLIENT_CERT_FILE" in detail
    assert "NPCI_CLIENT_KEY_FILE" in detail


def test_production_security_config_rejects_wildcard_cors(monkeypatch):
    _set_valid_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.internal:6379/0")
    monkeypatch.setenv("A2A_INGESTION_MODE", "worker")
    monkeypatch.setenv("A2A_CIDR_ALLOWLIST", "203.0.113.0/24")
    monkeypatch.setenv("A2A_MTLS_FINGERPRINTS", "aabbcc")
    monkeypatch.setenv("A2A_MTLS_PARTNER_MAP", "aabbcc=npci_central")
    monkeypatch.setenv("A2A_JWT_PARTNER_MAP", "npci-token=npci_central")
    monkeypatch.setenv("A2A_KNOWN_AGENTS", "npci_central")
    monkeypatch.setenv("NPCI_AGENT_URL", "https://npci.example.internal/a2a")
    monkeypatch.setenv("NPCI_CLIENT_CERT_FILE", "/run/secrets/npci_client_cert")
    monkeypatch.setenv("NPCI_CLIENT_KEY_FILE", "/run/secrets/npci_client_key")
    monkeypatch.setenv("CORS_ORIGINS", "*")

    with pytest.raises(EnvironmentError) as exc:
        main._validate_security_config()

    assert "CORS_ORIGINS" in str(exc.value)


def test_stream_chat_enforces_rate_limit(monkeypatch):
    def fake_rate_limit(user_key):
        raise HTTPException(status_code=429, detail=f"limited:{user_key}")

    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "check_chat_rate_limit", fake_rate_limit)

    req = chat_routes.ChatRequest(question="What is the leave policy?", session_id="s1")
    user = SimpleNamespace(id=123, is_admin=True, visibility_tier="admin")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_routes.chat_endpoint(req, current_user=user, db=None))

    assert exc.value.status_code == 429
    assert "123" in exc.value.detail


def test_app_startup_and_security_headers_without_live_services(monkeypatch):
    _set_valid_security_env(monkeypatch)

    async def fake_recover_orphaned_jobs():
        return 0

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "_cleanup_expired_security_rows", lambda: None)

    monkeypatch.setattr(
        "services.a2a_ingestor.recover_orphaned_jobs",
        fake_recover_orphaned_jobs,
    )

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "nonce-" in csp


def test_metrics_requires_authentication(monkeypatch):
    _set_valid_security_env(monkeypatch)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "_cleanup_expired_security_rows", lambda: None)

    monkeypatch.setattr(
        "services.a2a_ingestor.recover_orphaned_jobs",
        lambda: asyncio.sleep(0, result=0),
    )

    with TestClient(main.app) as client:
        response = client.get("/metrics")

    assert response.status_code == 401


def test_chat_rejects_oversized_body_before_auth(monkeypatch):
    _set_valid_security_env(monkeypatch)
    monkeypatch.setenv("CHAT_MAX_REQUEST_BYTES", "16")
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "_cleanup_expired_security_rows", lambda: None)

    monkeypatch.setattr(
        "services.a2a_ingestor.recover_orphaned_jobs",
        lambda: asyncio.sleep(0, result=0),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/chat",
            content='{"question":"short","padding":"' + ("x" * 100) + '"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
