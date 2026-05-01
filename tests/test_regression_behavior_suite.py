from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modelkeyguard.core import ModelKey, ModelKeyGuard, Principal, Request
from modelkeyguard.gateway import (
    build_guard,
    estimate_cost_and_tokens,
    extract_system_prompt,
    resolve_secret,
    select_key,
    sha256_text,
)
from modelkeyguard.graph_state import GraphStateStore, period_bucket
from modelkeyguard.kogwistar_acl_adapter import MiniACLGraph, load_acl_graph
from modelkeyguard.sealed_payload import open_json, seal_json
from modelkeyguard.services.usage_ops import build_usage_monitor_dataset
from modelkeyguard.token_auth import TokenAuthError, TokenVerifier

POLICY = "config/gateway_policy.json"
SYSTEM_PROMPT = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


@pytest.fixture(autouse=True)
def isolated_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-graph-key")
    monkeypatch.delenv("KOGWISTAR_REPO", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_USE_INSTALLED_KOGWISTAR", raising=False)
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")


def _guard():
    return build_guard(POLICY)


def _key(policy: dict, model: str = "gpt-4o-mini") -> str:
    out = select_key(policy, model)
    assert out is not None
    return out


def _ok_request(policy: dict, **overrides):
    data = dict(
        principal=Principal("agent:doc-ingestor", "agent", ("agent-dev",)),
        key_id=_key(policy),
        model="gpt-4o-mini",
        namespace="tenant:kogwistar",
        estimated_cost_usd=0.001,
        estimated_tokens=100,
        request_id="req-ok",
        token_id="local-doc-ingestor",
        on_behalf_of_user_id="user:alice",
    )
    data.update(overrides)
    return Request(**data)


# ---------------------------------------------------------------------------
# Sealed graph payload regression tests.
# ---------------------------------------------------------------------------

def test_001_sealed_payload_round_trips_nested_json():
    payload = {"a": 1, "nested": {"b": [1, 2, 3]}, "secret": "sk-test"}
    sealed = seal_json(payload, "key-a")
    assert open_json(sealed, "key-a") == payload


def test_002_sealed_payload_never_contains_plaintext_secret():
    sealed = seal_json({"secret": "sk-plaintext-forbidden"}, "key-a")
    assert "sk-plaintext-forbidden" not in json.dumps(sealed)


def test_003_sealed_payload_rejects_wrong_key():
    sealed = seal_json({"secret": "x"}, "key-a")
    with pytest.raises(ValueError):
        open_json(sealed, "key-b")


def test_003a_sealed_payload_sentinel_check_runs_before_encrypt(monkeypatch):
    def broken_open(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("modelkeyguard.sealed_payload._open_json_impl", broken_open)

    with pytest.raises(ValueError, match="sentinel payload did not decrypt to the expected text"):
        seal_json({"secret": "x"}, "key-a")


def test_004_sealed_payload_rejects_tampered_ciphertext():
    sealed = seal_json({"secret": "x"}, "key-a")
    sealed = dict(sealed)
    sealed["ciphertext"] = ("A" if sealed["ciphertext"][0] != "A" else "B") + sealed["ciphertext"][1:]
    with pytest.raises(ValueError):
        open_json(sealed, "key-a")


def test_005_sealed_payload_rejects_tampered_tag():
    sealed = seal_json({"secret": "x"}, "key-a")
    sealed = dict(sealed)
    if "tag" in sealed:
        sealed["tag"] = ("A" if sealed["tag"][0] != "A" else "B") + sealed["tag"][1:]
    else:
        sealed["aad"] = ("A" if sealed["aad"][0] != "A" else "B") + sealed["aad"][1:]
    with pytest.raises(ValueError):
        open_json(sealed, "key-a")


def test_006_sealed_payload_has_required_fields():
    sealed = seal_json({"x": 1}, "key-a")
    assert {"alg", "nonce", "ciphertext"}.issubset(sealed)
    assert sealed["alg"] in {"AES-256-GCM", "KGW-HMAC-XOR-fallback"}
    if sealed["alg"] == "AES-256-GCM":
        assert {"aad"}.issubset(sealed)
    else:
        assert {"aad", "tag"}.issubset(sealed)


def test_007_sealed_payload_nonce_changes_for_same_plaintext():
    a = seal_json({"x": 1}, "key-a")
    b = seal_json({"x": 1}, "key-a")
    assert a["nonce"] != b["nonce"]
    assert a["ciphertext"] != b["ciphertext"]


def test_008_sealed_payload_b64_fields_are_urlsafe():
    sealed = seal_json({"x": 1}, "key-a")
    fields = ["nonce", "ciphertext"]
    if "aad" in sealed:
        fields.append("aad")
    if "tag" in sealed:
        fields.append("tag")
    for name in fields:
        base64.urlsafe_b64decode(sealed[name] + "=" * (-len(sealed[name]) % 4))


def test_009_sealed_payload_empty_key_is_rejected():
    with pytest.raises(ValueError):
        seal_json({"x": 1}, "")


def test_010_graph_store_seals_node_edge_event_projection_payloads(tmp_path):
    path = tmp_path / "graph.jsonl"
    store = GraphStateStore(path, app_key="test-key")
    store.put_node("n", "kind", {"secret": "node-secret"})
    store.put_edge("e", "REL", "n", "m", {"secret": "edge-secret"})
    store.append_event("EV", "n", {"secret": "event-secret"})
    store.put_projection("p", {"secret": "projection-secret"})
    text = path.read_text()
    for forbidden in ["node-secret", "edge-secret", "event-secret", "projection-secret"]:
        assert forbidden not in text


# ---------------------------------------------------------------------------
# Period bucket behavior.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("10s", "2026-04-25T12:34:50+00:00"),
        ("hour", "2026-04-25T12:00:00+00:00"),
        ("day", "2026-04-25"),
        ("week", "2026-04-20"),
        ("month", "2026-04"),
        ("infinite", "lifetime"),
    ],
)
def test_011_period_bucket_supported_periods(period, expected):
    ts = datetime(2026, 4, 25, 12, 34, 56, tzinfo=timezone.utc)
    assert period_bucket(ts, period) == expected


