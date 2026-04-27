import json
from pathlib import Path

import pytest

from modelkeyguard.gateway import build_guard, select_key, sha256_text
from modelkeyguard.token_auth import TokenVerifier, TokenAuthError
from modelkeyguard.core import Principal, Request
from modelkeyguard.graph_state import GraphStateStore


def test_local_token_verifies_with_on_behalf_user(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    v = TokenVerifier("config/gateway_policy.json")
    p = v.verify_token("kgw_demo_doc_ingestor")
    assert p.principal_id == "agent:doc-ingestor"
    assert p.namespace == "tenant:kogwistar"
    assert p.on_behalf_of_user_id == "user:alice"


def test_acl_allows_doc_ingestor_and_blocks_external(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    key_id = select_key(policy, "gpt-4o-mini")
    d1 = guard.check(Request(Principal("agent:doc-ingestor", "agent", ("agent-dev",)), key_id, "gpt-4o-mini", "tenant:kogwistar", estimated_cost_usd=0.01, estimated_tokens=100, on_behalf_of_user_id="user:alice", token_id="t1"))
    d2 = guard.check(Request(Principal("agent:external-scraper", "agent", ()), key_id, "gpt-4o-mini", "tenant:other", estimated_cost_usd=0.01, estimated_tokens=100, token_id="t2"))
    assert d1.allowed is True
    assert d2.allowed is False
    assert d2.http_status == 403
    assert d2.reason == "permission_denied"


def test_expected_system_prompt_hash_matches_policy():
    policy = json.loads(Path("config/gateway_policy.json").read_text())
    profile = policy["usage_profiles"]["agent:doc-ingestor"]
    assert sha256_text(profile["expected_system_prompt"]) in profile["system_prompt_hashes"]


def test_principal_quota_returns_429_principal_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    key_id = select_key(policy, "gpt-4o-mini")
    d = guard.check(Request(Principal("agent:batch-heavy", "agent", ("agent-dev",)), key_id, "gpt-4o-mini", "tenant:kogwistar", estimated_cost_usd=0.01, estimated_tokens=100, on_behalf_of_user_id="user:alice", token_id="busy"))
    assert not d.allowed
    assert d.http_status == 429
    assert d.reason == "principal_capacity_exceeded"


def test_user_quota_returns_429_user_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    key_id = select_key(policy, "gpt-4o-mini")
    d = guard.check(Request(Principal("agent:doc-ingestor", "agent", ("agent-dev",)), key_id, "gpt-4o-mini", "tenant:kogwistar", estimated_cost_usd=0.01, estimated_tokens=100, on_behalf_of_user_id="user:bob", token_id="low-user"))
    assert not d.allowed
    assert d.http_status == 429
    assert d.reason == "user_quota_exceeded"


def test_allowed_usage_appends_access_and_strict_usage_lane_and_projection(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    guard, policy = build_guard("config/gateway_policy.json")
    key_id = select_key(policy, "gpt-4o-mini")
    d = guard.check(Request(Principal("agent:doc-ingestor", "agent", ("agent-dev",)), key_id, "gpt-4o-mini", "tenant:kogwistar", estimated_cost_usd=0.001, estimated_tokens=100, on_behalf_of_user_id="user:alice", token_id="ok"))
    assert d.allowed
    guard.record_usage(d, estimated_cost_usd=0.001, actual_cost_usd=0.001, actual_tokens=100)
    store = guard.graph_state
    assert store is not None
    assert any(n.kind == "access_conversation_event" and n.payload["event_type"] == "MODEL_USAGE_RESULT" for n in store.nodes.values())
    head = store.nodes["usage_head:user:alice"]
    assert head.payload["seq"] == 1
    assert store.get_quota_used("principal", "agent:doc-ingestor", "hour")["tokens"] == 100
    assert store.get_quota_used("user", "user:alice", "hour")["tokens"] == 100
    assert store.get_quota_used("key", key_id, "hour")["tokens"] == 100


def test_denied_event_does_not_increment_quota_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    key_id = select_key(policy, "gpt-4o-mini")
    d = guard.check(Request(Principal("agent:external-scraper", "agent", ()), key_id, "gpt-4o-mini", "tenant:other", estimated_cost_usd=0.01, estimated_tokens=100, token_id="bad"))
    assert not d.allowed
    assert guard.graph_state.get_quota_used("key", key_id, "hour")["tokens"] == 0


def test_graph_payload_is_sealed_at_rest(tmp_path):
    path = tmp_path / "graph.jsonl"
    store = GraphStateStore(path, app_key="test-key")
    store.put_node("secret:test", "encrypted_secret_payload", {"provider_key": "sk-should-not-appear"})
    assert store.secret_payload_plaintext_is_not_stored("sk-should-not-appear")


def test_missing_policy_file_bootstraps_empty_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard(tmp_path / "does-not-exist.json")
    assert policy == {}
    assert guard.graph_state is not None
    # Base policy seed nodes still exist for graph invariants.
    assert "policy:version:0001" in guard.graph_state.nodes
    assert "issuer:keycloak:modelguard" in guard.graph_state.nodes


def test_blank_policy_file_bootstraps_token_verifier(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    blank = tmp_path / "blank_policy.json"
    blank.write_text("", encoding="utf-8")
    verifier = TokenVerifier(blank)
    with pytest.raises(TokenAuthError):
        verifier.verify_token("missing-token")
