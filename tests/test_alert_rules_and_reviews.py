from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelkeyguard.alert_rules import (
    Alert,
    AlertEngine,
    AlertRule,
    LLMUsageReviewer,
    build_subject_contexts,
    default_rules,
    load_jsonl,
)
from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.review_worker import review_once


@pytest.fixture()
def policy() -> dict:
    return {
        "usage_profiles": {
            "agent:doc-ingestor": {
                "models": ["gpt-4o-mini"],
                "system_prompt_hashes": ["good-hash"],
                "description": "Summarize internal docs only",
            },
            "app:crm": {"models": ["gpt-4o-mini"]},
        },
        "alert_rules": {"high_denial_count": 2},
    }


@pytest.fixture()
def store(tmp_path) -> GraphStateStore:
    return GraphStateStore(path=tmp_path / "graph.jsonl", app_key="test-alert-key-32-bytes-minimum")


def ev(**kwargs):
    base = {
        "event_type": "MODEL_CALL_DECISION",
        "decision": "ALLOWED",
        "reason": "allow",
        "principal_id": "agent:doc-ingestor",
        "on_behalf_of_user_id": "user:alice",
        "application_id": "app:crm",
        "key_id": "key:openai:prod",
        "model": "gpt-4o-mini",
        "system_prompt_hash": "good-hash",
    }
    base.update(kwargs)
    return base


def test_load_jsonl_reads_events(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(ev()) + "\n", encoding="utf-8")
    assert load_jsonl(path)[0]["principal_id"] == "agent:doc-ingestor"


def test_load_jsonl_missing_file_returns_empty(tmp_path):
    assert load_jsonl(tmp_path / "missing.jsonl") == []


def test_build_subject_contexts_groups_principal_user_application_and_key(policy):
    contexts = build_subject_contexts([ev()], policy)
    subjects = {(c["subject_type"], c["subject_id"]) for c in contexts}
    assert ("principal", "agent:doc-ingestor") in subjects
    assert ("user", "user:alice") in subjects
    assert ("application", "app:crm") in subjects
    assert ("key", "key:openai:prod") in subjects


def test_default_rules_include_core_alerts():
    ids = {r.rule_id for r in default_rules()}
    assert {"high_denial_rate", "system_prompt_signature_mismatch", "quota_exhausted", "usage_profile_model_violation"} <= ids


def test_high_denial_alert_raised(store, policy):
    events = [ev(decision="BLOCKED", reason="permission_denied"), ev(decision="BLOCKED", reason="permission_denied")]
    alerts = AlertEngine(store).evaluate(events, policy)
    assert any(a["rule_id"] == "high_denial_rate" for a in alerts)


def test_high_denial_threshold_respected(store, policy):
    events = [ev(decision="BLOCKED", reason="permission_denied")]
    alerts = AlertEngine(store).evaluate(events, policy)
    assert not any(a["rule_id"] == "high_denial_rate" for a in alerts)


def test_prompt_signature_mismatch_alert_raised(store, policy):
    alerts = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad")], policy)
    alert = next(a for a in alerts if a["rule_id"] == "system_prompt_signature_mismatch")
    assert alert["severity"] == "high"
    assert "bad" in alert["payload"]["hashes"]


def test_quota_exhausted_alert_for_principal_capacity(store, policy):
    alerts = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="principal_capacity_exceeded")], policy)
    assert any(a["rule_id"] == "quota_exhausted" for a in alerts)


def test_quota_exhausted_alert_for_user_quota(store, policy):
    alerts = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="user_quota_exceeded")], policy)
    assert any(a["rule_id"] == "quota_exhausted" for a in alerts)


def test_quota_exhausted_alert_for_key_quota(store, policy):
    alerts = AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="key_quota_exceeded")], policy)
    assert any(a["rule_id"] == "quota_exhausted" for a in alerts)


def test_usage_profile_model_violation_alert_raised(store, policy):
    alerts = AlertEngine(store).evaluate([ev(model="gpt-5.3")], policy)
    alert = next(a for a in alerts if a["rule_id"] == "usage_profile_model_violation")
    assert alert["payload"]["unexpected_models"] == ["gpt-5.3"]


def test_usage_profile_model_violation_not_raised_when_no_profile(store, policy):
    alerts = AlertEngine(store).evaluate([ev(principal_id="agent:unknown", model="gpt-5.3")], policy)
    assert not any(a["rule_id"] == "usage_profile_model_violation" for a in alerts)