def test_012_period_bucket_10s_lower_bound():
    ts = datetime(2026, 4, 25, 12, 34, 0, tzinfo=timezone.utc)
    assert period_bucket(ts, "10s") == "2026-04-25T12:34:00+00:00"


def test_013_period_bucket_10s_middle_rounds_down():
    ts = datetime(2026, 4, 25, 12, 34, 19, tzinfo=timezone.utc)
    assert period_bucket(ts, "10s") == "2026-04-25T12:34:10+00:00"


def test_014_period_bucket_rejects_unknown_period():
    with pytest.raises(ValueError):
        period_bucket(datetime(2026, 4, 25, tzinfo=timezone.utc), "quarter")


def test_014b_period_bucket_lifetime_alias_maps_to_infinite():
    ts = datetime(2026, 4, 25, 12, 34, 56, tzinfo=timezone.utc)
    assert period_bucket(ts, "lifetime") == "lifetime"


# ---------------------------------------------------------------------------
# ACL adapter contract.
# ---------------------------------------------------------------------------

def _acl_with(mode: str, **kwargs):
    acl = MiniACLGraph()
    acl.add_record(
        truth_graph="model_keys",
        entity_id="key:openai:prod",
        grain="node",
        version=1,
        mode=mode,
        created_by="human:admin",
        owner_id=kwargs.pop("owner_id", "human:admin"),
        security_scope=kwargs.pop("security_scope", "tenant:kogwistar"),
        shared_with_principals=kwargs.pop("shared_with_principals", ("agent:doc",)),
        shared_with_groups=kwargs.pop("shared_with_groups", ("agent-dev",)),
        **kwargs,
    )
    return acl


def test_015_acl_public_allows_anyone():
    d = _acl_with("public").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="x")
    assert d.visible and d.reason == "public"


def test_016_acl_owner_override_allows_private_owner():
    d = _acl_with("private").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="human:admin")
    assert d.visible and d.reason == "owner"


def test_017_acl_private_blocks_non_owner():
    d = _acl_with("private").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:doc")
    assert not d.visible and d.reason == "private"


def test_018_acl_scope_allows_matching_namespace():
    d = _acl_with("scope").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:doc", security_scope="tenant:kogwistar")
    assert d.visible and d.reason == "scope_match"


def test_019_acl_scope_blocks_wrong_namespace():
    d = _acl_with("scope").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:doc", security_scope="tenant:other")
    assert not d.visible and d.reason == "scope_mismatch"


