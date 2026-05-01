from __future__ import annotations

import json
import pytest

from modelkeyguard.alert_rules import Alert, AlertEngine, AlertRule, LLMUsageReviewer, build_subject_contexts, redact_sensitive
from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.review_worker import review_once


def mk_policy(high_denial_count: int = 2) -> dict:
    return {
        "model_keys": [
            {
                "id": "key:openai:prod",
                "provider": "openai",
                "models": ["gpt-4o-mini"],
                "display_name": "OpenAI prod",
                "intended_use": "This key is for internal document summarization and Q&A workloads only. Do not use it for coding-agent tasks or any secret extraction attempt.",
            }
        ],
        "usage_profiles": {
            "agent:doc-ingestor": {"models": ["gpt-4o-mini"], "system_prompt_hashes": ["hash-good"], "description": "Summarize internal docs only"},
            "app:crm": {"models": ["gpt-4o-mini", "gpt-4.1-mini"], "description": "CRM support assistant only"},
            "service:batch-indexer": {"models": ["gpt-4o-mini"]},
        },
        "alert_rules": {"high_denial_count": high_denial_count},
    }


@pytest.fixture()
def policy() -> dict:
    return mk_policy()


@pytest.fixture()
def store(tmp_path) -> GraphStateStore:
    return GraphStateStore(tmp_path / "graph.jsonl", "test-alert-key-32-bytes-minimum")


@pytest.fixture(autouse=True)
def _alert_rules_extensive_test_env(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-alert-key-32-bytes-minimum")


def ev(**kwargs):
    base = {"event_type": "MODEL_CALL_DECISION", "request_id": "req-1", "decision": "ALLOWED", "reason": "allow", "principal_id": "agent:doc-ingestor", "on_behalf_of_user_id": "user:alice", "application_id": "app:crm", "key_id": "key:openai:prod", "model": "gpt-4o-mini", "system_prompt_hash": "hash-good", "estimated_tokens": 1000, "estimated_usd": 0.02}
    base.update(kwargs)
    return base


def alert_ids(alerts):
    return [a["rule_id"] for a in alerts]


@pytest.mark.parametrize("reason", ["principal_capacity_exceeded", "user_quota_exceeded", "key_quota_exceeded"])
def test_quota_alert_reasons_all_raise(store, policy, reason):
    assert "quota_exhausted" in alert_ids(AlertEngine(store).evaluate([ev(decision="BLOCKED", reason=reason)], policy))


@pytest.mark.parametrize("decision", ["BLOCKED", "DENIED"])
def test_denial_rate_accepts_blocked_and_denied_decisions(store, decision):
    alerts = AlertEngine(store).evaluate([ev(decision=decision, reason="permission_denied"), ev(decision=decision, reason="permission_denied", request_id="req-2")], mk_policy(2))
    assert "high_denial_rate" in alert_ids(alerts)


@pytest.mark.parametrize("reason", ["permission_denied", "namespace_denied", "auth_denied_by_policy"])
def test_denial_rate_accepts_denied_reason_text(store, reason):
    alerts = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason=reason), ev(decision="BLOCKED", reason=reason, request_id="req-2")], mk_policy(2))
    assert "high_denial_rate" in alert_ids(alerts)


@pytest.mark.parametrize("threshold,count,should_alert", [(1, 1, True), (2, 1, False), (2, 2, True), (4, 3, False), (4, 4, True)])
def test_high_denial_threshold_matrix(store, threshold, count, should_alert):
    events = [ev(decision="BLOCKED", reason="permission_denied", request_id=f"r{i}") for i in range(count)]
    assert ("high_denial_rate" in alert_ids(AlertEngine(store).evaluate(events, mk_policy(threshold)))) is should_alert


