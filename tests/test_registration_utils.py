from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

from modelkeyguard.gateway import build_guard, process_chat_completion
from modelkeyguard.graph_state import GraphStateStore, resolve_store_backend
from modelkeyguard import kogwistar_postgres_state as kog_state
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
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")

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
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")

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
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")

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


def test_modelkeyguard_top_level_registration_forwards_remote_admin_args(monkeypatch):
    import modelkeyguard.__main__ as cli_main

    captured: dict[str, list[str]] = {}

    def fake_registration_main(argv):
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(cli_main, "registration_main", fake_registration_main)

    rc = cli_main.main(
        [
            "--admin-base-url",
            "http://127.0.0.1:8789",
            "--admin-bearer-token",
            "token-123",
            "registration",
            "register-user",
            "--user-id",
            "user:alice",
            "--display-name",
            "Alice",
        ]
    )

    assert rc == 0
    assert captured["argv"] == [
        "--admin-base-url",
        "http://127.0.0.1:8789",
        "--admin-bearer-token",
        "token-123",
        "register-user",
        "--user-id",
        "user:alice",
        "--display-name",
        "Alice",
    ]


def test_registration_main_defaults_to_kogwistar_postgres_for_mixed_register_and_quota(monkeypatch):
    class FakeKogwistarStore:
        instances = 0
        nodes: dict[str, object] = {}
        edges: dict[str, object] = {}
        events: list[dict[str, object]] = []
        projections: dict[str, dict[str, object]] = {}

        def __init__(self, *args, **kwargs):
            type(self).instances += 1
            self.nodes = type(self).nodes
            self.edges = type(self).edges
            self.events = type(self).events
            self.projections = type(self).projections

        @classmethod
        def reset(cls):
            cls.instances = 0
            cls.nodes = {}
            cls.edges = {}
            cls.events = []
            cls.projections = {}

        def put_node(self, node_id, kind, payload):
            self.nodes[node_id] = SimpleNamespace(id=node_id, kind=kind, payload=dict(payload))

        def put_edge(self, edge_id, kind, source, target, payload):
            self.edges[edge_id] = SimpleNamespace(id=edge_id, kind=kind, source=source, target=target, payload=dict(payload))

        def append_event(self, event_type, subject_id, payload):
            event_id = f"event:{event_type}:{len(self.events)+1:08d}"
            event = {"record_type": "event", "id": event_id, "kind": event_type, "subject": subject_id, "payload": dict(payload)}
            self.events.append(event)
            return event

        def rebuild_quota_policy_projection(self, lane, subject_id):
            return {"lane": lane, "subject_id": subject_id, "items": []}

    FakeKogwistarStore.reset()
    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    register_rc = registration_main(
        [
            "register-user",
            "--user-id",
            "user:mixed-default",
            "--display-name",
            "Mixed Default",
        ]
    )
    quota_rc = registration_main(
        [
            "set-quota",
            "--lane",
            "user",
            "--subject-id",
            "user:mixed-default",
            "--quota-name",
            "month",
            "--period",
            "month",
            "--max-usd",
            "5",
            "--max-tokens",
            "50000",
            "--max-requests",
            "200",
        ]
    )

    assert register_rc == 0
    assert quota_rc == 0
    assert FakeKogwistarStore.instances >= 2
    assert "user:mixed-default" in FakeKogwistarStore.nodes
    assert any(str(node_id).startswith("quota:user:user:mixed-default:month") for node_id in FakeKogwistarStore.nodes)


