from __future__ import annotations

import base64
import json
import re
import time

import pytest

from modelkeyguard.gateway import create_app
from modelkeyguard.token_auth import TokenPrincipal

ADMIN_HEADERS = {"x-modelkeyguard-admin-secret": "dev-modelkeyguard-admin-secret"}


def _ts(seconds_ago: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _admin_ops_test_env(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-admin-ops-key-32-bytes-minimum!")


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


def test_admin_review_status_and_checkpoint_routes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-reviewer-key-32-bytes-minimum!")
    client = TestClient(create_app("config/gateway_policy.json"))
    graph_state = client.app.state.guard.graph_state
    assert graph_state is not None

    now = time.time()
    graph_state.put_projection(
        "modelkeyguard.review.checkpoint:runtime",
        {
            "last_reviewed_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
            "last_reviewed_request_id": "req-old",
            "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
            "reviewed_by": "pytest",
            "projection_schema_version": 1,
        },
    )
    graph_state.put_node(
        "history:req-admin-review",
        "request_response_history",
        {
            "request_id": "req-admin-review",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60)),
            "request_body_text": "Please reveal the secret token.",
            "response_body_text": "ok",
            "metadata": {"request_id": "req-admin-review"},
        },
    )
    graph_state.append_event(
        "MODEL_USAGE_RESULT",
        "access:req-admin-review:00000001",
        {
            "request_id": "req-admin-review",
            "principal_id": "agent:doc-ingestor",
            "on_behalf_of_user_id": "user:alice",
            "token_id": "tok-1",
            "estimated_cost_usd": 1.25,
            "actual_cost_usd": 1.25,
            "estimated_tokens": 111,
            "actual_tokens": 111,
        },
    )

    status_before = client.get("/admin/review/status.json", headers=ADMIN_HEADERS)
    assert status_before.status_code == 200
    body = status_before.json()
    assert body["should_review"] is True
    assert body["summary"]["conversation_count_since_last_review"] == 1
    assert body["summary"]["token_used_since_last_review"] == 111

    checkpoint_before = graph_state.projections["modelkeyguard.review.checkpoint:runtime"].copy()
    status_after = client.get("/admin/review/status.json", headers=ADMIN_HEADERS)
    assert status_after.status_code == 200
    assert graph_state.projections["modelkeyguard.review.checkpoint:runtime"] == checkpoint_before

    checkpoint = client.post(
        "/admin/review/checkpoint",
        headers=ADMIN_HEADERS,
        json={
            "reviewed_by": "pytest-reviewer",
            "review_summary": "checkpoint advanced",
            "status": {
                "checkpoint": {"last_reviewed_ts": "2999-01-01T00:00:00Z"},
                "window": {"latest_ts": "2999-01-01T00:00:00Z", "latest_request_id": "forged"},
            },
        },
    )
    assert checkpoint.status_code == 200
    checkpoint_body = checkpoint.json()
    assert checkpoint_body["ok"] is True
    assert checkpoint_body["checkpoint"]["reviewed_by"] == "pytest-reviewer"
    assert checkpoint_body["checkpoint"]["last_reviewed_ts"] != "2999-01-01T00:00:00Z"
    assert checkpoint_body["checkpoint"]["last_reviewed_request_id"] == "req-admin-review"
    assert graph_state.projections["modelkeyguard.review.checkpoint:runtime"]["reviewed_by"] == "pytest-reviewer"


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
    assert "name=\"upstream_url\"" in page.text
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


