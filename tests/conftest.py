from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from modelkeyguard.graph_state import GraphStateStore

TEST_GRAPH_KEY = "test-governance-harness-key-32-bytes!"


@dataclass
class GovernanceHarness:
    root: Path
    graph_path: Path
    audit_path: Path
    policy_path: Path
    graph: GraphStateStore

    def seed_history(
        self,
        *,
        request_id: str,
        request_body_text: str,
        response_body_text: str,
        ts: str = "2026-05-02T00:00:00Z",
    ) -> None:
        self.graph.put_node(
            f"history:{request_id}",
            "request_response_history",
            {
                "request_id": request_id,
                "ts": ts,
                "request_body_text": request_body_text,
                "response_body_text": response_body_text,
                "metadata": {"request_id": request_id},
            },
        )

    def seed_usage(
        self,
        *,
        request_id: str,
        principal_id: str = "agent:doc-ingestor",
        on_behalf_of_user_id: str = "user:alice",
        token_id: str = "tok-harness",
        actual_tokens: int = 321,
        actual_cost_usd: float = 0.001,
    ) -> None:
        self.graph.append_event(
            "MODEL_USAGE_RESULT",
            f"access:{request_id}:00000001",
            {
                "request_id": request_id,
                "principal_id": principal_id,
                "on_behalf_of_user_id": on_behalf_of_user_id,
                "token_id": token_id,
                "estimated_cost_usd": actual_cost_usd,
                "actual_cost_usd": actual_cost_usd,
                "estimated_tokens": actual_tokens,
                "actual_tokens": actual_tokens,
            },
        )

    def seed_named_projection(self, *, namespace: str, key: str, payload: dict[str, Any]) -> None:
        self.graph.put_projection(f"{namespace}:{key}", payload)


@pytest.fixture
def isolated_governance_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GovernanceHarness:
    root = tmp_path / "governance_harness"
    root.mkdir(parents=True, exist_ok=True)
    graph_path = root / "graph.jsonl"
    audit_path = root / "audit.jsonl"
    policy_path = root / "policy.json"

    policy = {
        "scanner_plugins": {
            "usage_analysis": ["topic_keywords"],
            "usage_reviewer": ["dangerous_keywords"],
            "topic_keywords": ["terrorist attacks", "credential leak"],
        },
        "scanner": {
            "backoff": {"initial_seconds": 30, "max_seconds": 900},
            "breaker": {"enabled": False, "max_consecutive_failures": 3},
            "retry": {
                "error_family_policy": {
                    "quota_limit": "retry",
                    "auth_denied": "retry",
                    "upstream_transient": "retry",
                    "runtime_internal": "retry",
                }
            },
        },
    }
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")

    monkeypatch.setenv("MODELKEYGUARD_ENV", "local")
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", TEST_GRAPH_KEY)
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_REVIEW_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("MODELKEYGUARD_POLICY_PATH", str(policy_path))
    monkeypatch.setenv("MODELKEYGUARD_SCANNER_INTERVAL_SECONDS", "0.01")

    graph = GraphStateStore(graph_path, app_key=TEST_GRAPH_KEY)
    harness = GovernanceHarness(
        root=root,
        graph_path=graph_path,
        audit_path=audit_path,
        policy_path=policy_path,
        graph=graph,
    )
    harness.seed_history(
        request_id="req-seed-1",
        request_body_text="please summarize and avoid terrorist attacks content",
        response_body_text="ack",
        ts="2026-05-02T00:00:00Z",
    )
    harness.seed_usage(request_id="req-seed-1")
    return harness