def test_high_denial_alert_payload_counts_and_top_reasons(store):
    events = [ev(decision="BLOCKED", reason="permission_denied", request_id="a"), ev(decision="BLOCKED", reason="permission_denied", request_id="b"), ev(decision="BLOCKED", reason="namespace_denied", request_id="c")]
    alerts = AlertEngine(store).evaluate(events, mk_policy(2))
    a = next(x for x in alerts if x["rule_id"] == "high_denial_rate" and x["subject_type"] == "principal")
    assert a["payload"]["denied_count"] == 3
    assert a["payload"]["top_reasons"][0][0] == "permission_denied"


def test_alerts_are_emitted_for_principal_user_application_and_key_contexts(store):
    events = [ev(decision="BLOCKED", reason="permission_denied", request_id="a"), ev(decision="BLOCKED", reason="permission_denied", request_id="b")]
    subjects = {(a["subject_type"], a["subject_id"]) for a in AlertEngine(store).evaluate(events, mk_policy(2)) if a["rule_id"] == "high_denial_rate"}
    assert {("principal", "agent:doc-ingestor"), ("user", "user:alice"), ("application", "app:crm"), ("key", "key:openai:prod")} <= subjects


def test_application_principal_prefix_is_reviewed_even_without_application_id(store, policy):
    reviews = LLMUsageReviewer(store).review([ev(principal_id="service:batch-indexer", application_id=None)], policy)
    assert any(r["subject_type"] == "application" and r["subject_id"] == "service:batch-indexer" for r in reviews)


def test_contexts_do_not_include_missing_optional_user_or_application(policy):
    subjects = {(c["subject_type"], c["subject_id"]) for c in build_subject_contexts([ev(on_behalf_of_user_id=None, application_id=None)], policy)}
    assert ("user", None) not in subjects and ("application", None) not in subjects


def test_prompt_mismatch_alert_collects_distinct_hashes(store, policy):
    events = [ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad-1", request_id="a"), ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad-2", request_id="b"), ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad-1", request_id="c")]
    a = next(x for x in AlertEngine(store).evaluate(events, policy) if x["rule_id"] == "system_prompt_signature_mismatch" and x["subject_type"] == "principal")
    assert a["payload"]["hashes"] == ["bad-1", "bad-2"] and a["payload"]["count"] == 3


def test_usage_profile_violation_does_not_run_for_user_context(store, policy):
    user_alerts = [a for a in AlertEngine(store).evaluate([ev(model="gpt-5.3")], policy) if a["rule_id"] == "usage_profile_model_violation" and a["subject_type"] == "user"]
    assert user_alerts == []


def test_usage_profile_violation_runs_for_application_context(store, policy):
    app_alert = next(a for a in AlertEngine(store).evaluate([ev(application_id="app:crm", model="gpt-5.3")], policy) if a["rule_id"] == "usage_profile_model_violation" and a["subject_type"] == "application")
    assert app_alert["payload"]["unexpected_models"] == ["gpt-5.3"]


def test_usage_profile_violation_ignores_subject_without_profile(store, policy):
    assert "usage_profile_model_violation" not in alert_ids(AlertEngine(store).evaluate([ev(principal_id="agent:other", application_id="app:other", model="gpt-5.3")], policy))


def test_multiple_unexpected_models_are_sorted_unique(store, policy):
    events = [ev(model="z-model", request_id="a"), ev(model="a-model", request_id="b"), ev(model="z-model", request_id="c")]
    a = next(x for x in AlertEngine(store).evaluate(events, policy) if x["rule_id"] == "usage_profile_model_violation" and x["subject_type"] == "principal")
    assert a["payload"]["unexpected_models"] == ["a-model", "z-model"]


def test_custom_action_runs_after_default_persist(store, policy):
    calls = []
    def condition(ctx, events, policy): return ctx["subject_type"] == "principal"
    def build(ctx, events, policy): return Alert("custom_action_order", "low", ctx["subject_type"], ctx["subject_id"], "custom", {})
    def action(alert, graph_state): calls.append((alert["rule_id"], any(n.kind == "alert" for n in graph_state.nodes.values())))
    AlertEngine(store, [AlertRule("custom_action_order", "custom", "low", condition, build, (action,))]).evaluate([ev()], policy)
    assert calls == [("custom_action_order", True)]