def test_registration_main_defaults_to_kogwistar_postgres_for_principal_flow(monkeypatch):
    class FakeKogwistarStore:
        instances = 0
        nodes: dict[str, object] = {}
        edges: dict[str, object] = {}
        events: list[dict[str, object]] = []
        projections: dict[str, dict[str, object]] = {}

        def __init__(self, *args, **kwargs):
            type(self).instances += 1
            self.nodes = type(self).nodes
            self.edges = type(self).edges
            self.events = type(self).events
            self.projections = type(self).projections

        @classmethod
        def reset(cls):
            cls.instances = 0
            cls.nodes = {}
            cls.edges = {}
            cls.events = []
            cls.projections = {}

        def put_node(self, node_id, kind, payload):
            self.nodes[node_id] = SimpleNamespace(id=node_id, kind=kind, payload=dict(payload))

        def put_edge(self, edge_id, kind, source, target, payload):
            self.edges[edge_id] = SimpleNamespace(id=edge_id, kind=kind, source=source, target=target, payload=dict(payload))

        def append_event(self, event_type, subject_id, payload):
            event_id = f"event:{event_type}:{len(self.events)+1:08d}"
            event = {"record_type": "event", "id": event_id, "kind": event_type, "subject": subject_id, "payload": dict(payload)}
            self.events.append(event)
            return event

        def rebuild_quota_policy_projection(self, lane, subject_id):
            return {"lane": lane, "subject_id": subject_id, "items": []}

    FakeKogwistarStore.reset()
    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    register_rc = registration_main(
        [
            "register-principal",
            "--principal-id",
            "agent:principal-default",
            "--kind",
            "agent",
            "--groups",
            "review-dev",
            "--namespace",
            "tenant:kogwistar",
            "--application-id",
            "app:principal-default",
        ]
    )
    quota_rc = registration_main(
        [
            "set-quota",
            "--lane",
            "principal",
            "--subject-id",
            "agent:principal-default",
            "--quota-name",
            "month",
            "--period",
            "month",
            "--max-usd",
            "5",
            "--max-tokens",
            "50000",
            "--max-requests",
            "200",
        ]
    )

    assert register_rc == 0
    assert quota_rc == 0
    assert FakeKogwistarStore.instances >= 2
    assert "agent:principal-default" in FakeKogwistarStore.nodes
    assert "app:principal-default" in FakeKogwistarStore.nodes
    assert any(str(node_id).startswith("quota:principal:agent:principal-default:month") for node_id in FakeKogwistarStore.nodes)


def test_registration_main_explicit_jsonl_demo_mode_still_works(tmp_path, monkeypatch):
    graph_path = tmp_path / "demo-graph.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "demo-jsonl-key")

    rc_user = registration_main(["register-user", "--user-id", "user:demo-jsonl", "--display-name", "Demo Jsonl"])
    rc_quota = registration_main(
        [
            "set-quota",
            "--lane",
            "user",
            "--subject-id",
            "user:demo-jsonl",
            "--quota-name",
            "hour",
            "--period",
            "hour",
            "--max-requests",
            "10",
        ]
    )

    assert rc_user == 0
    assert rc_quota == 0
    contents = graph_path.read_text(encoding="utf-8")
    assert "user:demo-jsonl" in contents
    assert "quota:user:user:demo-jsonl:hour" in contents


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


def test_registration_store_invariant_defaults_to_kogwistar_postgres(monkeypatch):
    calls: list[str] = []

    class FakeKogwistarStore:
        def __init__(self, *args, **kwargs):
            calls.append("kogwistar_postgres")

    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    store = open_registration_store()
    assert calls == ["kogwistar_postgres"]
    assert isinstance(store, FakeKogwistarStore)


def test_store_backend_resolver_defaults_to_kogwistar_postgres(monkeypatch):
    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    assert resolve_store_backend() == "kogwistar_postgres"


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


def test_graph_state_from_policy_defaults_to_kogwistar_postgres_without_store_env(monkeypatch):
    calls: list[str] = []

    class FakeKogwistarStore:
        @classmethod
        def from_policy(cls, policy, dsn=None, app_key=None):
            calls.append("dispatch")
            return cls()

    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    out = GraphStateStore.from_policy({})
    assert calls == ["dispatch"]
    assert isinstance(out, FakeKogwistarStore)


def test_token_verifier_empty_policy_defaults_to_kogwistar_postgres_without_store_env(tmp_path, monkeypatch):
    policy_path = tmp_path / "empty-policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    calls: list[str] = []

    class FakeKogwistarStore:
        def __init__(self, *args, **kwargs):
            calls.append("kogwistar_postgres")
            self.nodes = {}
            self.edges = {}
            self.events = []
            self.projections = {}

    monkeypatch.delenv("MODELKEYGUARD_STORE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "modelkeyguard.kogwistar_postgres_state",
        SimpleNamespace(KogwistarPostgresGraphStateStore=FakeKogwistarStore),
    )

    verifier = TokenVerifier(policy_path)

    assert calls == ["kogwistar_postgres"]
    assert isinstance(verifier.graph_state, FakeKogwistarStore)


