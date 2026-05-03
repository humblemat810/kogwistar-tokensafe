from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from modelkeyguard.governance_runtime import (
    GOVERNANCE_SCANNER_HEALTH_NAMESPACE,
    USAGE_SCANNER_CHECKPOINT_KEY,
    USAGE_SCANNER_CHECKPOINT_NAMESPACE,
    run_usage_analysis_runtime,
    run_usage_reviewer_runtime,
)
from modelkeyguard.graph_state import GraphStateStore


class _FakeUsageClient:
    def analyze(self, *, subject_type: str | None = None, subject_id: str | None = None, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return {
            "overview": {"requests": 1},
            "filters": {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "time_range": time_range,
                "bucket": bucket,
            },
        }


class _FakeStatusClient:
    def __init__(self, status_payload: dict[str, Any]) -> None:
        self.base_url = "http://127.0.0.1:8789"
        self.bearer_token = "bearer"
        self.admin_secret = None
        self.keycloak = None
        self.timeout_seconds = 5
        self._status_payload = status_payload

    def status(self) -> dict[str, Any]:
        return dict(self._status_payload)

    def checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "payload": payload}


def _run_script(script_path: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", [str(script_path)] + argv)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script_path), run_name="__main__")
    out = capsys.readouterr()
    return int(exc.value.code), out.out, out.err


def _capture_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        calls.append(float(seconds))

    monkeypatch.setattr("time.sleep", _fake_sleep)
    return calls


def test_runtime_usage_analysis_sync_async_smoke_isolated_jsonl(isolated_governance_harness):
    out_sync = run_usage_analysis_runtime(
        client=_FakeUsageClient(),
        base_url="http://127.0.0.1:8789",
        time_range="24h",
        bucket="hour",
        user_subjects=["user:alice"],
        principal_subjects=["agent:doc-ingestor"],
        key_subjects=["key:openai:prod"],
        runtime_mode="sync",
    )
    out_async = run_usage_analysis_runtime(
        client=_FakeUsageClient(),
        base_url="http://127.0.0.1:8789",
        time_range="24h",
        bucket="hour",
        user_subjects=["user:alice"],
        principal_subjects=["agent:doc-ingestor"],
        key_subjects=["key:openai:prod"],
        runtime_mode="async",
    )
    assert out_sync == out_async
    assert out_sync["results"]["user"]["user:alice"]["filters"]["subject_type"] == "user"
    assert str(isolated_governance_harness.graph_path).startswith(str(isolated_governance_harness.root))


def test_runtime_usage_reviewer_sync_async_smoke_with_fake_llm(monkeypatch: pytest.MonkeyPatch, isolated_governance_harness):
    def _fake_reviewer(**kwargs: Any) -> dict[str, Any]:
        return {
            "base_url": kwargs["base_url"],
            "model": kwargs["model"],
            "text": "fake review summary",
            "key_id": kwargs.get("key_id", ""),
        }

    monkeypatch.setattr("modelkeyguard.reviewer_agent.run_langchain_reviewer", _fake_reviewer)
    status = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-seed-1"},
    }
    out_sync = run_usage_reviewer_runtime(
        status=status,
        base_url="http://127.0.0.1:8789",
        safe_token="safe-token",
        model="gemma4:e2b",
        system_prompt="sys",
        key_id="key:fwd-ollama:gemma4-e2b:3",
        runtime_mode="sync",
    )
    out_async = run_usage_reviewer_runtime(
        status=status,
        base_url="http://127.0.0.1:8789",
        safe_token="safe-token",
        model="gemma4:e2b",
        system_prompt="sys",
        key_id="key:fwd-ollama:gemma4-e2b:3",
        runtime_mode="async",
    )
    assert out_sync == out_async
    assert out_sync["text"] == "fake review summary"