def test_custom_rule_can_be_evaluated_without_default_actions(tmp_path, policy):
    store = GraphStateStore(tmp_path / "graph.jsonl", "test-alert-key-32-bytes-minimum")
    def condition(ctx, events, policy): return ctx["subject_type"] == "principal"
    def build(ctx, events, policy): return Alert("no_persist", "low", ctx["subject_type"], ctx["subject_id"], "custom", {})
    alerts = AlertEngine(store, [AlertRule("no_persist", "custom", "low", condition, build)], default_actions=[]).evaluate([ev()], policy)
    assert alerts and not store.nodes


def test_action_callback_receives_redacted_alert_payload(store, policy):
    captured = []
    def condition(ctx, events, policy): return ctx["subject_type"] == "principal"
    def build(ctx, events, policy): return Alert("secret_payload_rule", "high", ctx["subject_type"], ctx["subject_id"], "custom", {"provider_secret": "sk-real-secret"})
    def action(alert, graph_state): captured.append(alert)
    AlertEngine(store, [AlertRule("secret_payload_rule", "custom", "high", condition, build, (action,))]).evaluate([ev()], policy)
    assert captured[0]["payload"]["provider_secret"] == "<redacted>"
    assert "sk-real-secret" not in store.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("key,value", [("provider_secret", "sk-live"), ("api_key", "plain"), ("Authorization", "Bearer abc"), ("refresh_token", "rt"), ("password", "pw"), ("nested", {"access_token": "tok"})])
def test_redact_sensitive_field_matrix(key, value):
    out = redact_sensitive({key: value})
    assert (out[key]["access_token"] if key == "nested" else out[key]) == "<redacted>"


@pytest.mark.parametrize("value", ["sk-live-abc", "kgw_sk_app_x", "Bearer abc", "api_key=abc"])
def test_redact_sensitive_value_matrix(value):
    assert redact_sensitive({"safe_field": value})["safe_field"] == "<redacted>"


def test_reviewer_payload_redacts_secrets_before_callback(store, policy):
    seen = []
    LLMUsageReviewer(store, lambda payload: seen.append(payload) or {"risk": "low"}).review([ev(provider_secret="sk-nope", authorization="Bearer nope")], policy)
    text = json.dumps(seen[0])
    assert "sk-nope" not in text and "Bearer nope" not in text and "<redacted>" in text


def test_reviewer_payload_caps_sample_at_twenty_events(store, policy):
    seen = []
    LLMUsageReviewer(store, lambda payload: seen.append(payload) or {"risk": "low"}).review([ev(request_id=f"r{i}") for i in range(30)], policy)
    principal_payload = next(p for p in seen if p["subject_type"] == "principal")
    assert principal_payload["event_count"] == 30 and len(principal_payload["sample"]) == 20


def test_reviewer_aggregates_decisions_reasons_and_models(store, policy):
    seen = []
    events = [ev(request_id="a"), ev(request_id="b", decision="BLOCKED", reason="user_quota_exceeded", model="gpt-5.3")]
    LLMUsageReviewer(store, lambda payload: seen.append(payload) or {"risk": "low"}).review(events, policy)
    agg = next(p for p in seen if p["subject_type"] == "principal")["aggregates"]
    assert ("ALLOWED", 1) in agg["decisions"] and ("BLOCKED", 1) in agg["decisions"] and ("user_quota_exceeded", 1) in agg["reasons"] and ("gpt-5.3", 1) in agg["models"]