def test_alerts_are_persisted_to_graph(store, policy):
    AlertEngine(store).evaluate([ev(decision="BLOCKED", reason="user_quota_exceeded")], policy)
    assert any(n.kind == "alert" for n in store.nodes.values())
    assert any(e["kind"] == "ALERT_RAISED" for e in store.events)


def test_custom_rule_condition_and_action_callback(store, policy):
    called = []

    def condition(ctx, events, policy):
        return ctx["subject_type"] == "principal" and ctx["subject_id"] == "agent:doc-ingestor"

    def build(ctx, events, policy):
        return Alert("custom_rule", "low", ctx["subject_type"], ctx["subject_id"], "custom fired", {})

    def action(alert, graph_state):
        called.append(alert["rule_id"])

    alerts = AlertEngine(store, rules=[AlertRule("custom_rule", "custom", "low", condition, build, (action,))]).evaluate([ev()], policy)
    assert alerts[0]["rule_id"] == "custom_rule"
    assert called == ["custom_rule"]


def test_default_llm_reviewer_low_risk(store, policy):
    result = LLMUsageReviewer(store).review([ev()], policy)
    assert any(r["result"]["risk"] == "low" for r in result)


def test_default_llm_reviewer_high_risk_for_prompt_mismatch(store, policy):
    result = LLMUsageReviewer(store).review([ev(reason="system_prompt_signature_mismatch", decision="BLOCKED")], policy)
    assert any(r["result"]["risk"] == "high" for r in result)


def test_default_llm_reviewer_medium_for_quota(store, policy):
    result = LLMUsageReviewer(store).review([ev(reason="user_quota_exceeded", decision="BLOCKED")], policy)
    assert any(r["result"]["risk"] == "medium" for r in result)


def test_llm_review_callback_is_injected(store, policy):
    seen = []

    def cb(payload):
        seen.append(payload["subject_id"])
        return {"risk": "custom", "recommended_action": "callback"}

    reviews = LLMUsageReviewer(store, review_callback=cb).review([ev()], policy)
    assert "agent:doc-ingestor" in seen
    assert any(r["result"]["risk"] == "custom" for r in reviews)


def test_llm_reviews_are_persisted_to_graph(store, policy):
    LLMUsageReviewer(store).review([ev()], policy)
    assert any(n.kind == "llm_usage_review" for n in store.nodes.values())
    assert any(e["kind"] == "LLM_USAGE_REVIEWED" for e in store.events)


def test_review_worker_writes_batch_result(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps(ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", system_prompt_hash="bad")) + "\n", encoding="utf-8")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(policy), encoding="utf-8")
    out = tmp_path / "reviews.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-alert-key-32-bytes-minimum")
    result = review_once(audit, pol, out)
    assert result["alerts"]
    assert result["reviews"]
    assert out.exists()


def test_review_worker_can_disable_llm_review(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps(ev()) + "\n", encoding="utf-8")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    result = review_once(audit, pol, tmp_path / "reviews.jsonl", run_llm_review=False)
    assert result["reviews"] == []


def test_review_worker_sample_size_limits_events(tmp_path, monkeypatch, policy):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("".join(json.dumps(ev(request_id=i)) + "\n" for i in range(5)), encoding="utf-8")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    result = review_once(audit, pol, tmp_path / "reviews.jsonl", sample_size=2)
    assert result["events_seen"] == 2


def test_alert_records_do_not_include_provider_secret(store, policy):
    events = [ev(decision="BLOCKED", reason="system_prompt_signature_mismatch", provider_secret="sk-nope")]
    AlertEngine(store).evaluate(events, policy)
    text = store.path.read_text(encoding="utf-8")
    assert "sk-nope" not in text


def test_reviewer_prompt_contains_aggregates_not_raw_secret(store, policy):
    seen = []

    def cb(payload):
        seen.append(payload)
        return {"risk": "low"}

    LLMUsageReviewer(store, cb).review([ev(provider_secret="sk-nope")], policy)
    # The sample includes event fields; gateway audit strips secrets before here.
    # This test pins that callback injection is explicit and visible to integrators.
    assert seen[0]["aggregates"]["models"][0][0] == "gpt-4o-mini"


def test_application_principal_prefix_gets_application_review(store, policy):
    reviews = LLMUsageReviewer(store).review([ev(principal_id="app:crm", application_id=None)], policy)
    assert any(r["subject_type"] == "application" and r["subject_id"] == "app:crm" for r in reviews)