def test_020_acl_shared_allows_listed_principal():
    d = _acl_with("shared").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:doc")
    assert d.visible and d.reason == "principal_share"


def test_021_acl_shared_blocks_unlisted_principal():
    d = _acl_with("shared").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:other")
    assert not d.visible and d.reason == "not_shared"


def test_022_acl_group_allows_matching_group():
    d = _acl_with("group").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:other", principal_groups=("agent-dev",))
    assert d.visible and d.reason == "group_share"


def test_023_acl_group_blocks_missing_group():
    d = _acl_with("group").decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="agent:other", principal_groups=("finance",))
    assert not d.visible and d.reason == "not_shared"


def test_024_acl_missing_record_denies():
    d = MiniACLGraph().decide(truth_graph="model_keys", entity_id="missing", principal_id="agent:doc")
    assert not d.visible and d.reason == "no_acl_record"


def test_025_acl_latest_record_wins_by_version():
    acl = _acl_with("private")
    acl.add_record(truth_graph="model_keys", entity_id="key:openai:prod", grain="node", version=2, mode="public", created_by="human:admin")
    d = acl.decide(truth_graph="model_keys", entity_id="key:openai:prod", principal_id="any")
    assert d.visible and d.reason == "public"


def test_026_load_acl_graph_defaults_to_standalone_without_importing_package(monkeypatch):
    monkeypatch.delenv("KOGWISTAR_REPO", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_USE_INSTALLED_KOGWISTAR", raising=False)
    graph, info = load_acl_graph()
    assert isinstance(graph, MiniACLGraph)
    assert info.backend == "compat"


def test_026b_load_acl_graph_ignores_reference_repo_env(monkeypatch):
    monkeypatch.setenv("KOGWISTAR_REPO", "/tmp/reference-only-not-runtime")
    monkeypatch.delenv("MODELKEYGUARD_USE_INSTALLED_KOGWISTAR", raising=False)
    graph, info = load_acl_graph()
    assert isinstance(graph, MiniACLGraph)
    assert info.backend == "compat"


def test_026c_load_acl_graph_enforces_installed_only_guard(monkeypatch):
    import modelkeyguard.kogwistar_acl_adapter as adapter

    def _fail_guard() -> None:
        raise RuntimeError("repository-local path import is forbidden")

    monkeypatch.setenv("MODELKEYGUARD_USE_INSTALLED_KOGWISTAR", "1")
    monkeypatch.setattr(adapter, "enforce_installed_kogwistar_only", _fail_guard)
    graph, info = adapter.load_acl_graph()
    assert isinstance(graph, MiniACLGraph)
    assert info.backend == "compat"
    assert "repository-local path import is forbidden" in info.detail


def test_026d_kogwistar_postgres_acl_path_has_no_compat_fallback(monkeypatch):
    import modelkeyguard.kogwistar_acl_adapter as adapter

    def _fail_guard() -> None:
        raise RuntimeError("bad install path")

    monkeypatch.setenv("MODELKEYGUARD_STORE", "kogwistar_postgres")
    monkeypatch.setattr(adapter, "enforce_installed_kogwistar_only", _fail_guard)
    with pytest.raises(RuntimeError, match="kogwistar_postgres requires installed Kogwistar ACLGraph"):
        adapter.load_acl_graph()


# ---------------------------------------------------------------------------
# Graph state, usage lane, projection contract.
# ---------------------------------------------------------------------------

def test_027_from_policy_creates_single_policy_version_node():
    guard, _ = _guard()
    assert "policy:version:0001" in guard.graph_state.nodes


def test_028_from_policy_creates_principal_nodes():
    guard, _ = _guard()
    assert guard.graph_state.nodes["agent:doc-ingestor"].kind == "principal"
    assert guard.graph_state.nodes["agent:batch-heavy"].kind == "principal"


def test_029_from_policy_creates_user_quota_nodes():
    guard, _ = _guard()
    assert any(n.kind == "quota_policy" and n.payload.get("lane") == "user" for n in guard.graph_state.nodes.values())


def test_030_from_policy_creates_principal_quota_edges():
    guard, _ = _guard()
    edges = list(guard.graph_state.edges_from("agent:doc-ingestor", "HAS_QUOTA_POLICY"))
    assert {guard.graph_state.nodes[e.target].payload["period"] for e in edges} >= {"10s", "hour"}


def test_031_from_policy_creates_key_quota_edges():
    guard, policy = _guard()
    edges = list(guard.graph_state.edges_from(_key(policy), "HAS_QUOTA_POLICY"))
    assert {guard.graph_state.nodes[e.target].payload["period"] for e in edges} >= {"hour", "month"}


def test_032_from_policy_creates_token_to_principal_edge():
    guard, _ = _guard()
    assert guard.graph_state.get_token_principal_id("token:local-doc-ingestor") == "agent:doc-ingestor"


def test_033_append_usage_lane_first_event_creates_head_and_first_edge():
    store = GraphStateStore()
    usage_id = store.append_usage_ledger_event("user:alice", {"actual_tokens": 10})
    assert store.nodes["usage_head:user:alice"].payload["tail"] == usage_id
    assert any(e.kind == "FIRST_USAGE" for e in store.edges.values())


def test_034_append_usage_lane_second_event_creates_next_edge():
    store = GraphStateStore()
    first = store.append_usage_ledger_event("user:alice", {"actual_tokens": 10})
    second = store.append_usage_ledger_event("user:alice", {"actual_tokens": 20})
    assert store.nodes["usage_head:user:alice"].payload["tail"] == second
    assert f"edge:{first}:NEXT_USAGE:{second}" in store.edges


def test_035_append_usage_lane_sequence_is_strictly_linear():
    store = GraphStateStore()
    ids = [store.append_usage_ledger_event("user:alice", {"i": i}) for i in range(3)]
    assert [store.nodes[i].payload["seq"] for i in ids] == [1, 2, 3]
    assert len([e for e in store.edges.values() if e.kind == "NEXT_USAGE"]) == 2


def test_036_quota_projection_accumulates_tokens_usd_and_requests():
    store = GraphStateStore()
    store.add_quota_usage("user", "user:alice", "hour", 0.1, 100)
    store.add_quota_usage("user", "user:alice", "hour", 0.2, 200)
    used = store.get_quota_used("user", "user:alice", "hour")
    assert used == {"usd": 0.3, "tokens": 300.0, "requests": 2.0}


def test_037_quota_projection_is_bucket_specific():
    store = GraphStateStore()
    t1 = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    store.add_quota_usage("user", "user:alice", "hour", 0.1, 100, when=t1)
    assert store.get_quota_used("user", "user:alice", "hour", when=t1)["tokens"] == 100
    assert store.get_quota_used("user", "user:alice", "hour", when=t2)["tokens"] == 0


def test_038_graph_state_load_reconstructs_nodes_edges_events_and_projections(tmp_path):
    path = tmp_path / "graph.jsonl"
    s1 = GraphStateStore(path, app_key="k")
    s1.put_node("n", "kind", {"x": 1})
    s1.put_edge("e", "REL", "n", "m", {"y": 2})
    s1.append_event("EV", "n", {"z": 3})
    s1.put_projection("p", {"w": 4})
    s2 = GraphStateStore(path, app_key="k")
    assert s2.nodes["n"].payload == {"x": 1}
    assert s2.edges["e"].payload == {"y": 2}
    assert s2.events[0]["payload"] == {"z": 3}
    assert s2.projections["p"] == {"w": 4}


def test_039_graph_state_wrong_key_cannot_load(tmp_path):
    path = tmp_path / "graph.jsonl"
    GraphStateStore(path, app_key="good").put_node("n", "kind", {"x": 1})
    with pytest.raises(ValueError):
        GraphStateStore(path, app_key="bad")


def test_040_access_conversation_event_creates_node_and_event():
    store = GraphStateStore()
    out = store.append_access_conversation_event("req-1", "ACL_DECISION_DENY", {"reason": "permission_denied"})
    assert out["event_type"] == "ACL_DECISION_DENY"
    assert any(n.kind == "access_conversation_event" for n in store.nodes.values())
    assert any(e["kind"] == "ACL_DECISION_DENY" for e in store.events)


# ---------------------------------------------------------------------------
# ModelKeyGuard decision behavior.
# ---------------------------------------------------------------------------

def test_041_check_allows_happy_path():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy))
    assert d.allowed and d.http_status == 200 and d.reason == "allow"


