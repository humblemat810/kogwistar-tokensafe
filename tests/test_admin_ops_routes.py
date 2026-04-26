from __future__ import annotations

import json
import time

import pytest

from modelkeyguard.gateway import create_app


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

    usage_page = client.get("/admin/usage")
    assert usage_page.status_code == 200
    assert "/static/admin_usage.css" in usage_page.text
    assert "/static/vendor/chart.umd.min.js" in usage_page.text
    assert "/static/admin_usage.js" in usage_page.text
    assert client.get("/static/admin_usage.css").status_code == 200
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert client.get("/static/admin_usage.js").status_code == 200
    data = client.get(
        "/admin/usage.json",
        params={"subject_type": "principal", "subject_id": "agent:doc-ingestor", "time_range": "24h", "bucket": "hour"},
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

    page = client.get("/admin/keys")
    assert page.status_code == 200
    assert "/static/admin_keys.css" in page.text
    assert "ModelKeyGuard Key Management" in page.text
    assert client.get("/static/admin_keys.css").status_code == 200


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
    resp = client.post("/admin/security-events", json={"username": "azureuser"})
    assert resp.status_code == 503