@pytest.mark.parametrize("reason,expected_risk,expected_action", [("allow", "low", "continue_monitoring"), ("user_quota_exceeded", "medium", "review_quota_or_possible_token_leak"), ("principal_capacity_exceeded", "medium", "review_quota_or_possible_token_leak"), ("system_prompt_signature_mismatch", "high", "investigate_and_consider_revocation")])
def test_default_reviewer_risk_matrix(store, policy, reason, expected_risk, expected_action):
    principal = next(r for r in LLMUsageReviewer(store).review([ev(reason=reason, decision="BLOCKED" if reason != "allow" else "ALLOWED")], policy) if r["subject_type"] == "principal")
    assert principal["result"]["risk"] == expected_risk and principal["result"]["recommended_action"] == expected_action


def test_high_risk_prompt_mismatch_overrides_quota_medium(store, policy):
    principal = next(r for r in LLMUsageReviewer(store).review([ev(reason="user_quota_exceeded", decision="BLOCKED", request_id="a"), ev(reason="system_prompt_signature_mismatch", decision="BLOCKED", request_id="b")], policy) if r["subject_type"] == "principal")
    assert principal["result"]["risk"] == "high"


def test_reviewer_persists_one_node_and_event_per_review_context(store, policy):
    reviews = LLMUsageReviewer(store).review([ev()], policy)
    assert len([n for n in store.nodes.values() if n.kind == "llm_usage_review"]) == len(reviews)
    assert len([e for e in store.events if e["kind"] == "LLM_USAGE_REVIEWED"]) == len(reviews)


def test_review_callback_can_mark_revoke_recommendation(store, policy):
    reviews = LLMUsageReviewer(store, lambda payload: {"risk": "critical", "recommended_action": "revoke_safe_key", "subject": payload["subject_id"]}).review([ev()], policy)
    assert any(r["result"]["recommended_action"] == "revoke_safe_key" for r in reviews)


def test_review_once_writes_alerts_reviews_and_batch_event(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"; audit.write_text("\n".join(json.dumps(ev(request_id=str(i), decision="BLOCKED", reason="permission_denied")) for i in range(2)) + "\n", encoding="utf-8")
    pol = tmp_path / "policy.json"; pol.write_text(json.dumps(policy), encoding="utf-8")
    graph_path = tmp_path / "graph.jsonl"; monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path)); monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-alert-key-32-bytes-minimum")
    result = review_once(audit, pol, tmp_path / "review.jsonl")
    assert result["alerts"] and result["reviews"] and "MODEL_USAGE_REVIEW_BATCH_COMPLETED" in graph_path.read_text(encoding="utf-8")


def test_review_once_uses_latest_sample_window(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"
    rows = [ev(request_id=f"old-{i}", model="gpt-4o-mini") for i in range(3)] + [ev(request_id="new", model="gpt-5.3")]
    audit.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    pol = tmp_path / "policy.json"; pol.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    result = review_once(audit, pol, tmp_path / "review.jsonl", sample_size=1)
    assert result["events_seen"] == 1 and any(a["rule_id"] == "usage_profile_model_violation" for a in result["alerts"])


def test_alert_record_schema_is_stable(store, policy):
    a = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="user_quota_exceeded")], policy)[0]
    assert {"event_type", "ts", "rule_id", "severity", "subject_type", "subject_id", "reason", "payload"} <= set(a)
    assert a["event_type"] == "ALERT_RAISED"


def test_llm_review_record_schema_is_stable(store, policy):
    r = LLMUsageReviewer(store).review([ev()], policy)[0]
    assert {"event_type", "ts", "subject_type", "subject_id", "result"} <= set(r)
    assert r["event_type"] == "LLM_USAGE_REVIEWED"


def test_alert_engine_empty_events_returns_empty(store, policy):
    assert AlertEngine(store).evaluate([], policy) == []


def test_reviewer_empty_events_returns_empty(store, policy):
    assert LLMUsageReviewer(store).review([], policy) == []


