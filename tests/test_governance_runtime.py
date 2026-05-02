from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modelkeyguard.governance_runtime import (
    classify_scanner_error,
    evaluate_scanner_plugins,
    load_scanner_loop_health,
    load_scanner_runtime_config,
    load_usage_scanner_checkpoint,
    recent_history_text,
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


def test_runtime_mode_invalid_raises_for_both_workflows():
    client = _FakeUsageClient()
    try:
        run_usage_analysis_runtime(
            client=client,
            base_url="http://gateway.example",
            time_range="24h",
            bucket="hour",
            user_subjects=["user:alice"],
            principal_subjects=["agent:doc-ingestor"],
            key_subjects=["key:openai:prod"],
            runtime_mode="invalid-mode",
        )
    except ValueError as exc:
        assert "unsupported runtime mode" in str(exc)
    else:
        raise AssertionError("expected usage runtime to reject invalid mode")

    try:
        run_usage_reviewer_runtime(
            status={"should_review": True, "summary": {}, "window": {"latest_request_id": "req-1"}},
            base_url="http://127.0.0.1:8789",
            safe_token="safe-token",
            model="gemma4:e2b",
            system_prompt="sys",
            runtime_mode="invalid-mode",
        )
    except ValueError as exc:
        assert "unsupported runtime mode" in str(exc)
    else:
        raise AssertionError("expected reviewer runtime to reject invalid mode")


def test_scanner_transition_backoff_progression_and_cap():
    config = load_scanner_runtime_config(
        policy={"scanner": {"backoff": {"initial_seconds": 30, "max_seconds": 120}}},
        workflow_name="usage_reviewer",
    )
    out_1 = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 0},
        run_error=RuntimeError("HTTP 503 temporary upstream failure"),
    )
    out_2 = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 1},
        run_error=RuntimeError("HTTP 503 temporary upstream failure"),
    )
    out_3 = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 2},
        run_error=RuntimeError("HTTP 503 temporary upstream failure"),
    )
    out_4 = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=config,
        health={"consecutive_failures": 3},
        run_error=RuntimeError("HTTP 503 temporary upstream failure"),
    )
    assert out_1["retry_after_seconds"] == 30.0
    assert out_2["retry_after_seconds"] == 60.0
    assert out_3["retry_after_seconds"] == 120.0
    assert out_4["retry_after_seconds"] == 120.0


def test_scanner_transition_terminal_policy_without_breaker_stays_backoff(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_ENABLED", "0")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_ERROR_FAMILY_POLICY_JSON", '{"auth_denied":"terminal"}')
    cfg = load_scanner_runtime_config(policy={}, workflow_name="usage_reviewer")
    out = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=cfg,
        health={"consecutive_failures": 99, "last_success_ts": ""},
        run_error=RuntimeError("HTTP 401 Unauthorized"),
    )
    assert out["error_family_policy"] == "terminal"
    assert out["state"] == "backoff_wait"
    assert out["terminal_stop"] is False
    assert out["breaker_tripped"] is False


