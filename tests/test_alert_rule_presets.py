from __future__ import annotations

import time

from modelkeyguard.alert_rules import AlertEngine
from modelkeyguard.graph_state import GraphStateStore


def _event(**overrides):
    base = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": "r",
        "principal_id": "agent:doc-ingestor",
        "decision": "ALLOWED",
        "reason": "allowed",
        "model": "gpt-4o-mini",
        "prompt_heuristics": {},
    }
    base.update(overrides)
    return base


def _policy():
    return {
        "usage_profiles": {
            "agent:doc-ingestor": {
                "models": ["gpt-4o-mini"],
                "allowed_intents": ["summarization", "qna"],
            }
        },
        "alert_rules": {
            "deny_spike_min_events": 5,
            "deny_spike_ratio": 0.6,
            "model_shift_min_events": 8,
        },
    }


def _engine(tmp_path):
    return AlertEngine(GraphStateStore(tmp_path / "graph.jsonl", "test-alert-preset-key-32-bytes-minimum"), default_actions=[])


def test_token_exfiltration_rule_positive(tmp_path):
    alerts = _engine(tmp_path).evaluate(
        [_event(prompt_heuristics={"token_exfiltration_attempt": True, "token_exfiltration_signals": ["api key"]})],
        _policy(),
    )
    assert any(a["rule_id"] == "token_exfiltration_attempt" for a in alerts)


def test_intent_drift_rule_positive(tmp_path):
    alerts = _engine(tmp_path).evaluate(
        [_event(prompt_heuristics={"intent_drift": True, "disallowed_intents": ["coding"], "observed_intents": ["coding"]})],
        _policy(),
    )
    assert any(a["rule_id"] == "intent_drift" for a in alerts)


def test_deny_spike_rule_positive_and_negative(tmp_path):
    denied = [_event(decision="BLOCKED", reason="user_quota_exceeded") for _ in range(6)]
    allowed = [_event(decision="ALLOWED", reason="allowed") for _ in range(4)]
    positive = _engine(tmp_path).evaluate(denied + allowed, _policy())
    assert any(a["rule_id"] == "deny_spike" for a in positive)

    negative = _engine(tmp_path).evaluate([_event(decision="BLOCKED", reason="user_quota_exceeded")] * 2 + [_event()] * 4, _policy())
    assert not any(a["rule_id"] == "deny_spike" for a in negative)


def test_sudden_model_shift_rule_positive_and_negative(tmp_path):
    before = [_event(model="gpt-4o-mini") for _ in range(5)]
    after = [_event(model="gpt-5.3-mini") for _ in range(5)]
    positive = _engine(tmp_path).evaluate(before + after, _policy())
    assert any(a["rule_id"] == "sudden_model_shift" for a in positive)

    negative = _engine(tmp_path).evaluate([_event(model="gpt-4o-mini") for _ in range(10)], _policy())
    assert not any(a["rule_id"] == "sudden_model_shift" for a in negative)
