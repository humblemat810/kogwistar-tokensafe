from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest

from modelkeyguard.gateway import build_guard, process_chat_completion
from modelkeyguard.graph_state import GraphStateStore, resolve_store_backend
from modelkeyguard.registration import RegistrationService, main as registration_main, open_registration_store, register_usage_demo, safe_token_hash
from modelkeyguard.policy_loader import load_policy_json
from modelkeyguard.token_auth import TokenVerifier

EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


def payload(max_tokens=16):
    return {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": EXPECTED_SYSTEM},
            {"role": "user", "content": "Prove this registered token works."},
        ],
        "max_tokens": max_tokens,
    }


def test_register_user_principal_application_quota_and_safe_token(tmp_path):
    store = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    reg = RegistrationService(store)
    reg.register_user("user:test", "Test User")
    reg.register_application("app:test", "Test App")
    reg.register_principal("agent:test", kind="agent", groups=["agent-dev"], namespace="tenant:kogwistar", application_id="app:test")
    reg.set_quota("principal", "agent:test", "hour", period="hour", max_tokens=1000, max_requests=10)
    reg.set_quota("user", "user:test", "hour", period="hour", max_tokens=1000, max_requests=10)
    issued = reg.issue_safe_token(principal_id="agent:test", namespace="tenant:kogwistar", on_behalf_of_user_id="user:test", application_id="app:test")

    token_node = store.nodes[issued.token_node_id]
    assert token_node.payload["safe_token_hash"] == safe_token_hash(issued.token)
    assert "token" not in token_node.payload
    assert issued.token not in (tmp_path / "graph.jsonl").read_text(encoding="utf-8")
    assert any(e.kind == "AUTHENTICATES_AS" and e.target == "agent:test" for e in store.edges_from(issued.token_node_id))
    assert any(e.kind == "ON_BEHALF_OF" and e.target == "user:test" for e in store.edges_from(issued.token_node_id))


def test_safe_token_verifier_infers_principal_user_and_application(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    token = register_usage_demo(graph_path, "demo-test-key").token
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "demo-test-key")

    verified = TokenVerifier("config/gateway_policy.json").verify_token(token)

    assert verified.principal_id == "agent:demo-saas-agent"
    assert verified.on_behalf_of_user_id == "user:demo-saas-alice"
    assert verified.namespace == "tenant:kogwistar"
    assert "model.invoke" in verified.scopes


def test_registered_safe_token_end_to_end_openai_compatible_call(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    token = register_usage_demo(graph_path, "demo-test-key").token
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "demo-test-key")
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")

    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")
    status, data, _headers = process_chat_completion(payload(), f"Bearer {token}", guard, policy, verifier, source_ip="127.0.0.1")

    assert status == 200
    assert data["modelkeyguard"]["decision"] == "ALLOWED"
    assert "agent:demo-saas-agent" in data["choices"][0]["message"]["content"]

    store = GraphStateStore(graph_path, app_key="demo-test-key")
    assert store.get_quota_used("principal", "agent:demo-saas-agent", "hour")["requests"] == 1
    assert store.get_quota_used("user", "user:demo-saas-alice", "hour")["requests"] == 1
    assert any(n.kind == "usage_lane_head" and n.payload["subject_id"] == "user:demo-saas-alice" for n in store.nodes.values())


def test_registration_summary_contains_expected_objects(tmp_path):
    graph_path = tmp_path / "graph.jsonl"
    token = register_usage_demo(graph_path, "demo-test-key").token
    store = GraphStateStore(graph_path, app_key="demo-test-key")
    out = tmp_path / "summary.json"
    RegistrationService(store).write_usage_summary(out)
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert "user:demo-saas-alice" in summary["users"]
    assert "agent:demo-saas-agent" in summary["principals"]
    assert "app:demo-saas" in summary["applications"]
    assert token not in graph_path.read_text(encoding="utf-8")


def test_registered_user_quota_can_return_user_429(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    token = register_usage_demo(graph_path, "demo-test-key").token
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "demo-test-key")
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")

    # Tighten registered user's hour quota after demo registration.
    store = GraphStateStore(graph_path, app_key="demo-test-key")
    RegistrationService(store).set_quota("user", "user:demo-saas-alice", "tiny", period="hour", max_tokens=1, max_requests=1)

    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")
    status, data, _headers = process_chat_completion(payload(max_tokens=16), f"Bearer {token}", guard, policy, verifier, source_ip="127.0.0.1")

    assert status == 429
    assert data["error"]["message"] == "user_quota_exceeded"


