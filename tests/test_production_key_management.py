from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from modelkeyguard.gateway import append_audit, create_app, process_chat_completion
from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.key_manager import KeyLifecycleError, KeyManager
from modelkeyguard.settings import AppSettings, read_env_or_file
from modelkeyguard.token_auth import TokenVerifier

EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


@pytest.fixture()
def prod_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-graph-key-32-bytes-minimum-abcdef")
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ENV", "local")
    monkeypatch.setenv("MODELKEYGUARD_AUTH_MODE", "local")
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    return tmp_path


def test_read_env_or_file_prefers_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET", "from-env")
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))
    assert read_env_or_file("EXAMPLE_SECRET") == "from-file"


def test_settings_production_requires_real_graph_key(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_ENV", "production")
    monkeypatch.delenv("MODELKEYGUARD_GRAPH_KEY", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_GRAPH_KEY_FILE", raising=False)
    settings = AppSettings.from_env()
    assert any("GRAPH_KEY" in e for e in settings.validate_for_startup())


def test_settings_accepts_graph_key_file(monkeypatch, tmp_path):
    f = tmp_path / "graph_key"
    f.write_text("x" * 40, encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_ENV", "local")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY_FILE", str(f))
    assert AppSettings.from_env().graph_key == "x" * 40


def test_key_create_stores_no_plaintext(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    km.create_key(key_id="key:test:one", provider="openai", models=["gpt-test"], display_name="Test", provider_secret="sk-super-secret", created_by="tester")
    assert "sk-super-secret" not in (prod_env / "graph.jsonl").read_text(encoding="utf-8")


def test_key_create_returns_safe_view(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    view = KeyManager(store, store.app_key).create_key(key_id="key:test:view", provider="openai", models=["m"], display_name="View", provider_secret="secret", created_by="tester")
    assert view.active_secret_ref and view.active_secret_ref.startswith("secret:key:test:view")
    assert not hasattr(view, "provider_secret")


def test_key_resolve_decrypts_only_inside_manager(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    view = km.create_key(key_id="key:test:resolve", provider="openai", models=["m"], display_name="Resolve", provider_secret="sk-live", created_by="tester")
    assert km.resolve_provider_secret(view.active_secret_ref) == "sk-live"


def test_key_wrong_graph_key_cannot_decrypt(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    view = km.create_key(key_id="key:test:wrong", provider="openai", models=["m"], display_name="Wrong", provider_secret="secret", created_by="tester")
    store2 = GraphStateStore(path=prod_env / "graph.jsonl", app_key="different-graph-key-32-bytes-minimum")
    with pytest.raises(Exception):
        KeyManager(store2, store2.app_key).resolve_provider_secret(view.active_secret_ref)


def test_key_rotate_changes_active_secret_ref(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    v1 = km.create_key(key_id="key:test:rotate", provider="openai", models=["m"], display_name="Rotate", provider_secret="old", created_by="tester")
    v2 = km.rotate_key(key_id="key:test:rotate", provider_secret="new", rotated_by="tester")
    assert v1.active_secret_ref != v2.active_secret_ref
    assert km.resolve_provider_secret(v2.active_secret_ref) == "new"


def test_rotated_old_secret_is_not_active(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    v1 = km.create_key(key_id="key:test:old", provider="openai", models=["m"], display_name="Old", provider_secret="old", created_by="tester")
    km.rotate_key(key_id="key:test:old", provider_secret="new", rotated_by="tester")
    with pytest.raises(KeyLifecycleError):
        km.resolve_provider_secret(v1.active_secret_ref)


def test_key_revoke_marks_key_revoked(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    km.create_key(key_id="key:test:revoke", provider="openai", models=["m"], display_name="Revoke", provider_secret="secret", created_by="tester")
    view = km.revoke_key(key_id="key:test:revoke", revoked_by="tester", reason="leak")
    assert view.status == "revoked"


def test_revoked_secret_cannot_resolve(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    view = km.create_key(key_id="key:test:revoked-secret", provider="openai", models=["m"], display_name="Revoke", provider_secret="secret", created_by="tester")
    km.revoke_key(key_id="key:test:revoked-secret", revoked_by="tester")
    with pytest.raises(KeyLifecycleError):
        km.resolve_provider_secret(view.active_secret_ref)


def test_expired_secret_cannot_resolve(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    view = km.create_key(key_id="key:test:expired", provider="openai", models=["m"], display_name="Expired", provider_secret="secret", created_by="tester", expires_at_epoch=int(time.time()) - 1)
    with pytest.raises(KeyLifecycleError):
        km.resolve_provider_secret(view.active_secret_ref)


def test_create_duplicate_key_rejected(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    km.create_key(key_id="key:test:dup", provider="openai", models=["m"], display_name="Dup", provider_secret="secret", created_by="tester")
    with pytest.raises(KeyLifecycleError):
        km.create_key(key_id="key:test:dup", provider="openai", models=["m"], display_name="Dup", provider_secret="secret", created_by="tester")


def test_create_requires_key_prefix(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    with pytest.raises(KeyLifecycleError):
        KeyManager(store, store.app_key).create_key(key_id="bad", provider="openai", models=["m"], display_name="Bad", provider_secret="secret", created_by="tester")


def test_create_requires_secret(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    with pytest.raises(KeyLifecycleError):
        KeyManager(store, store.app_key).create_key(key_id="key:test:nosecret", provider="openai", models=["m"], display_name="Bad", provider_secret="", created_by="tester")


def test_events_record_key_lifecycle_without_secret(prod_env):
    store = GraphStateStore(path=prod_env / "graph.jsonl", app_key="test-graph-key-32-bytes-minimum-abcdef")
    km = KeyManager(store, store.app_key)
    km.create_key(key_id="key:test:events", provider="openai", models=["m"], display_name="Events", provider_secret="dont-log-me", created_by="tester")
    kinds = [e["kind"] for e in store.events]
    assert "MODEL_KEY_CREATED" in kinds
    assert "dont-log-me" not in (prod_env / "graph.jsonl").read_text(encoding="utf-8")


def test_append_audit_filters_secret_fields(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr("modelkeyguard.gateway.AUDIT_PATH", audit)
    append_audit({"event": "x", "provider_secret": "sk-nope", "secret_ref": "secret:x", "safe": "ok"})
    text = audit.read_text(encoding="utf-8")
    assert "sk-nope" not in text
    assert "secret_ref" not in text
    assert "ok" in text


def test_admin_page_uses_password_inputs(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    html = TestClient(app).get("/admin/keys").text
    assert "type='password'" in html
    assert "provider key" in html


def test_admin_create_key_does_not_return_secret(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/admin/keys", data={"key_id": "key:test:admin", "provider": "openai", "models": "gpt-managed", "display_name": "Admin", "provider_secret": "sk-admin-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["secret_value"] is None
    assert "sk-admin-secret" not in r.text


def test_admin_keys_json_never_returns_secret(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:list", "provider": "openai", "models": "gpt-list", "display_name": "List", "provider_secret": "sk-list-secret"})
    r = c.get("/admin/keys.json")
    assert r.status_code == 200
    assert "sk-list-secret" not in r.text
    assert "active_secret_ref" in r.text


def test_admin_rotate_key_endpoint_changes_ref(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    a = c.post("/admin/keys", data={"key_id": "key:test:http-rotate", "provider": "openai", "models": "gpt-rot", "display_name": "Rot", "provider_secret": "old"}).json()["secret_ref"]
    b = c.post("/admin/keys/key:test:http-rotate/rotate", data={"provider_secret": "new"}).json()["secret_ref"]
    assert a != b


def test_admin_revoke_key_endpoint(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:http-revoke", "provider": "openai", "models": "gpt-rev", "display_name": "Rev", "provider_secret": "secret"})
    r = c.post("/admin/keys/key:test:http-revoke/revoke", data={"reason": "test"})
    assert r.json()["status"] == "revoked"


def test_managed_key_is_listed_as_openai_compatible_model(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:model-list", "provider": "openai", "models": "gpt-managed-list", "display_name": "Managed", "provider_secret": "secret"})
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "gpt-managed-list" in ids


def test_managed_key_can_serve_openai_compatible_request(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:serve", "provider": "openai", "models": "gpt-managed-serve", "display_name": "Managed", "provider_secret": "sk-serve"})
    r = c.post("/v1/chat/completions", headers={"authorization": "Bearer kgw_demo_doc_ingestor"}, json={"model": "gpt-managed-serve", "messages": [{"role": "system", "content": EXPECTED_SYSTEM}, {"role": "user", "content": "hi"}], "max_tokens": 5})
    assert r.status_code == 200
    assert r.json()["modelkeyguard"]["decision"] == "ALLOWED"


def test_revoked_managed_key_no_longer_serves(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:serve-rev", "provider": "openai", "models": "gpt-managed-rev", "display_name": "Managed", "provider_secret": "sk-serve"})
    c.post("/admin/keys/key:test:serve-rev/revoke", data={"reason": "x"})
    r = c.post("/v1/chat/completions", headers={"authorization": "Bearer kgw_demo_doc_ingestor"}, json={"model": "gpt-managed-rev", "messages": [{"role": "system", "content": EXPECTED_SYSTEM}], "max_tokens": 5})
    assert r.status_code == 403


def test_invalid_admin_create_returns_400(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/admin/keys", data={"key_id": "bad", "provider": "openai", "models": "m", "provider_secret": "x"})
    assert r.status_code == 400


def test_local_langchain_style_fields_work(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/v1/chat/completions", headers={"authorization": "Bearer kgw_demo_doc_ingestor"}, json={"model": "gpt-5.3-mini", "messages": [{"role": "system", "content": EXPECTED_SYSTEM}, {"role": "user", "content": "hello"}], "max_tokens": 8})
    assert r.status_code == 200
    assert r.json()["object"] == "chat.completion"


def test_missing_bearer_logs_auth_denied(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/v1/chat/completions", json={"model": "gpt-5.3-mini", "messages": []})
    assert r.status_code == 401
    assert any(e["kind"] == "AUTH_TOKEN_DENIED" for e in app.state.guard.graph_state.events)


def test_secret_resolution_denied_for_expired_managed_key(prod_env):
    app = create_app("config/gateway_policy.json")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/admin/keys", data={"key_id": "key:test:expired-http", "provider": "openai", "models": "gpt-expired-http", "display_name": "Expired", "provider_secret": "secret", "expires_at_epoch": str(int(time.time()) - 1)})
    r = c.post("/v1/chat/completions", headers={"authorization": "Bearer kgw_demo_doc_ingestor"}, json={"model": "gpt-expired-http", "messages": [{"role": "system", "content": EXPECTED_SYSTEM}], "max_tokens": 5})
    assert r.status_code == 403
    assert "expired" in r.text