def test_042_check_denies_unknown_key():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, key_id="key:missing"))
    assert not d.allowed and d.http_status == 403 and d.reason == "model_key_not_registered"


def test_043_check_denies_model_not_on_key():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, model="not-a-model"))
    assert not d.allowed and d.http_status == 403 and d.reason == "model_not_allowed_for_key"


def test_044_check_denies_wrong_namespace_permission():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, namespace="tenant:other"))
    assert not d.allowed and d.http_status == 403 and d.reason == "permission_denied"


def test_045_check_approval_required_at_threshold():
    guard, policy = _guard()
    key_id = _key(policy)
    original = guard.keys[key_id]
    guard.keys[key_id] = ModelKey(original.id, original.provider, original.models, original.secret_ref, original.display_name, approval_threshold_usd=0.0005)
    d = guard.check(_ok_request(policy, estimated_cost_usd=0.001, estimated_tokens=100))
    assert not d.allowed and d.requires_approval and d.http_status == 202


def test_046_check_principal_quota_exceeded_is_429():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, principal=Principal("agent:batch-heavy", "agent", ("agent-dev",))))
    assert not d.allowed and d.http_status == 429 and d.reason == "principal_capacity_exceeded"


def test_047_check_user_quota_exceeded_is_429():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, on_behalf_of_user_id="user:bob"))
    assert not d.allowed and d.http_status == 429 and d.reason == "user_quota_exceeded"


