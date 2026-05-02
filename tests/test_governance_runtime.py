from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modelkeyguard.governance_runtime import (
    evaluate_scanner_plugins,
    load_usage_scanner_checkpoint,
    run_usage_analysis_runtime,
    run_usage_reviewer_runtime,
    save_usage_scanner_checkpoint,
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

    saved = save_usage_scanner_checkpoint(graph, latest_request_id="req-999")
    assert saved["last_seen_request_id"] == "req-999"

    after = load_usage_scanner_checkpoint(graph)
    assert after["last_seen_request_id"] == "req-999"
