"""DB-free unit tests for portal backend logic (settings resolver, dashboard buckets)."""

import importlib

import services.settings_store as settings_store


def test_settings_resolver_env_fallback(monkeypatch):
    # No DB row → falls back to the mapped env var.
    monkeypatch.setenv("NPCI_AGENT_URL", "https://npci.example/a2a")
    settings_store.invalidate_cache()
    assert settings_store.get_setting("NPCI_AGENT_URL") == "https://npci.example/a2a"


def test_settings_resolver_default_when_unset(monkeypatch):
    monkeypatch.delenv("CODE_REPO_URL", raising=False)
    settings_store.invalidate_cache()
    assert settings_store.get_setting("CODE_REPO_URL", "n/a") == "n/a"


def test_get_grouped_masks_secrets(monkeypatch):
    monkeypatch.setenv("NPCI_API_KEY", "super-secret-key")
    settings_store.invalidate_cache()
    grouped = settings_store.get_grouped(mask_secrets=True)
    cats = {c["key"]: c for c in grouped["categories"]}
    npci = {f["key"]: f for f in cats["npci_connection"]["fields"]}
    assert npci["NPCI_API_KEY"]["secret"] is True
    assert npci["NPCI_API_KEY"]["value"] == "********"  # never leaks plaintext
    assert npci["NPCI_API_KEY"]["set"] is True
    # required-credential rollup drives the "N of 5 configured" header
    assert grouped["required_total"] == 5
    assert isinstance(grouped["required_configured"], int)


def test_settings_schema_categories():
    cats = set(settings_store.SETTINGS_SCHEMA.keys())
    assert cats == {"npci_connection", "partner_profile", "ai_drafting", "a2a_security", "code_repository"}


def test_change_bucket_mapping():
    admin_routes = importlib.import_module("routes.admin_routes")
    assert admin_routes._change_bucket("COMMUNICATED") == "new"
    assert admin_routes._change_bucket("RECEIVED") == "new"
    assert admin_routes._change_bucket("ACCEPTED") == "in_progress"
    assert admin_routes._change_bucket("CERTIFYING") == "in_progress"
    assert admin_routes._change_bucket("CERTIFIED") == "certified"
    assert admin_routes._change_bucket("ABORTED") == "other"