def test_048_check_key_quota_exceeded_is_429():
    guard, policy = _guard()
    # Pre-consume almost all hourly key quota, then ask for more.
    guard.graph_state.add_quota_usage("key", _key(policy), "hour", 9.99999, 1)
    d = guard.check(_ok_request(policy, estimated_cost_usd=0.01, estimated_tokens=1))
    assert not d.allowed and d.http_status == 429 and d.reason == "key_quota_exceeded"


def test_049_record_usage_noops_for_denied_decision():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, namespace="tenant:other"))
    guard.record_usage(d, 0.1, 0.1, 100)
    assert guard.graph_state.get_quota_used("key", _key(policy), "hour")["requests"] == 0


def test_050_record_usage_updates_all_applicable_lanes():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, request_id="req-usage"))
    guard.record_usage(d, 0.001, 0.002, 321)
    assert guard.graph_state.get_quota_used("principal", "agent:doc-ingestor", "hour")["tokens"] == 321
    assert guard.graph_state.get_quota_used("user", "user:alice", "hour")["tokens"] == 321
    assert guard.graph_state.get_quota_used("key", _key(policy), "hour")["tokens"] == 321


def test_051_record_usage_for_no_user_updates_principal_lane_as_usage_lane():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, on_behalf_of_user_id=None, request_id="req-no-user"))
    guard.record_usage(d, 0.001, 0.001, 50)
    assert "usage_head:agent:doc-ingestor" in guard.graph_state.nodes


def test_052_remaining_contains_principal_user_and_key_lanes():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy))
    assert f"principal:agent:doc-ingestor" in d.remaining
    assert f"user:user:alice" in d.remaining
    assert f"key:{_key(policy)}" in d.remaining


def test_053_request_id_is_generated_when_missing():
    guard, policy = _guard()
    req = _ok_request(policy, request_id="")
    d = guard.check(req)
    assert d.request_id


def test_054_token_id_is_preserved_in_decision():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, token_id="token-special"))
    assert d.token_id == "token-special"


def test_055_deny_event_is_appended_for_unknown_key():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, key_id="key:missing", request_id="req-deny"))
    assert not d.allowed
    assert any(n.payload.get("event_type") == "ACL_DECISION_DENY" and n.payload.get("request_id") == "req-deny" for n in guard.graph_state.nodes.values())


def test_056_allow_event_is_appended_for_allowed_request():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, request_id="req-allow"))
    assert d.allowed
    assert any(n.payload.get("event_type") == "ACL_DECISION_ALLOW" and n.payload.get("request_id") == "req-allow" for n in guard.graph_state.nodes.values())


def test_057_approval_event_is_appended_for_threshold_request():
    guard, policy = _guard()
    key_id = _key(policy)
    original = guard.keys[key_id]
    guard.keys[key_id] = ModelKey(original.id, original.provider, original.models, original.secret_ref, original.display_name, approval_threshold_usd=0.0005)
    d = guard.check(_ok_request(policy, estimated_cost_usd=0.001, request_id="req-approval"))
    assert d.requires_approval
    assert any(n.payload.get("event_type") == "ACL_DECISION_APPROVAL_REQUIRED" for n in guard.graph_state.nodes.values())