def test_condition_sees_full_event_batch_not_only_subject_rows(store, policy):
    observed = []
    def condition(ctx, events, policy):
        if ctx["subject_type"] == "principal": observed.append(len(events))
        return False
    def build(ctx, events, policy): return Alert("never", "low", ctx["subject_type"], ctx["subject_id"], "never", {})
    AlertEngine(store, [AlertRule("observer", "observer", "low", condition, build)]).evaluate([ev(request_id="a"), ev(request_id="b", principal_id="agent:other")], policy)
    assert max(observed) == 2


def test_policy_object_is_available_to_condition_and_builder(store):
    policy = mk_policy(); policy["custom_flag"] = "yes"
    def condition(ctx, events, pol): return ctx["subject_type"] == "principal" and pol["custom_flag"] == "yes"
    def build(ctx, events, pol): return Alert("policy_seen", "low", ctx["subject_type"], ctx["subject_id"], pol["custom_flag"], {})
    assert AlertEngine(store, [AlertRule("policy_seen", "policy", "low", condition, build)]).evaluate([ev()], policy)[0]["reason"] == "yes"


def test_review_payload_includes_usage_profile_for_subject(store, policy):
    seen = []
    LLMUsageReviewer(store, lambda payload: seen.append(payload) or {"risk": "low"}).review([ev()], policy)
    payload = next(p for p in seen if p["subject_type"] == "principal")
    assert payload["usage_profile"]["description"] == "Summarize internal docs only"
    assert payload["key_usage_contracts"]["key:openai:prod"]["intended_use"].startswith("This key is for internal document summarization")


def test_review_payload_has_empty_profile_for_unknown_subject(store, policy):
    seen = []
    LLMUsageReviewer(store, lambda payload: seen.append(payload) or {"risk": "low"}).review([ev(principal_id="agent:unknown")], policy)
    assert next(p for p in seen if p["subject_type"] == "principal" and p["subject_id"] == "agent:unknown")["usage_profile"] == {}


def test_alerts_remain_graph_native_nodes_and_events(store, policy):
    AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="user_quota_exceeded")], policy)
    assert any(node.kind == "alert" for node in store.nodes.values()) and any(event["kind"] == "ALERT_RAISED" for event in store.events)


