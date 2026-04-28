from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from modelkeyguard.gateway import build_guard, process_chat_completion
from modelkeyguard.graph_state import GraphStateStore, resolve_store_backend
from modelkeyguard.registration import RegistrationService, main as registration_main, open_registration_store, register_usage_demo, safe_token_hash
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