# ---------------------------------------------------------------------------
# Token verifier behavior.
# ---------------------------------------------------------------------------

def test_058_verify_local_doc_ingestor_token():
    token = TokenVerifier(POLICY).verify_token("kgw_demo_doc_ingestor")
    assert token.principal_id == "agent:doc-ingestor"
    assert token.on_behalf_of_user_id == "user:alice"


def test_059_verify_low_user_token_maps_user_bob():
    token = TokenVerifier(POLICY).verify_token("kgw_demo_low_user")
    assert token.principal_id == "agent:doc-ingestor"
    assert token.on_behalf_of_user_id == "user:bob"


def test_060_verify_principal_busy_token_maps_batch_agent():
    token = TokenVerifier(POLICY).verify_token("kgw_demo_principal_busy")
    assert token.principal_id == "agent:batch-heavy"


def test_061_verify_external_token_maps_other_namespace():
    token = TokenVerifier(POLICY).verify_token("kgw_demo_external")
    assert token.principal_id == "agent:external-scraper"
    assert token.namespace == "tenant:other"


def test_062_authorization_header_requires_bearer():
    with pytest.raises(TokenAuthError, match="missing_bearer_token"):
        TokenVerifier(POLICY).verify_authorization_header(None)


def test_063_authorization_header_accepts_bearer_case_insensitive():
    token = TokenVerifier(POLICY).verify_authorization_header("bEaReR kgw_demo_doc_ingestor")
    assert token.principal_id == "agent:doc-ingestor"


def test_064_invalid_token_is_rejected():
    with pytest.raises(TokenAuthError, match="invalid_or_inactive_token"):
        TokenVerifier(POLICY).verify_token("not-valid")


def test_065_expired_local_token_is_rejected(tmp_path):
    policy = json.loads(Path(POLICY).read_text())
    policy["local_tokens"] = {
        "expired": {"principal_id": "agent:doc-ingestor", "kind": "agent", "groups": [], "namespace": "tenant:kogwistar", "expires_at_epoch": int(time.time()) - 1}
    }
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy))
    with pytest.raises(TokenAuthError, match="local_token_expired"):
        TokenVerifier(p).verify_token("expired")


def test_066_require_keycloak_allows_local_fallback_when_keycloak_unavailable(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_REQUIRE_KEYCLOAK", "1")
    token = TokenVerifier(POLICY).verify_token("kgw_demo_doc_ingestor")
    assert token.principal_id == "agent:doc-ingestor"


# ---------------------------------------------------------------------------
# Gateway helper behavior.
# ---------------------------------------------------------------------------

def test_067_select_key_finds_each_configured_model():
    policy = json.loads(Path(POLICY).read_text())
    for model in ["gpt-4o-mini", "gpt-5.3-mini", "gpt-5.3"]:
        assert select_key(policy, model) == "key:openai:prod"


def test_068_select_key_returns_none_for_unknown_model():
    policy = json.loads(Path(POLICY).read_text())
    assert select_key(policy, "unknown-model") is None


def test_069_estimate_cost_uses_configured_price():
    policy = json.loads(Path(POLICY).read_text())
    cost, tokens = estimate_cost_and_tokens({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}, policy)
    assert tokens >= 100
    assert cost == round((tokens / 1000.0) * 0.001, 6)


def test_070_estimate_cost_defaults_unknown_model_price():
    policy = json.loads(Path(POLICY).read_text())
    cost, tokens = estimate_cost_and_tokens({"model": "unknown", "messages": [], "max_tokens": 10}, policy)
    assert cost == round((tokens / 1000.0) * 0.002, 6)


def test_071_extract_system_prompt_concatenates_only_system_messages():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "B"},
        {"role": "system", "content": "C"},
    ]
    assert extract_system_prompt(messages) == "A\nC"