def test_alert_engine_can_chain_multiple_rules_for_same_subject(store):
    events = [ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad", model="gpt-5.3", request_id="a"), ev(decision="BLOCKED", reason="permission_denied", model="gpt-5.3", request_id="b")]
    assert {"high_denial_rate", "system_prompt_signature_mismatch", "usage_profile_model_violation"} <= set(alert_ids(AlertEngine(store).evaluate(events, mk_policy(2))))


def test_alert_engine_supports_no_rule_configuration(store, policy):
    assert AlertEngine(store, rules=[]).evaluate([ev(decision="BLOCKED", reason="user_quota_exceeded")], policy) == []


def test_reviewer_handles_events_without_model_field(store, policy):
    e = ev(); e.pop("model")
    assert LLMUsageReviewer(store).review([e], policy)


def test_reviewer_handles_non_string_decision_and_reason_values(store, policy):
    assert LLMUsageReviewer(store).review([ev(decision=None, reason=None)], policy)


def test_alert_top_reasons_limited_to_five(store):
    events = [ev(decision="BLOCKED", reason=f"reason_denied_{i}", request_id=str(i)) for i in range(7)]
    a = next(x for x in AlertEngine(store).evaluate(events, mk_policy(2)) if x["rule_id"] == "high_denial_rate" and x["subject_type"] == "principal")
    assert len(a["payload"]["top_reasons"]) == 5


def test_review_result_findings_include_quota_text(store, policy):
    review = next(r for r in LLMUsageReviewer(store).review([ev(reason="user_quota_exceeded", decision="BLOCKED")], policy) if r["subject_type"] == "principal")
    assert "quota or capacity exceeded" in review["result"]["findings"]


def test_review_result_findings_include_prompt_mismatch_text(store, policy):
    review = next(r for r in LLMUsageReviewer(store).review([ev(reason="system_prompt_signature_mismatch", decision="BLOCKED")], policy) if r["subject_type"] == "principal")
    assert "system prompt signature mismatch observed" in review["result"]["findings"]


def test_review_output_file_appends_batches(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"; audit.write_text(json.dumps(ev()) + "\n", encoding="utf-8")
    pol = tmp_path / "policy.json"; pol.write_text(json.dumps(policy), encoding="utf-8")
    out = tmp_path / "reviews.jsonl"; monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    review_once(audit, pol, out); review_once(audit, pol, out)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_alerts_do_not_store_bearer_tokens_from_custom_payload(store, policy):
    def condition(ctx, events, policy): return ctx["subject_type"] == "principal"
    def build(ctx, events, policy): return Alert("bearer_leak_check", "high", ctx["subject_type"], ctx["subject_id"], "custom", {"Authorization": "Bearer secret-token"})
    alerts = AlertEngine(store, [AlertRule("bearer_leak_check", "custom", "high", condition, build)]).evaluate([ev()], policy)
    assert alerts[0]["payload"]["Authorization"] == "<redacted>" and "secret-token" not in store.path.read_text(encoding="utf-8")


def test_review_payload_does_not_mutate_original_event(store, policy):
    event = ev(provider_secret="sk-still-in-original")
    LLMUsageReviewer(store, lambda payload: {"risk": "low"}).review([event], policy)
    assert event["provider_secret"] == "sk-still-in-original"


def test_context_grouping_keeps_rows_isolated_by_subject(policy):
    contexts = build_subject_contexts([ev(principal_id="agent:a", request_id="a"), ev(principal_id="agent:b", request_id="b")], policy)
    ctx = {c["subject_id"]: c for c in contexts if c["subject_type"] == "principal"}
    assert [r["request_id"] for r in ctx["agent:a"]["events"]] == ["a"]
    assert [r["request_id"] for r in ctx["agent:b"]["events"]] == ["b"]


def test_quota_alert_payload_top_reasons_limited_to_five(store, policy):
    reasons = ["principal_capacity_exceeded", "user_quota_exceeded", "key_quota_exceeded"]
    a = next(x for x in AlertEngine(store).evaluate([ev(decision="BLOCKED", reason=reasons[i % 3], request_id=str(i)) for i in range(20)], policy) if x["rule_id"] == "quota_exhausted" and x["subject_type"] == "principal")
    assert len(a["payload"]["top_reasons"]) <= 5


def test_llm_reviewer_reviews_principal_user_and_application_but_not_key(store, policy):
    types = {r["subject_type"] for r in LLMUsageReviewer(store).review([ev()], policy)}
    assert {"principal", "user", "application"} <= types and "key" not in types


def test_high_denial_does_not_count_allowed_events_with_allowed_reason(store):
    assert "high_denial_rate" not in alert_ids(AlertEngine(store).evaluate([ev(decision="ALLOWED", reason="allow"), ev(decision="ALLOWED", reason="allow", request_id="b")], mk_policy(1)))


def test_denied_text_in_reason_counts_even_if_decision_missing(store):
    e1 = ev(reason="permission_denied"); e2 = ev(reason="namespace_denied", request_id="b"); e1.pop("decision"); e2.pop("decision")
    assert "high_denial_rate" in alert_ids(AlertEngine(store).evaluate([e1, e2], mk_policy(2)))


def test_rule_action_callback_can_append_followup_event(store, policy):
    def condition(ctx, events, policy): return ctx["subject_type"] == "principal"
    def build(ctx, events, policy): return Alert("followup", "low", ctx["subject_type"], ctx["subject_id"], "custom", {})
    def action(alert, graph_state): graph_state.append_event("CUSTOM_ACTION_CALLED", alert["subject_id"], {"rule_id": alert["rule_id"]})
    AlertEngine(store, [AlertRule("followup", "custom", "low", condition, build, (action,))]).evaluate([ev()], policy)
    assert any(e["kind"] == "CUSTOM_ACTION_CALLED" for e in store.events)