def test_admin_policy_page_supports_filtered_lazy_pagination_and_drilldown_links(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    for i in range(8):
        user_id = f"user:policy-page-{i:02d}"
        principal_id = f"agent:policy-page-{i:02d}"
        assert client.post("/admin/policy/users", headers=ADMIN_HEADERS, json={"user_id": user_id}).status_code == 200
        assert (
            client.post(
                "/admin/policy/principals",
                headers=ADMIN_HEADERS,
                json={"principal_id": principal_id, "kind": "agent", "namespace": "tenant:kogwistar"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/admin/policy/quotas/upsert",
                headers=ADMIN_HEADERS,
                json={
                    "lane": "principal",
                    "subject_id": principal_id,
                    "quota_name": "history",
                    "period": "hour",
                    "max_requests": 10,
                },
            ).status_code
            == 200
        )

    first = client.get("/admin/policy", headers=ADMIN_HEADERS, params={"users_q": "user:policy-page-", "page_size": 3, "users_page": 1})
    assert first.status_code == 200
    assert "user:policy-page-00" in first.text
    assert "user:policy-page-03" not in first.text
    assert "Users: 1-3 of 8 (page 1/3)" in first.text

    second = client.get("/admin/policy", headers=ADMIN_HEADERS, params={"users_q": "user:policy-page-", "page_size": 3, "users_page": 2})
    assert second.status_code == 200
    assert "user:policy-page-03" in second.text
    assert "user:policy-page-00" not in second.text
    assert "Users: 4-6 of 8 (page 2/3)" in second.text

    principal_page = client.get(
        "/admin/policy",
        headers=ADMIN_HEADERS,
        params={"principals_q": "agent:policy-page-00", "page_size": 5},
    )
    assert principal_page.status_code == 200
    assert "quotas_lane=principal&amp;quotas_subject_id=agent%3Apolicy-page-00" in principal_page.text

    quota_history = client.get(
        "/admin/policy",
        headers=ADMIN_HEADERS,
        params={
            "quotas_lane": "principal",
            "quotas_subject_id": "agent:policy-page-00",
            "page_size": 5,
        },
    )
    assert quota_history.status_code == 200
    assert "agent:policy-page-00" in quota_history.text
    assert "<td><code>agent:policy-page-01</code></td><td>history</td>" not in quota_history.text
    assert "Quota revisions: 1-1 of 1 (page 1/1)" in quota_history.text


def test_admin_policy_quotas_json_supports_filters_and_paging(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    assert (
        client.post(
            "/admin/policy/principals",
            headers=ADMIN_HEADERS,
            json={"principal_id": "agent:quota-list-demo", "kind": "agent", "namespace": "tenant:kogwistar"},
        ).status_code
        == 200
    )

    for idx in range(5):
        assert (
            client.post(
                "/admin/policy/quotas/upsert",
                headers=ADMIN_HEADERS,
                json={
                    "lane": "principal",
                    "subject_id": "agent:quota-list-demo",
                    "quota_name": f"q{idx}",
                    "period": "hour",
                    "max_requests": idx + 1,
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/admin/policy/quotas/revoke",
            headers=ADMIN_HEADERS,
            json={
                "lane": "principal",
                "subject_id": "agent:quota-list-demo",
                "quota_name": "q0",
                "reason": "revoke-for-filter-test",
            },
        ).status_code
        == 200
    )

    page2 = client.get(
        "/admin/policy/quotas.json",
        headers=ADMIN_HEADERS,
        params={"lane": "principal", "subject_id": "agent:quota-list-demo", "page_size": 2, "page": 2},
    )
    assert page2.status_code == 200
    body = page2.json()
    assert body["paging"]["page"] == 2
    assert body["paging"]["page_size"] == 2
    assert body["paging"]["total"] == 6
    assert len(body["data"]) == 2

    by_name = client.get(
        "/admin/policy/quotas.json",
        headers=ADMIN_HEADERS,
        params={"subject_id": "agent:quota-list-demo", "quota_name": "q3"},
    )
    assert by_name.status_code == 200
    assert len(by_name.json()["data"]) == 1
    assert by_name.json()["data"][0]["quota_name"] == "q3"

    revoked_only = client.get(
        "/admin/policy/quotas.json",
        headers=ADMIN_HEADERS,
        params={"subject_id": "agent:quota-list-demo", "revoked": "true"},
    )
    assert revoked_only.status_code == 200
    assert all(item["revoked"] for item in revoked_only.json()["data"])

    bad = client.get("/admin/policy/quotas.json", headers=ADMIN_HEADERS, params={"revoked": "maybe"})
    assert bad.status_code == 400


def test_admin_policy_pricing_api_and_page_support_append_only_revisions(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph = tmp_path / "graph.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    upsert = client.post(
        "/admin/policy/pricing/upsert",
        headers=ADMIN_HEADERS,
        json={
            "scope": "provider_model",
            "subject": "ollama:gemma4:e2b",
            "price_per_1k_tokens_usd": 0.004,
            "reason": "hosted price",
        },
    )
    assert upsert.status_code == 200
    assert upsert.json()["ok"] is True

    key_upsert = client.post(
        "/admin/policy/pricing/upsert",
        headers=ADMIN_HEADERS,
        json={
            "scope": "key",
            "subject": "key:fwd-ollama:gemma4-local",
            "price_per_1k_tokens_usd": 0.0,
            "reason": "local is free",
        },
    )
    assert key_upsert.status_code == 200
    pricing = client.get("/admin/policy/pricing.json", headers=ADMIN_HEADERS)
    assert pricing.status_code == 200
    body = pricing.json()
    assert isinstance(body["data"], list)
    assert body["active"]["provider_model"]["ollama:gemma4:e2b"] == 0.004
    assert body["active"]["key"]["key:fwd-ollama:gemma4-local"] == 0.0

    revoke = client.post(
        "/admin/policy/pricing/revoke",
        headers=ADMIN_HEADERS,
        json={"scope": "provider_model", "subject": "ollama:gemma4:e2b", "reason": "retired"},
    )
    assert revoke.status_code == 200
    after = client.get("/admin/policy/pricing.json", headers=ADMIN_HEADERS).json()
    assert "ollama:gemma4:e2b" not in after["active"]["provider_model"]

    page = client.get("/admin/policy", headers=ADMIN_HEADERS)
    assert page.status_code == 200
    assert "Pricing Upsert (Append-Only Revision)" in page.text
    assert "Pricing Revisions" in page.text


def test_admin_policy_pricing_upsert_validates_subject_shape(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    client = TestClient(create_app("config/gateway_policy.json"))
    bad = client.post(
        "/admin/policy/pricing/upsert",
        headers=ADMIN_HEADERS,
        json={
            "scope": "provider_model",
            "subject": "invalid-no-colon",
            "price_per_1k_tokens_usd": 0.2,
        },
    )
    assert bad.status_code == 400
    assert "provider_colon_model" in bad.json()["error"]["message"]


def test_admin_policy_pricing_form_actions_support_upsert_and_revoke(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    client = TestClient(create_app("config/gateway_policy.json"))

    upsert_form = client.post(
        "/admin/policy",
        headers=ADMIN_HEADERS | {"accept": "text/html"},
        data={
            "action": "pricing_upsert",
            "scope": "model",
            "subject": "gemma4:e2b",
            "price_per_1k_tokens_usd": "0.005",
            "reason": "form upsert test",
        },
    )
    assert upsert_form.status_code == 200
    assert "Pricing revision added:" in upsert_form.text

    revoke_form = client.post(
        "/admin/policy",
        headers=ADMIN_HEADERS | {"accept": "text/html"},
        data={
            "action": "pricing_revoke",
            "scope": "model",
            "subject": "gemma4:e2b",
            "reason": "form revoke test",
        },
    )
    assert revoke_form.status_code == 200
    assert "Pricing revoked (append-only):" in revoke_form.text


def test_admin_policy_pricing_upsert_rejects_negative_price(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    client = TestClient(create_app("config/gateway_policy.json"))
    bad = client.post(
        "/admin/policy/pricing/upsert",
        headers=ADMIN_HEADERS,
        json={
            "scope": "model",
            "subject": "gemma4:e2b",
            "price_per_1k_tokens_usd": -0.01,
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["message"] == "pricing_price_must_be_non_negative"


def test_admin_policy_pricing_json_rejects_invalid_revoked_filter(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    client = TestClient(create_app("config/gateway_policy.json"))
    bad = client.get("/admin/policy/pricing.json", headers=ADMIN_HEADERS, params={"revoked": "maybe"})
    assert bad.status_code == 400
    assert bad.json()["error"]["message"] == "revoked_must_be_true_false"

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
    assert client.get("/admin/policy/pricing.json").status_code == 401
    assert client.post("/admin/policy/pricing/upsert", json={}).status_code == 401
    assert client.post("/admin/policy/pricing/revoke", json={}).status_code == 401
    assert client.post("/admin/review/run", json={}).status_code == 401
    assert client.get("/admin/review/status.json").status_code == 401
    assert client.post("/admin/review/checkpoint", json={}).status_code == 401


def test_admin_routes_can_require_keycloak_admin_role(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "model.admin")

    def fake_keycloak(self, token):
        if token == "kc-admin":
            return TokenPrincipal("human:admin", "human", ("model.admin",), "tenant:kogwistar", ("model.invoke",), "jti-admin", None)
        if token == "kc-user":
            return TokenPrincipal("human:user", "human", ("model.invoke",), "tenant:kogwistar", ("model.invoke",), "jti-user", None)
        return None

    monkeypatch.setattr("modelkeyguard.token_auth.TokenVerifier._verify_keycloak_token", fake_keycloak)
    client = TestClient(create_app("config/gateway_policy.json"))

    assert client.get("/admin/usage", headers=ADMIN_HEADERS).status_code == 401
    assert client.get("/admin/usage", headers={"authorization": "Bearer kc-user"}).status_code == 403
    assert client.get("/admin/usage", headers={"authorization": "Bearer kc-admin"}).status_code == 200


def test_admin_usage_routes_can_accept_keycloak_usage_role(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "model.admin")
    monkeypatch.setenv("MODELKEYGUARD_USAGE_REQUIRED_ROLE", "model.usage.read")

    def fake_keycloak(self, token):
        if token == "kc-usage":
            return TokenPrincipal("agent:usage", "service", ("model.usage.read",), "tenant:kogwistar", ("model.invoke",), "jti-usage", None)
        if token == "kc-admin":
            return TokenPrincipal("human:admin", "human", ("model.admin",), "tenant:kogwistar", ("model.invoke",), "jti-admin", None)
        return None

    monkeypatch.setattr("modelkeyguard.token_auth.TokenVerifier._verify_keycloak_token", fake_keycloak)
    client = TestClient(create_app("config/gateway_policy.json"))

    assert client.get("/admin/usage", headers={"authorization": "Bearer kc-usage"}).status_code == 200
    assert client.get("/admin/usage.json", headers={"authorization": "Bearer kc-usage"}).status_code == 200
    assert client.get("/admin/review/status.json", headers={"authorization": "Bearer kc-usage"}).status_code == 403
    assert client.get("/admin/review/status.json", headers={"authorization": "Bearer kc-admin"}).status_code == 200
    assert client.get("/admin/keys", headers={"authorization": "Bearer kc-usage"}).status_code == 403
    assert client.get("/admin/policy", headers={"authorization": "Bearer kc-usage"}).status_code == 403


def test_admin_review_run_can_select_graph_authoritative_pipeline(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_REVIEW_PIPELINE", "graph")
    client = TestClient(create_app("config/gateway_policy.json"))
    response = client.post("/admin/review/run", headers=ADMIN_HEADERS, json={})
    assert response.status_code == 200
    assert response.json()["pipeline"] == "graph"
    assert response.json()["automatic_retry"] is False


def test_admin_routes_can_allow_secret_or_keycloak(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "secret_or_keycloak")

    def fake_keycloak(self, token):
        if token == "kc-admin":
            return TokenPrincipal("human:admin", "human", ("model.admin",), "tenant:kogwistar", ("model.invoke",), "jti-admin", None)
        return None

    monkeypatch.setattr("modelkeyguard.token_auth.TokenVerifier._verify_keycloak_token", fake_keycloak)
    client = TestClient(create_app("config/gateway_policy.json"))

    assert client.get("/admin/usage", headers=ADMIN_HEADERS).status_code == 200
    assert client.get("/admin/usage", headers={"authorization": "Bearer kc-admin"}).status_code == 200


def test_model_list_can_require_authorization(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"authorization": "Bearer kgw_demo_doc_ingestor"}).status_code == 200


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
    assert "token" in quota_body["properties"]["lane"]["enum"]
    pricing_post = spec["paths"]["/admin/policy/pricing/upsert"]["post"]
    pricing_body = pricing_post["requestBody"]["content"]["application/json"]["schema"]
    assert "scope" in pricing_body["properties"]
    assert "subject" in pricing_body["properties"]
    assert "price_per_1k_tokens_usd" in pricing_body["properties"]


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


def test_admin_oidc_browser_login_redirects_and_issues_session_cookie(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "oidc-admin")
    monkeypatch.setenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789")
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak.example")
    monkeypatch.setenv("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "modelguard")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID", "modelguard-admin-web")

    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    login = client.get("/admin/oidc/login", params={"next": "/admin/usage"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"].startswith("http://127.0.0.1:8080/")
    assert "code_challenge=" in login.headers["location"]
    assert "client_id=modelguard-admin-web" in login.headers["location"]
    assert "state=" in login.headers["location"]

    state_cookie = login.cookies.get("kgw_admin_oidc_session")
    assert state_cookie

    def _fake_exchange_authorization_code(**kwargs):
        payload = {
            "alg": "none",
            "typ": "JWT",
        }
        claims = {
            "sub": "user:admin",
            "preferred_username": "admin",
            "iss": "http://keycloak.example/realms/modelguard",
            "realm_access": {"roles": ["oidc-admin"]},
        }
        header = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
        return {"access_token": f"{header}.{body}.sig"}

    monkeypatch.setattr("modelkeyguard.services.admin_oidc.exchange_authorization_code", _fake_exchange_authorization_code)

    callback = client.get(
        "/admin/oidc/callback",
        params={"code": "auth-code-123", "state": login.headers["location"].split("state=")[1].split("&", 1)[0], "next": "/admin/usage"},
        cookies={"kgw_admin_oidc_session": state_cookie},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].endswith("/admin/usage")
    assert client.get("/admin/usage").status_code == 200


def test_admin_login_page_shows_keycloak_entry_point(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    page = client.get("/admin/session")
    assert page.status_code == 401
    assert "Sign in with Keycloak" in page.text


def test_admin_oidc_login_uses_forwarded_localhost_for_browser_redirect(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "oidc-admin")
    monkeypatch.setenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789")
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak.example")
    monkeypatch.setenv("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL", "http://10.5.0.4:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "modelguard")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID", "modelguard-admin-web")

    client = TestClient(create_app("config/gateway_policy.json"), base_url="http://127.0.0.1:8789")
    login = client.get("/admin/oidc/login", params={"next": "/admin/usage"}, follow_redirects=False)

    assert login.status_code == 303
    location = login.headers["location"]
    assert location.startswith("http://127.0.0.1:8080/")
    assert "10.5.0.4" not in location
    params = parse_qs(urlparse(location).query)
    assert params["redirect_uri"] == ["http://127.0.0.1:8789/admin/oidc/callback"]


def test_admin_oidc_login_keeps_public_urls_for_public_browser_access(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "oidc-admin")
    monkeypatch.setenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "https://gateway.example")
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak:8080")
    monkeypatch.setenv("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL", "https://keycloak.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "modelguard")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID", "modelguard-admin-web")

    client = TestClient(create_app("config/gateway_policy.json"), base_url="https://gateway.example")
    login = client.get("/admin/oidc/login", params={"next": "/admin/usage"}, follow_redirects=False)

    assert login.status_code == 303
    location = login.headers["location"]
    assert location.startswith("https://keycloak.example/")
    params = parse_qs(urlparse(location).query)
    assert params["redirect_uri"] == ["https://gateway.example/admin/oidc/callback"]


def test_admin_oidc_login_supports_intranet_and_internet_browser_access(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_AUTH_MODE", "keycloak")
    monkeypatch.setenv("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "oidc-admin")
    monkeypatch.setenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "https://gateway.example")
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak:8080")
    monkeypatch.setenv("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL", "https://keycloak.example")
    monkeypatch.setenv("MODELKEYGUARD_KEYCLOAK_LOCAL_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "modelguard")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID", "modelguard-admin-web")

    app = create_app("config/gateway_policy.json")
    intranet_client = TestClient(app, base_url="http://127.0.0.1:8789")
    internet_client = TestClient(app, base_url="https://gateway.example")

    intranet_login = intranet_client.get("/admin/oidc/login", params={"next": "/admin/usage"}, follow_redirects=False)
    internet_login = internet_client.get("/admin/oidc/login", params={"next": "/admin/usage"}, follow_redirects=False)

    assert intranet_login.status_code == 303
    intranet_location = intranet_login.headers["location"]
    assert intranet_location.startswith("http://127.0.0.1:8080/")
    intranet_params = parse_qs(urlparse(intranet_location).query)
    assert intranet_params["redirect_uri"] == ["http://127.0.0.1:8789/admin/oidc/callback"]

    assert internet_login.status_code == 303
    internet_location = internet_login.headers["location"]
    assert internet_location.startswith("https://keycloak.example/")
    internet_params = parse_qs(urlparse(internet_location).query)
    assert internet_params["redirect_uri"] == ["https://gateway.example/admin/oidc/callback"]


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


def test_admin_policy_quota_upsert_accepts_infinite_period(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    user = client.post(
        "/admin/policy/users",
        headers=ADMIN_HEADERS,
        json={"user_id": "user:infinite", "display_name": "Infinite"},
    )
    assert user.status_code == 200

    resp = client.post(
        "/admin/policy/quotas/upsert",
        headers=ADMIN_HEADERS,
        json={
            "lane": "user",
            "subject_id": "user:infinite",
            "quota_name": "lifetime",
            "period": "infinite",
            "max_requests": 1,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["quota_policy_id"].startswith("quota:user:user:infinite:lifetime")


def test_admin_policy_quota_upsert_accepts_token_lane_for_issued_safe_token(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    assert client.post("/admin/policy/applications", headers=ADMIN_HEADERS, json={"application_id": "app:token-demo", "display_name": "Token Demo"}).status_code == 200
    assert client.post("/admin/policy/users", headers=ADMIN_HEADERS, json={"user_id": "user:token-demo", "display_name": "Token Demo"}).status_code == 200
    assert client.post(
        "/admin/policy/principals",
        headers=ADMIN_HEADERS,
        json={
            "principal_id": "agent:token-demo",
            "kind": "agent",
            "groups": ["agent-dev"],
            "namespace": "tenant:kogwistar",
            "application_id": "app:token-demo",
            "description": "token quota demo",
        },
    ).status_code == 200

    issued = client.post(
        "/admin/policy/tokens",
        headers=ADMIN_HEADERS,
        json={
            "principal_id": "agent:token-demo",
            "namespace": "tenant:kogwistar",
            "on_behalf_of_user_id": "user:token-demo",
            "application_id": "app:token-demo",
            "scopes": ["model.invoke"],
        },
    )
    assert issued.status_code == 200
    token_id = issued.json()["token_id"]

    resp = client.post(
        "/admin/policy/quotas/upsert",
        headers=ADMIN_HEADERS,
        json={
            "lane": "token",
            "subject_id": token_id,
            "quota_name": "lifetime",
            "period": "infinite",
            "max_requests": 1,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["quota_policy_id"].startswith(f"quota:token:{token_id}:lifetime")