def test_kogwistar_runtime_uses_postgres_search_index_in_postgres_mode(monkeypatch):
    calls: dict[str, object] = {}

    class ExplodingSearchIndex:
        def __init__(self, engine, index_db_path):
            raise AssertionError(f"sqlite search index must not initialize: {index_db_path}")

    fake_engine_core_module = ModuleType("kogwistar.engine_core")
    fake_engine_module = ModuleType("kogwistar.engine_core.engine")
    fake_engine_module.SearchIndexService = ExplodingSearchIndex

    class FakeGraphKnowledgeEngine:
        def __init__(self, *, persist_directory, embedding_function, backend):
            calls["persist_directory"] = persist_directory
            calls["backend"] = backend
            calls["search_index_class"] = fake_engine_module.SearchIndexService
            self.search_index = "postgres-search-index"
            self.meta_sqlite = object()

    fake_engine_module.GraphKnowledgeEngine = FakeGraphKnowledgeEngine

    class FakePostgresConfig:
        def __init__(self, *, dsn, embedding_dim):
            calls["dsn"] = dsn
            calls["embedding_dim"] = embedding_dim

    fake_postgres_module = ModuleType("kogwistar.engine_core.engine_postgres")
    fake_postgres_module.EnginePostgresConfig = FakePostgresConfig
    fake_postgres_module.build_postgres_backend = lambda cfg: ("postgres-backend", "postgres-uow")

    monkeypatch.setattr(kog_state, "enforce_installed_kogwistar_only", lambda: None)
    monkeypatch.setitem(sys.modules, "kogwistar.engine_core", fake_engine_core_module)
    monkeypatch.setitem(sys.modules, "kogwistar.engine_core.engine", fake_engine_module)
    monkeypatch.setitem(sys.modules, "kogwistar.engine_core.engine_postgres", fake_postgres_module)

    store = kog_state.KogwistarPostgresGraphStateStore.__new__(kog_state.KogwistarPostgresGraphStateStore)
    store.dsn = "postgresql://example/modelguard"
    store.embed_dim = 2

    runtime = store._build_runtime()

    assert calls["persist_directory"] is None
    assert calls["backend"] == "postgres-backend"
    assert calls["search_index_class"] is kog_state._PostgresKogwistarSearchIndexService
    assert runtime.engine._backend_uow == "postgres-uow"
    assert fake_engine_module.SearchIndexService is ExplodingSearchIndex


def test_postgres_search_index_uses_postgres_table_not_sqlite(monkeypatch):
    statements: list[str] = []
    params_seen: list[dict[str, object]] = []

    class FakeResult:
        returns_rows = False

        def mappings(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            statements.append(str(sql))
            params_seen.append(dict(params or {}))
            return FakeResult()

    class FakeEngine:
        def begin(self):
            return FakeConnection()

    class FakeBackend:
        schema = "public"
        engine = FakeEngine()

    events: list[tuple[str, str]] = []

    fake_engine = SimpleNamespace(
        backend=FakeBackend(),
        meta_sqlite=SimpleNamespace(engine=FakeEngine()),
        namespace="default",
        kg_graph_type="knowledge",
        _append_event_for_entity=lambda **kwargs: events.append(("append", kwargs["entity_id"])),
        _emit_change=lambda **kwargs: events.append(("emit", kwargs["entity"].id)),
    )
    service = kog_state._PostgresKogwistarSearchIndexService(fake_engine, index_db_path="/must/not/use.sqlite")

    class Item:
        node_id = "node:alpha"
        canonical_title = "Alpha"
        keywords = ["security", "quota"]
        aliases = ["A"]
        provision = "review"
        doc_id = "doc:1"

    service.upsert_entries([Item()])

    all_sql = "\n".join(statements).lower()
    assert "create table if not exists public.semantic_index" in all_sql
    assert "tsvector" in all_sql
    assert "using gin(search_text)" in all_sql
    assert "insert into public.semantic_index" in all_sql
    assert "sqlite" not in all_sql
    assert params_seen[-1]["index_key"] == "node:alpha|Alpha|review"
    assert ("append", "node:alpha|Alpha|review") in events
    assert ("emit", "node:alpha|Alpha|review") in events