def test_usage_analysis_script_loop_smoke_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_governance_harness):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_analysis_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-usage-1"},
    }
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr(
        "modelkeyguard.analytics.UsageAnalyticsClient.analyze",
        lambda self, subject_type=None, subject_id=None, time_range="24h", bucket="hour": {
            "overview": {"requests": 3},
            "filters": {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "time_range": time_range,
                "bucket": bucket,
            },
        },
    )
    sleep_calls = _capture_sleep(monkeypatch)
    code, out, _err = _run_script(
        script,
        [
            "--loop",
            "--max-iterations",
            "1",
            "--force",
            "--bearer-token",
            "bearer",
            "--principal",
            "agent:doc-ingestor",
        ],
        monkeypatch,
        capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["scanner_action"] == "ran"
    assert "loop_health" in payload
    assert "checkpoint" in payload
    assert sleep_calls == []

    graph = GraphStateStore(isolated_governance_harness.graph_path, app_key="test-governance-harness-key-32-bytes!")
    assert f"{USAGE_SCANNER_CHECKPOINT_NAMESPACE}:{USAGE_SCANNER_CHECKPOINT_KEY}" in graph.projections
    assert f"{GOVERNANCE_SCANNER_HEALTH_NAMESPACE}:usage_analysis" in graph.projections


def test_usage_analysis_script_loop_smoke_async_runtime(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_analysis_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-usage-async"},
    }
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr(
        "modelkeyguard.analytics.UsageAnalyticsClient.analyze",
        lambda self, subject_type=None, subject_id=None, time_range="24h", bucket="hour": {
            "overview": {"requests": 5},
            "filters": {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "time_range": time_range,
                "bucket": bucket,
            },
        },
    )
    _capture_sleep(monkeypatch)
    code, out, _err = _run_script(
        script,
        [
            "--loop",
            "--max-iterations",
            "1",
            "--force",
            "--runtime-mode",
            "async",
            "--bearer-token",
            "bearer",
            "--principal",
            "agent:doc-ingestor",
        ],
        monkeypatch,
        capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["runtime_mode"] == "async"
    assert payload["scanner_action"] == "ran"


def test_usage_reviewer_script_loop_smoke_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_governance_harness):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_reviewer_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-review-1"},
    }
    monkeypatch.setenv("REVIEWER_SAFE_TOKEN", "safe-token")
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.status", lambda self: dict(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.checkpoint", lambda self, payload: {"ok": True, "payload": payload})
    monkeypatch.setattr(
        "modelkeyguard.governance_runtime.run_usage_reviewer_runtime",
        lambda **kwargs: {
            "base_url": kwargs["base_url"],
            "model": kwargs["model"],
            "text": "review-ok",
            "key_id": kwargs.get("key_id", ""),
        },
    )
    sleep_calls = _capture_sleep(monkeypatch)
    code, out, _err = _run_script(script, ["--loop", "--max-iterations", "1", "--force"], monkeypatch, capsys)
    assert code == 0
    assert "review_result:" in out
    assert "reviewer_loop_health:" in out
    assert sleep_calls == []

    graph = GraphStateStore(isolated_governance_harness.graph_path, app_key="test-governance-harness-key-32-bytes!")
    assert f"{GOVERNANCE_SCANNER_HEALTH_NAMESPACE}:usage_reviewer" in graph.projections


def test_usage_reviewer_script_loop_smoke_async_runtime(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_reviewer_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-review-async"},
    }
    monkeypatch.setenv("REVIEWER_SAFE_TOKEN", "safe-token")
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.status", lambda self: dict(status_payload))
    monkeypatch.setattr("modelkeyguard.governance_runtime.run_usage_reviewer_runtime", lambda **kwargs: {"text": "ok", "model": kwargs["model"]})
    _capture_sleep(monkeypatch)
    code, out, _err = _run_script(
        script,
        ["--loop", "--max-iterations", "1", "--force", "--runtime-mode", "async"],
        monkeypatch,
        capsys,
    )
    assert code == 0
    assert "review_result:" in out
    assert '"model": "gemma4:e2b"' in out


def test_usage_reviewer_script_backoff_smoke(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_reviewer_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-review-backoff"},
    }
    monkeypatch.setenv("REVIEWER_SAFE_TOKEN", "safe-token")
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.status", lambda self: dict(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.checkpoint", lambda self, payload: {"ok": True, "payload": payload})
    monkeypatch.setattr(
        "modelkeyguard.governance_runtime.run_usage_reviewer_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("review model call failed with HTTP 401: missing_bearer_token")),
    )
    sleep_calls = _capture_sleep(monkeypatch)
    code, _out, err = _run_script(script, ["--loop", "--max-iterations", "2", "--force"], monkeypatch, capsys)
    assert code == 0
    assert '"reviewer_action": "error"' in err
    assert sleep_calls
    assert sleep_calls[0] >= 30.0


def test_usage_reviewer_script_breaker_terminal_stop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated_governance_harness):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_reviewer_agent.py"
    status_payload = {
        "should_review": True,
        "summary": {"dangerous_keyword_hits": 1},
        "window": {"latest_request_id": "req-script-review-breaker"},
    }
    monkeypatch.setenv("REVIEWER_SAFE_TOKEN", "safe-token")
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.from_env", lambda: _FakeStatusClient(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.status", lambda self: dict(status_payload))
    monkeypatch.setattr("modelkeyguard.reviewer_agent.ReviewStatusClient.checkpoint", lambda self, payload: {"ok": True, "payload": payload})
    monkeypatch.setattr(
        "modelkeyguard.governance_runtime.run_usage_reviewer_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("review model call failed with HTTP 401: missing_bearer_token")),
    )
    _capture_sleep(monkeypatch)
    code, _out, err = _run_script(
        script,
        [
            "--loop",
            "--max-iterations",
            "1",
            "--force",
            "--scanner-breaker-enabled",
            "--scanner-breaker-max-failures",
            "1",
            "--scanner-error-family-policy-json",
            '{"auth_denied":"terminal"}',
        ],
        monkeypatch,
        capsys,
    )
    assert code == 2
    assert '"reviewer_action": "error"' in err

    graph = GraphStateStore(isolated_governance_harness.graph_path, app_key="test-governance-harness-key-32-bytes!")
    health = graph.projections[f"{GOVERNANCE_SCANNER_HEALTH_NAMESPACE}:usage_reviewer"]
    assert health["state"] == "terminal_stop"
    assert bool(health["breaker_tripped"]) is True
