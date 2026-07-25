from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modelkeyguard.review_worker import review_once


@pytest.fixture(autouse=True)
def _pin_local_review_backend(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-review-worker-key-32-bytes-minimum")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")


def test_review_once_lookback_and_checkpoint_are_idempotent(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "review_results.jsonl"
    checkpoint = tmp_path / "review_checkpoint.json"
    graph = tmp_path / "graph.jsonl"

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-review-worker-key-32-bytes-minimum")

    _write_jsonl(
        audit,
        [
            {"ts": _iso(now - timedelta(hours=2)), "decision": "ALLOWED", "reason": "allowed", "principal_id": "agent:doc-ingestor", "model": "gpt-4o-mini"},
            {"ts": _iso(now - timedelta(minutes=1)), "decision": "ALLOWED", "reason": "allowed", "principal_id": "agent:doc-ingestor", "model": "gpt-4o-mini"},
        ],
    )

    first = review_once(
        audit,
        Path("config/gateway_policy.json"),
        out,
        sample_size=200,
        run_llm_review=False,
        lookback_minutes=10,
        checkpoint_path=checkpoint,
    )
    assert first["events_seen"] == 1
    assert checkpoint.exists()

    second = review_once(
        audit,
        Path("config/gateway_policy.json"),
        out,
        sample_size=200,
        run_llm_review=False,
        lookback_minutes=10,
        checkpoint_path=checkpoint,
    )
    assert second["events_seen"] == 0


def test_review_once_checkpoint_cursor_keeps_same_timestamp_events(tmp_path, monkeypatch):
    ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    audit = tmp_path / "audit.jsonl"
    out = tmp_path / "review_results.jsonl"
    checkpoint = tmp_path / "review_checkpoint.json"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    _write_jsonl(audit, [
        {"ts": ts, "request_id": "a", "decision": "ALLOWED", "reason": "allowed"},
        {"ts": ts, "request_id": "b", "decision": "ALLOWED", "reason": "allowed"},
    ])
    first = review_once(audit, Path("config/gateway_policy.json"), out, run_llm_review=False, checkpoint_path=checkpoint)
    assert first["events_seen"] == 2
    _write_jsonl(audit, [
        {"ts": ts, "request_id": "a", "decision": "ALLOWED", "reason": "allowed"},
        {"ts": ts, "request_id": "b", "decision": "ALLOWED", "reason": "allowed"},
        {"ts": ts, "request_id": "c", "decision": "ALLOWED", "reason": "allowed"},
    ])
    second = review_once(audit, Path("config/gateway_policy.json"), out, run_llm_review=False, checkpoint_path=checkpoint)
    assert second["events_seen"] == 1