def test_072_sha256_text_matches_known_value():
    assert sha256_text("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_073_resolve_secret_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert resolve_secret("env://OPENAI_API_KEY") == "sk-from-env"


def test_074_resolve_secret_returns_none_for_missing_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_secret("env://OPENAI_API_KEY") is None


def test_075_resolve_secret_returns_none_for_unknown_scheme():
    assert resolve_secret("vault://secret") is None


def test_076_system_prompt_hash_in_policy_matches_expected_prompt():
    policy = json.loads(Path(POLICY).read_text())
    expected_hashes = policy["usage_profiles"]["agent:doc-ingestor"]["system_prompt_hashes"]
    assert sha256_text(SYSTEM_PROMPT) in expected_hashes


# ---------------------------------------------------------------------------
# Regression pinning around graph-native invariants.
# ---------------------------------------------------------------------------

def test_077_policy_access_usage_are_one_store_not_multiple_engines():
    guard, _ = _guard()
    store = guard.graph_state
    assert store.nodes["policy:version:0001"].kind == "policy_version"
    guard.check(_ok_request(_guard()[1], request_id="req-one-store"))
    assert any(n.kind == "access_conversation_event" for n in store.nodes.values())


def test_078_denied_request_does_not_create_usage_lane():
    guard, policy = _guard()
    guard.check(_ok_request(policy, namespace="tenant:other", request_id="req-denied-no-usage"))
    assert not any(n.kind == "usage_lane_head" for n in guard.graph_state.nodes.values())


def test_079_usage_result_references_request_id_token_principal_key_and_namespace():
    guard, policy = _guard()
    d = guard.check(_ok_request(policy, request_id="req-ref"))
    guard.record_usage(d, 0.001, 0.001, 100)
    events = [n.payload for n in guard.graph_state.nodes.values() if n.payload.get("event_type") == "MODEL_USAGE_RESULT"]
    assert events
    e = events[-1]
    assert e["request_id"] == "req-ref"
    assert e["principal_id"] == "agent:doc-ingestor"
    assert e["key_id"] == _key(policy)
    assert e["namespace"] == "tenant:kogwistar"
    assert e["token_id"] == "local-doc-ingestor"


def test_080_sequential_usage_ids_sanitize_subject_colons():
    store = GraphStateStore()
    usage_id = store.append_usage_ledger_event("user:alice", {})
    assert usage_id.startswith("usage:user_alice:")


def test_081_projection_id_contains_lane_subject_period_bucket():
    store = GraphStateStore()
    pid = store.quota_projection_id("principal", "agent:doc-ingestor", "hour", "2026-04-25T12:00:00+00:00")
    assert pid == "quota_projection:principal:agent:doc-ingestor:hour:2026-04-25T12:00:00+00:00"


def test_082_no_plaintext_policy_values_in_jsonl_graph_file(tmp_path):
    path = tmp_path / "graph.jsonl"
    policy = json.loads(Path(POLICY).read_text())
    GraphStateStore.from_policy(policy, path=path, app_key="policy-key")
    text = path.read_text()
    assert "Alice Example" not in text
    assert "kgw_demo_doc_ingestor" not in text


def test_083_usage_monitor_totals_are_event_cost_based_not_retroactive_policy_price():
    now = datetime.now(timezone.utc)
    events = [
        {
            "ts": now.replace(minute=0, second=10, microsecond=0).isoformat(),
            "request_id": "pre-price-change",
            "decision": "ALLOWED",
            "principal_id": "app:billing-demo",
            "on_behalf_of_user_id": "user:billing-demo",
            "key_id": "key:azure:billing",
            "token_id": "tok-1",
            "model": "gpt-5-mini",
            "estimated_tokens": 1000,
            "estimated_cost_usd": 0.010,  # old price phase
        },
        {
            "ts": now.replace(minute=10, second=10, microsecond=0).isoformat(),
            "request_id": "post-price-change",
            "decision": "ALLOWED",
            "principal_id": "app:billing-demo",
            "on_behalf_of_user_id": "user:billing-demo",
            "key_id": "key:azure:billing",
            "token_id": "tok-1",
            "model": "gpt-5-mini",
            "estimated_tokens": 1000,
            "estimated_cost_usd": 0.020,  # new price phase
        },
    ]
    # Current policy reflects only the new price, but historical totals must
    # still sum event-recorded costs (0.01 + 0.02), not recompute as 0.04.
    policy = {"model_price_per_1k_tokens_usd": {"gpt-5-mini": 0.020}}
    dataset = build_usage_monitor_dataset(events, policy, time_range="24h", bucket="hour")
    assert dataset["overview"]["tokens"] == 2000
    assert dataset["overview"]["usd"] == 0.03
