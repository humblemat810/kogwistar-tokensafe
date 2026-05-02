from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modelkeyguard.governance_runtime import (
    classify_scanner_error,
    evaluate_scanner_plugins,
    load_scanner_loop_health,
    load_scanner_runtime_config,
    load_usage_scanner_checkpoint,
    run_usage_analysis_runtime,
    run_usage_reviewer_runtime,
    save_scanner_loop_health,
    save_usage_scanner_checkpoint,
    scanner_state_transition,
    scanner_should_run,
)


@dataclass
class _FakeUsageClient:
    def analyze(self, *, subject_type: str | None = None, subject_id: str | None = None, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return {
            "filters": {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "time_range": time_range,
                "bucket": bucket,
            }
        }


def test_usage_runtime_sync_and_async_are_shape_equivalent():
    client = _FakeUsageClient()
    kwargs = dict(
        client=client,
        base_url="http://gateway.example",
        time_range="24h",
        bucket="hour",
        user_subjects=["user:alice"],
        principal_subjects=["agent:doc-ingestor"],
        key_subjects=["key:openai:prod"],
    )
    sync_out = run_usage_analysis_runtime(**kwargs, runtime_mode="sync")
    async_out = run_usage_analysis_runtime(**kwargs, runtime_mode="async")

    assert sync_out == async_out
    assert sync_out["results"]["principal"]["agent:doc-ingestor"]["filters"]["subject_type"] == "principal"


def test_reviewer_runtime_sync_and_async_are_shape_equivalent(monkeypatch):
    def fake_run_langchain_reviewer(**kwargs):
        return {
            "model": kwargs["model"],
            "base_url": kwargs["base_url"],
            "key_id": kwargs.get("key_id", ""),
            "text": "review-ok",
        }

    monkeypatch.setattr("modelkeyguard.reviewer_agent.run_langchain_reviewer", fake_run_langchain_reviewer)

    kwargs = dict(
        status={"should_review": True, "summary": {"dangerous_keyword_hits": 1}, "window": {"latest_request_id": "req-1"}},
        base_url="http://127.0.0.1:8789",
        safe_token="safe-token",
        model="gemma4:e2b",
        system_prompt="sys",
        key_id="key:fwd-ollama:gemma4-e2b:3",
    )
    sync_out = run_usage_reviewer_runtime(**kwargs, runtime_mode="sync")
    async_out = run_usage_reviewer_runtime(**kwargs, runtime_mode="async")

    assert sync_out == async_out
    assert sync_out["text"] == "review-ok"


def test_scanner_plugins_topic_keyword_and_trigger_decision():
    status = {"should_review": False, "summary": {"dangerous_keyword_hits": 0}, "window": {"latest_request_id": "req-2"}}
    policy = {"scanner_plugins": {"usage_analysis": ["topic_keywords"], "topic_keywords": ["terrorist attacks"]}}
    plugins = evaluate_scanner_plugins(
        policy=policy,
        workflow_name="usage_analysis",
        status=status,
        recent_text="discussion mentions terrorist attacks in context",
    )

    assert any(bool(item.get("triggered")) for item in plugins)
    should_run, meta = scanner_should_run(status=status, force=False, plugin_results=plugins, usage_checkpoint=None)
    assert should_run is True
    assert meta["reason"] == "triggered"


class _FakeGraphState:
    def __init__(self) -> None:
        self.projections: dict[str, dict[str, Any]] = {}

    def put_projection(self, key: str, payload: dict[str, Any]) -> None:
        self.projections[key] = payload


def test_usage_scanner_checkpoint_roundtrip():
    graph = _FakeGraphState()
    before = load_usage_scanner_checkpoint(graph)
    assert before["last_seen_request_id"] == ""

    saved = save_usage_scanner_checkpoint(graph, latest_request_id="req-999", last_action="ran", update_last_run=True)
    assert saved["last_seen_request_id"] == "req-999"
    assert saved["last_action"] == "ran"
    assert saved["last_run_ts"]

    after = load_usage_scanner_checkpoint(graph)
    assert after["last_seen_request_id"] == "req-999"

    skipped = save_usage_scanner_checkpoint(graph, latest_request_id="req-999", last_action="skipped", update_last_run=False)
    assert skipped["last_action"] == "skipped"
    assert skipped["last_run_ts"] == saved["last_run_ts"]


def test_scanner_loop_health_roundtrip():
    graph = _FakeGraphState()
    before = load_scanner_loop_health(graph, workflow_name="usage_reviewer")
    assert before["state"] == "run"
    assert before["consecutive_failures"] == 0

    saved = save_scanner_loop_health(
        graph,
        workflow_name="usage_reviewer",
        state="backoff_wait",
        last_error_family="auth_denied",
        last_error_message="HTTP 401",
        consecutive_failures=2,
        next_retry_at="2026-05-02T00:00:00Z",
        breaker_tripped=False,
        last_success_ts="2026-05-01T00:00:00Z",
    )
    assert saved["last_error_family"] == "auth_denied"
    after = load_scanner_loop_health(graph, workflow_name="usage_reviewer")
    assert after["consecutive_failures"] == 2
    assert after["state"] == "backoff_wait"


def test_classify_scanner_error_families():
    assert classify_scanner_error(RuntimeError("review model call failed with HTTP 429: user_quota_exceeded")) == "quota_limit"
    assert classify_scanner_error(RuntimeError("review model call failed with HTTP 401: missing_bearer_token")) == "auth_denied"
    assert classify_scanner_error(RuntimeError("upstream timeout during model call")) == "upstream_transient"
    assert classify_scanner_error(RuntimeError("unexpected resolver failure")) == "runtime_internal"


def test_scanner_transition_success_resets_failures():
    config = load_scanner_runtime_config(policy={}, workflow_name="usage_analysis")
    out = scanner_state_transition(
        workflow_name="usage_analysis",
        config=config,
        health={"consecutive_failures": 5, "last_success_ts": ""},
        run_error=None,
    )
    assert out["state"] == "run"
    assert out["consecutive_failures"] == 0
    assert out["terminal_stop"] is False
    assert out["last_success_ts"]


def test_scanner_transition_backoff_without_breaker():
    config = load_scanner_runtime_config(policy={}, workflow_name="usage_reviewer")
    out = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 2, "last_success_ts": ""},
        run_error=RuntimeError("HTTP 401 Unauthorized"),
    )
    assert out["state"] == "backoff_wait"
    assert out["consecutive_failures"] == 3
    assert out["retry_after_seconds"] >= 120.0
    assert out["terminal_stop"] is False