def test_scanner_transition_breaker_enabled_below_threshold_stays_backoff(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_BREAKER_MAX_FAILURES", "5")
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_ERROR_FAMILY_POLICY_JSON", '{"auth_denied":"terminal"}')
    cfg = load_scanner_runtime_config(policy={}, workflow_name="usage_reviewer")
    out = scanner_state_transition(
        workflow_name="usage_reviewer",
        config=cfg,
        health={"consecutive_failures": 3, "last_success_ts": ""},
        run_error=RuntimeError("HTTP 401 Unauthorized"),
    )
    assert out["consecutive_failures"] == 4
    assert out["state"] == "backoff_wait"
    assert out["terminal_stop"] is False


def test_scanner_should_run_force_and_no_new_history_paths():
    status = {"should_review": False, "window": {"latest_request_id": "req-55"}}
    should_run_force, meta_force = scanner_should_run(
        status=status,
        force=True,
        plugin_results=[],
        usage_checkpoint={"last_seen_request_id": "req-55"},
    )
    assert should_run_force is True
    assert meta_force["reason"] == "forced"

    should_run_skip, meta_skip = scanner_should_run(
        status=status,
        force=False,
        plugin_results=[{"triggered": True, "plugin": "topic_keywords"}],
        usage_checkpoint={"last_seen_request_id": "req-55"},
    )
    assert should_run_skip is False
    assert meta_skip["reason"] == "no_new_history"

    should_run_none, meta_none = scanner_should_run(
        status={"should_review": False, "window": {"latest_request_id": ""}},
        force=False,
        plugin_results=[],
        usage_checkpoint={"last_seen_request_id": ""},
    )
    assert should_run_none is False
    assert meta_none["reason"] == "thresholds_not_met"


def test_runtime_config_policy_only_and_bad_env_json(monkeypatch):
    monkeypatch.delenv("MODELKEYGUARD_SCANNER_BACKOFF_INITIAL_SECONDS", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_SCANNER_BACKOFF_MAX_SECONDS", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_SCANNER_BREAKER_ENABLED", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_SCANNER_BREAKER_MAX_FAILURES", raising=False)
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_ERROR_FAMILY_POLICY_JSON", "{bad-json")
    cfg = load_scanner_runtime_config(
        policy={
            "scanner": {
                "backoff": {"initial_seconds": 12, "max_seconds": 48},
                "breaker": {"enabled": True, "max_consecutive_failures": 9},
                "retry": {"error_family_policy": {"auth_denied": "terminal"}},
            }
        },
        workflow_name="usage_reviewer",
    )
    assert cfg.backoff_initial_seconds == 12.0
    assert cfg.backoff_max_seconds == 48.0
    assert cfg.breaker_enabled is True
    assert cfg.breaker_max_consecutive_failures == 9
    assert cfg.error_family_policy["auth_denied"] == "terminal"
    assert cfg.error_family_policy["quota_limit"] == "retry"


def test_plugin_loader_handles_invalid_and_plugin_error_fallback(monkeypatch):
    policy = {
        "scanner_plugins": {
            "usage_analysis": [
                "not_a_real_plugin",
                "fake_plugin_module:raise_plugin",
            ],
            "topic_keywords": ["credential leak"],
        }
    }

    class _FakeModule:
        @staticmethod
        def raise_plugin(_payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

    def _fake_import(name: str):
        if name == "fake_plugin_module":
            return _FakeModule()
        raise ImportError(name)

    monkeypatch.setattr("modelkeyguard.governance_runtime.importlib.import_module", _fake_import)
    results = evaluate_scanner_plugins(
        policy=policy,
        workflow_name="usage_analysis",
        status={"summary": {"dangerous_keyword_hits": 0}},
        recent_text="no matches",
    )
    assert any(str(item.get("reason", "")).startswith("plugin_error:") for item in results)


def test_recent_history_text_orders_desc_and_truncates(tmp_path, monkeypatch):
    from modelkeyguard.graph_state import GraphStateStore

    monkeypatch.setenv("MODELKEYGUARD_ENV", "local")
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-governance-harness-key-32-bytes!")
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-governance-harness-key-32-bytes!")
    graph.put_node(
        "history:1",
        "request_response_history",
        {
            "request_id": "req-1",
            "ts": "2026-05-02T00:00:00Z",
            "request_body_text": "old",
            "response_body_text": "old-r",
        },
    )
    graph.put_node(
        "history:2",
        "request_response_history",
        {
            "request_id": "req-2",
            "ts": "2026-05-02T00:10:00Z",
            "request_body_text": "new",
            "response_body_text": "new-r",
        },
    )
    merged = recent_history_text(graph, limit=2)
    assert merged.index("new") < merged.index("old")
    very_long = "x" * 25000
    graph.put_node(
        "history:3",
        "request_response_history",
        {
            "request_id": "req-3",
            "ts": "2026-05-02T00:20:00Z",
            "request_body_text": very_long,
            "response_body_text": "",
        },
    )
    truncated = recent_history_text(graph, limit=3)
    assert len(truncated) <= 20000