def test_registration_seed_cli_creates_user_principal_and_token(tmp_path, monkeypatch, capsys):
    graph_path = tmp_path / "seed-graph.jsonl"
    token_file = tmp_path / "seed.token"
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "seed-test-key")

    code = registration_main(
        [
            "seed",
            "--user-id",
            "user:cli-seed",
            "--user-display-name",
            "CLI Seed User",
            "--principal-id",
            "agent:cli-seed",
            "--principal-groups",
            "agent-dev",
            "--namespace",
            "tenant:kogwistar",
            "--application-id",
            "app:cli-seed",
            "--token-output-file",
            str(token_file),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["user_id"] == "user:cli-seed"
    assert payload["principal_id"] == "agent:cli-seed"
    assert payload["application_id"] == "app:cli-seed"
    assert payload["safe_token"]
    assert token_file.read_text(encoding="utf-8").strip() == payload["safe_token"]

    store = GraphStateStore(graph_path, app_key="seed-test-key")
    assert "user:cli-seed" in store.nodes
    assert "agent:cli-seed" in store.nodes
    assert "app:cli-seed" in store.nodes
    assert any(edge.kind == "ON_BEHALF_OF" and edge.target == "user:cli-seed" for edge in store.edges.values())


def test_registration_seed_cli_uses_remote_admin_api_when_configured(tmp_path, monkeypatch, capsys):
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]):
            self.payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=10):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        headers = {str(k).lower(): str(v) for k, v in req.header_items()}
        calls.append({"url": req.full_url, "method": req.get_method(), "headers": headers, "body": body})
        if req.full_url.endswith("/admin/policy/users"):
            return FakeResponse({"ok": True, "user_id": body["user_id"]})
        if req.full_url.endswith("/admin/policy/applications"):
            return FakeResponse({"ok": True, "application_id": body["application_id"]})
        if req.full_url.endswith("/admin/policy/principals"):
            return FakeResponse({"ok": True, "principal_id": body["principal_id"]})
        if req.full_url.endswith("/admin/policy/quotas/upsert"):
            return FakeResponse({"ok": True, "quota_policy_id": f"quota:{body['lane']}:{body['subject_id']}:{body['quota_name']}"})
        if req.full_url.endswith("/admin/policy/tokens"):
            return FakeResponse(
                {
                    "ok": True,
                    "one_time_reveal": True,
                    "safe_token": "kgw_sk_remote_test",
                    "token_id": "remote-token-id",
                    "principal_id": body["principal_id"],
                    "namespace": body["namespace"],
                    "on_behalf_of_user_id": body.get("on_behalf_of_user_id"),
                    "application_id": body.get("application_id"),
                    "scopes": body.get("scopes", ["model.invoke"]),
                    "safe_token_hash": "sha256:remote",
                }
            )
        raise AssertionError(f"unexpected remote url: {req.full_url}")

    monkeypatch.setattr("modelkeyguard.registration.urllib.request.urlopen", fake_urlopen)

    token_file = tmp_path / "remote.token"
    code = registration_main(
        [
            "--admin-base-url",
            "https://remote.example",
            "--admin-bearer-token",
            "bearer-123",
            "seed",
            "--user-id",
            "user:remote-cli",
            "--user-display-name",
            "Remote CLI User",
            "--principal-id",
            "agent:remote-cli",
            "--principal-groups",
            "review-dev",
            "--namespace",
            "tenant:kogwistar",
            "--application-id",
            "app:remote-cli",
            "--token-output-file",
            str(token_file),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["user_id"] == "user:remote-cli"
    assert payload["principal_id"] == "agent:remote-cli"
    assert payload["application_id"] == "app:remote-cli"
    assert payload["safe_token"] == "kgw_sk_remote_test"
    assert token_file.read_text(encoding="utf-8").strip() == "kgw_sk_remote_test"
    assert any(call["url"].endswith("/admin/policy/users") for call in calls)
    assert any(call["url"].endswith("/admin/policy/quotas/upsert") for call in calls)
    assert any(call["headers"].get("authorization") == "Bearer bearer-123" for call in calls)


def test_append_only_quota_revision_updates_projection_latest_only(tmp_path):
    store = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    reg = RegistrationService(store)
    reg.register_user("user:test", "Test User")

    reg.append_quota_revision("user", "user:test", "hourly", period="hour", max_requests=10)
    reg.append_quota_revision("user", "user:test", "hourly", period="hour", max_requests=5)
    projection = store.get_quota_policy_projection("user", "user:test")
    assert projection is not None
    assert len(projection["items"]) == 1
    assert projection["items"][0]["max_requests"] == 5

    reg.revoke_quota("user", "user:test", "hourly", reason="disable")
    projection2 = store.get_quota_policy_projection("user", "user:test")
    assert projection2 is not None
    assert projection2["items"] == []


def test_infinite_quota_does_not_refresh_across_future_usage_windows(tmp_path):
    store = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    reg = RegistrationService(store)
    reg.register_user("user:test", "Test User")
    reg.set_quota("user", "user:test", "lifetime", period="infinite", max_requests=1)

    now = datetime.now(timezone.utc)
    store.add_quota_usage("user", "user:test", "infinite", 0.1, 100, when=now)
    assert store.get_quota_used("user", "user:test", "infinite")["requests"] == 1
    assert store.get_quota_used("user", "user:test", "infinite", when=now + timedelta(days=3650))["requests"] == 1


def test_registration_store_invariant_uses_postgres_when_configured(monkeypatch):
    calls: list[str] = []

    class FakePostgresStore:
        def __init__(self, *args, **kwargs):
            calls.append("postgres")

    monkeypatch.setenv("MODELKEYGUARD_STORE", "postgres")
    monkeypatch.setitem(sys.modules, "modelkeyguard.postgres_state", SimpleNamespace(PostgresGraphStateStore=FakePostgresStore))

    store = open_registration_store()
    assert calls == ["postgres"]
    assert isinstance(store, FakePostgresStore)


def test_registration_store_invariant_uses_kogwistar_postgres_when_configured(monkeypatch):
    calls: list[str] = []

    class FakeKogwistarStore:
        def __init__(self, *args, **kwargs):
            calls.append("kogwistar_postgres")

    monkeypatch.setenv("MODELKEYGUARD_STORE", "kogwistar_postgres")
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    store = open_registration_store()
    assert calls == ["kogwistar_postgres"]
    assert isinstance(store, FakeKogwistarStore)


def test_packaged_default_policy_is_available_without_checkout_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    policy = load_policy_json("config/gateway_policy.json")
    assert policy["issuer"] == "keycloak:modelguard"
    assert policy["users"]["user:alice"]["display_name"] == "Alice Example"
    assert policy["model_keys"][0]["id"] == "key:openai:prod"

    issued = register_usage_demo(tmp_path / "graph.jsonl", "packaged-policy-test-key")
    assert issued.principal_id == "agent:demo-saas-agent"
    assert issued.on_behalf_of_user_id == "user:demo-saas-alice"
    assert (tmp_path / "out" / "registration_demo_token.txt").read_text(encoding="utf-8").strip() == issued.token


def test_registration_store_invariant_rejects_unknown_serious_backend(monkeypatch, capsys):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "chroma")
    code = registration_main(["register-user", "--user-id", "user:test"])
    captured = capsys.readouterr()
    assert code == 2
    assert "unsupported_store_backend:chroma" in captured.out


def test_token_verifier_rejects_unknown_serious_backend_without_jsonl_fallback(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_STORE", "chroma")
    with pytest.raises(ValueError, match="unsupported_store_backend:chroma"):
        TokenVerifier(policy_path)


def test_backend_resolver_accepts_kogwistar_postgres():
    assert resolve_store_backend("kogwistar_postgres") == "kogwistar_postgres"


def test_graph_state_from_policy_dispatches_kogwistar_postgres(monkeypatch):
    calls: list[str] = []

    class FakeKogwistarStore:
        @classmethod
        def from_policy(cls, policy, dsn=None, app_key=None):
            calls.append("dispatch")
            return cls()

    monkeypatch.setenv("MODELKEYGUARD_STORE", "kogwistar_postgres")
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    out = GraphStateStore.from_policy({})
    assert calls == ["dispatch"]
    assert isinstance(out, FakeKogwistarStore)