def test_scanner_transition_breaker_terminal_when_enabled(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_MAX_FAILURES", "3")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_ERROR_FAMILY_POLICY_JSON", '{"auth_denied":"terminal"}')
    config = load_scanner_runtime_config(policy={}, workflow_name="usage_reviewer")
    out = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 2, "last_success_ts": ""},
        run_error=RuntimeError("HTTP 401 Unauthorized"),
    )
    assert out["terminal_stop"] is True
    assert out["state"] == "terminal_stop"
    assert out["breaker_tripped"] is True


def test_runtime_config_defaults_and_env_override(monkeypatch):
    cfg_default = load_scanner_runtime_config(policy={}, workflow_name="usage_analysis")
    assert cfg_default.backoff_initial_seconds == 30.0
    assert cfg_default.backoff_max_seconds == 900.0
    assert cfg_default.breaker_enabled is False

    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BACKOFF_INITIAL_SECONDS", "45")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BACKOFF_MAX_SECONDS", "600")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_MAX_FAILURES", "7")
    cfg_override = load_scanner_runtime_config(policy={}, workflow_name="usage_analysis")
    assert cfg_override.backoff_initial_seconds == 45.0
    assert cfg_override.backoff_max_seconds == 600.0
    assert cfg_override.breaker_enabled is True
    assert cfg_override.breaker_max_consecutive_failures == 7
