from __future__ import annotations

import json
import re
import time

import pytest

from modelkeyguard.gateway import create_app

ADMIN_HEADERS = {"x-modelkeyguard-admin-secret": "dev-modelkeyguard-admin-secret"}


def _ts(seconds_ago: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")


def test_admin_usage_routes_and_filters(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    _write_jsonl(
        audit,
        [
            {
                "ts": _ts(60),
                "request_id": "r1",
                "decision": "ALLOWED",
                "reason": "allowed",
                "principal_id": "agent:doc-ingestor",
                "on_behalf_of_user_id": "user:alice",
                "token_id": "tok-1",
                "key_id": "key:openai:prod",
                "model": "gpt-4o-mini",
                "estimated_tokens": 123,
                "estimated_cost_usd": 0.01,
            },
            {
                "ts": _ts(30),
                "request_id": "r2",
                "decision": "BLOCKED",
                "reason": "user_quota_exceeded",
                "principal_id": "agent:other",
                "on_behalf_of_user_id": "user:bob",
                "token_id": "tok-2",
                "key_id": "key:openai:prod",
                "model": "gpt-4o-mini",
                "estimated_tokens": 10,
                "estimated_cost_usd": 0.001,
            },
        ],
    )
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    usage_page = client.get("/admin/usage", headers=ADMIN_HEADERS)
    assert usage_page.status_code == 200
    assert "Admin Links:" in usage_page.text
    assert 'href="/admin/keys"' in usage_page.text
    assert 'href="/admin/policy"' in usage_page.text
    assert 'href="/docs"' in usage_page.text
    assert "/static/admin_usage.css" in usage_page.text
    assert "/static/vendor/chart.umd.min.js" in usage_page.text
    assert "/static/admin_usage.js" in usage_page.text
    assert client.get("/static/admin_usage.css").status_code == 200
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert client.get("/static/admin_usage.js").status_code == 200
    data = client.get(
        "/admin/usage.json",
        params={"subject_type": "principal", "subject_id": "agent:doc-ingestor", "time_range": "24h", "bucket": "5m"},
        headers=ADMIN_HEADERS,
    ).json()
    assert data["overview"]["requests"] == 1
    assert data["overview"]["allowed"] == 1
    assert data["overview"]["denied"] == 0
    assert isinstance(data["charts"]["time_series"], list)
    assert isinstance(data["drilldown"]["events"], list)


def test_admin_keys_page_serves_template_and_css(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    page = client.get("/admin/keys", headers=ADMIN_HEADERS)
    assert page.status_code == 200
    assert "Admin Links:" in page.text
    assert 'href="/admin/policy"' in page.text
    assert 'href="/admin/usage"' in page.text
    assert 'href="/openapi.json"' in page.text
    assert "/static/admin_keys.css" in page.text
    assert "ModelKeyGuard Key Management" in page.text
    assert "name=\"intended_use\"" in page.text
    assert client.get("/static/admin_keys.css").status_code == 200


def test_admin_policy_page_forms_and_one_time_token_reveal(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    page = client.get("/admin/policy", headers=ADMIN_HEADERS)
    assert page.status_code == 200
    assert "Policy Operations" in page.text
    assert "Issue Safe Token" in page.text
    assert "Principal ID" in page.text
    assert "Lane" in page.text
    assert "On Behalf Of User ID" in page.text
    assert "Required. One of:" in page.text
    assert "/static/admin_policy.css" in page.text
    assert client.get("/static/admin_policy.css").status_code == 200

    principal_form = client.post(
        "/admin/policy",
        headers=ADMIN_HEADERS,
        data={
            "action": "register_principal",
            "principal_id": "agent:policy-ui-demo",
            "kind": "agent",
            "groups": "app-dev",
            "namespace": "tenant:kogwistar",
        },
    )
    assert principal_form.status_code == 200
    assert "Principal registered: agent:policy-ui-demo" in principal_form.text

    token_form = client.post(
        "/admin/policy/tokens",
        headers=ADMIN_HEADERS | {"accept": "text/html"},
        data={
            "principal_id": "agent:policy-ui-demo",
            "namespace": "tenant:kogwistar",
            "scopes": "model.invoke",
        },
    )
    assert token_form.status_code == 200
    assert "One-Time Safe Token" in token_form.text
    match = re.search(r"kgw_sk_[A-Za-z0-9_\\-]+", token_form.text)
    assert match, token_form.text
    issued_token = match.group(0)

    fresh_page = client.get("/admin/policy", headers=ADMIN_HEADERS)
    assert fresh_page.status_code == 200
    assert issued_token not in fresh_page.text

    # New token should work immediately without gateway restart.
    response = client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {issued_token}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
                {"role": "user", "content": "Summarize this note."},
            ],
            "max_tokens": 32,
        },
    )
    assert response.status_code == 200

    audit_text = audit.read_text(encoding="utf-8")
    assert "ADMIN_SAFE_TOKEN_ISSUED" in audit_text
    assert issued_token not in audit_text


def test_admin_policy_token_api_returns_one_time_token_and_hash_only_audit(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    issued = client.post(
        "/admin/policy/tokens",
        headers=ADMIN_HEADERS,
        json={
            "principal_id": "agent:doc-ingestor",
            "namespace": "tenant:kogwistar",
            "on_behalf_of_user_id": "user:alice",
            "scopes": ["model.invoke"],
        },
    )
    assert issued.status_code == 200
    body = issued.json()
    assert body["ok"] is True
    assert body["one_time_reveal"] is True
    assert body["safe_token"].startswith("kgw_sk_")
    assert body["safe_token_hash"].startswith("sha256:")

def test_admin_review_run_endpoint_supports_scheduler_controls(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "review_out.jsonl"
    checkpoint = tmp_path / "review_checkpoint.json"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    _write_jsonl(
        audit,
        [
            {
                "ts": _ts(90),
                "request_id": "r1",
                "decision": "ALLOWED",
                "reason": "allowed",
                "principal_id": "agent:doc-ingestor",
                "model": "gpt-4o-mini",
            }
        ],
    )
    client = TestClient(create_app("config/gateway_policy.json"))

    first = client.post(
        "/admin/review/run",
        json={
            "run_llm_review": False,
            "sample_size": 100,
            "lookback_minutes": 60,
            "out_path": str(out),
            "checkpoint_path": str(checkpoint),
        },
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["events_seen"] == 1
    assert first_data["reviews"] == []
    assert out.exists()
    assert checkpoint.exists()

    second = client.post(
        "/admin/review/run",
        json={
            "run_llm_review": False,
            "sample_size": 100,
            "lookback_minutes": 60,
            "out_path": str(out),
            "checkpoint_path": str(checkpoint),
        },
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200
    assert second.json()["events_seen"] == 0


def test_admin_security_events_auth_allowlist_and_persist(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    security_log = tmp_path / "security_events.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("SECURITY_EVENT_SHARED_SECRET", "s3cr3t")
    monkeypatch.setenv("ADMIN_WATCH_USERS", "azureuser")
    monkeypatch.setenv("ADMIN_SECURITY_EVENT_LOG_PATH", str(security_log))
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    unauth = client.post("/admin/security-events", json={"username": "azureuser"})
    assert unauth.status_code == 401

    ignored = client.post(
        "/admin/security-events",
        headers={"x-modelkeyguard-security-secret": "s3cr3t"},
        json={"event_type": "admin_ssh_login", "username": "other", "host": "h1", "source_ip": "1.2.3.4", "auth_method": "publickey"},
    )
    assert ignored.status_code == 200
    assert ignored.json()["ignored"] is True

    accepted = client.post(
        "/admin/security-events",
        headers={"x-modelkeyguard-security-secret": "s3cr3t"},
        json={"event_type": "admin_ssh_login", "username": "azureuser", "host": "h1", "source_ip": "1.2.3.4", "auth_method": "publickey"},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert "notify_status" in body
    assert security_log.exists()
    assert any(node.kind == "admin_security_event" for node in app.state.guard.graph_state.nodes.values())


def test_admin_security_events_returns_503_when_secret_not_configured(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("SECURITY_EVENT_SHARED_SECRET", raising=False)
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    resp = client.post("/admin/security-events", json={"username": "azureuser"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 503


def test_all_admin_routes_require_authentication(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    assert client.get("/admin/keys").status_code == 401
    assert client.get("/admin/usage").status_code == 401
    assert client.get("/admin/history").status_code == 401
    assert client.get("/admin/history.json").status_code == 401
    assert client.get("/admin/history/config").status_code == 401
    assert client.get("/admin/policy/quotas.json").status_code == 401
    assert client.post("/admin/policy/quotas/upsert", json={}).status_code == 401
    assert client.post("/admin/policy/quotas/revoke", json={}).status_code == 401
    assert client.post("/admin/review/run", json={}).status_code == 401


def test_admin_policy_routes_expose_swagger_request_bodies(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    spec = client.get("/openapi.json").json()
    principal_post = spec["paths"]["/admin/policy/principals"]["post"]
    principal_body = principal_post["requestBody"]["content"]["application/json"]["schema"]
    assert principal_body["type"] == "object"
    assert "principal_id" in principal_body["properties"]

    quota_post = spec["paths"]["/admin/policy/quotas/upsert"]["post"]
    quota_body = quota_post["requestBody"]["content"]["application/json"]["schema"]
    assert "lane" in quota_body["properties"]
    assert "subject_id" in quota_body["properties"]
    assert "quota_name" in quota_body["properties"]


def test_admin_session_login_logout_and_cookie_access(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_API_SECRET", "test-admin-secret-abc")
    client = TestClient(create_app("config/gateway_policy.json"))

    bad = client.post("/admin/session", json={"secret": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/admin/session", json={"secret": "test-admin-secret-abc"})
    assert ok.status_code == 200
    assert client.get("/admin/usage").status_code == 200

    logout = client.delete("/admin/session")
    assert logout.status_code == 200
    assert client.get("/admin/usage").status_code == 401


def test_admin_policy_quota_upsert_and_revoke_are_append_only_and_effective(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    upsert = client.post(
        "/admin/policy/quotas/upsert",
        headers=ADMIN_HEADERS,
        json={
            "lane": "user",
            "subject_id": "user:alice",
            "quota_name": "tiny_admin",
            "period": "hour",
            "max_requests": 0,
        },
    )
    assert upsert.status_code == 200
    qid = upsert.json()["quota_policy_id"]
    assert ":rev:" in qid

    quotas = client.get(
        "/admin/policy/quotas.json",
        headers=ADMIN_HEADERS,
        params={"lane": "user", "subject_id": "user:alice"},
    ).json()["data"]
    assert any(q["quota_name"] == "tiny_admin" and not q["revoked"] for q in quotas)

    blocked = client.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer kgw_demo_doc_ingestor"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
                {"role": "user", "content": "quota test"},
            ],
            "max_tokens": 8,
        },
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["message"] == "user_quota_exceeded"

    revoked = client.post(
        "/admin/policy/quotas/revoke",
        headers=ADMIN_HEADERS,
        json={
            "lane": "user",
            "subject_id": "user:alice",
            "quota_name": "tiny_admin",
            "reason": "rollback test",
        },
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    projection = app.state.guard.graph_state.get_quota_policy_projection("user", "user:alice")
    assert projection is not None
    assert not any(item.get("quota_name") == "tiny_admin" for item in projection.get("items", []))

    allowed = client.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer kgw_demo_doc_ingestor"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
                {"role": "user", "content": "quota test"},
            ],
            "max_tokens": 8,
        },
    )
    assert allowed.status_code == 200
