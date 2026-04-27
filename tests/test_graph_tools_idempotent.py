from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from modelkeyguard import graph_tools


def test_init_with_self_heal_retries_after_sealed_mismatch_and_resets_jsonl(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    graph_path.write_text("stale", encoding="utf-8")
    calls = {"n": 0}
    expected = object()

    def fake_from_policy(policy, path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("sealed graph payload authentication failed")
        return expected

    monkeypatch.setattr(graph_tools.GraphStateStore, "from_policy", fake_from_policy)

    out = graph_tools._init_with_self_heal(
        policy={},
        graph_path=graph_path,
        store_kind="jsonl",
        reset_existing=True,
    )
    assert out is expected
    assert calls["n"] == 2
    assert not graph_path.exists()


def test_init_with_self_heal_raises_when_reset_disabled(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"

    def fake_from_policy(policy, path=None):
        raise ValueError("sealed graph payload authentication failed")

    monkeypatch.setattr(graph_tools.GraphStateStore, "from_policy", fake_from_policy)

    with pytest.raises(ValueError, match="sealed graph payload authentication failed"):
        graph_tools._init_with_self_heal(
            policy={},
            graph_path=graph_path,
            store_kind="jsonl",
            reset_existing=False,
        )


def test_init_graph_is_repeatable_with_changed_key_in_jsonl_mode(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-graph-key-A-32-bytes-minimum")
    monkeypatch.setenv("MODELKEYGUARD_INIT_RESET_EXISTING", "1")

    assert graph_tools.init_graph(str(policy_path), str(graph_path)) == 0

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-graph-key-B-32-bytes-minimum")
    assert graph_tools.init_graph(str(policy_path), str(graph_path)) == 0


def test_init_graph_rejects_unknown_serious_backend_instead_of_jsonl_fallback(tmp_path, monkeypatch):
    graph_path = tmp_path / "graph.jsonl"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MODELKEYGUARD_STORE", "chroma")
    with pytest.raises(ValueError, match="unsupported_store_backend:chroma"):
        graph_tools.init_graph(str(policy_path), str(graph_path))


@dataclass(frozen=True)
class _TutorialCase:
    name: str
    store: str
    graph_relpath: str


def test_tutorial_rerun_safe_invariant(tmp_path, monkeypatch):
    """All applicable tutorials must support clean reruns with changed keys."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")

    # JSONL + Postgres-backed tutorial tracks that rely on init_graph/startup flows.
    cases = [
        _TutorialCase("slow_quickstart_cli_gui_parity", "jsonl", "out/quickstart_graph.jsonl"),
        _TutorialCase("e2e_langchain_case1_fake_jsonl", "jsonl", "out/case1_graph.jsonl"),
        _TutorialCase("e2e_langchain_case2_real_postgres", "postgres", "out/case2_graph.jsonl"),
        _TutorialCase("e2e_single_azure_gui_key_and_principal", "postgres", "out/single_e2e_graph.jsonl"),
        _TutorialCase("e2e_azure_real_key_usage_and_billing", "postgres", "out/azure_real_billing_graph.jsonl"),
        _TutorialCase("final_dev_guard_azure_real_setup", "postgres", "out/finaldev_graph.jsonl"),
    ]

    reset_calls: list[str] = []
    postgres_namespaces: dict[str, str] = {}

    class _DummyGraph:
        nodes: dict = {}
        edges: dict = {}
        events: list = []
        projections: dict = {}

    real_from_policy = graph_tools.GraphStateStore.from_policy.__func__

    def fake_postgres_reset(dsn: str | None = None) -> None:
        reset_calls.append(dsn or "")
        postgres_namespaces.clear()

    def fake_from_policy(cls, policy, path=None, app_key=None):
        if os.getenv("MODELKEYGUARD_STORE", "jsonl").lower() != "postgres":
            return real_from_policy(cls, policy, path=path, app_key=app_key)

        ns = str(path) if path is not None else "__postgres__"
        key = os.getenv("MODELKEYGUARD_GRAPH_KEY", "")
        existing = postgres_namespaces.get(ns)
        if existing and existing != key:
            raise ValueError("sealed graph payload authentication failed")
        postgres_namespaces[ns] = key
        return _DummyGraph()

    monkeypatch.setattr(graph_tools, "_reset_postgres_graph_state", fake_postgres_reset)
    monkeypatch.setattr(graph_tools.GraphStateStore, "from_policy", classmethod(fake_from_policy))

    for case in cases:
        graph_path = tmp_path / case.graph_relpath
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MODELKEYGUARD_STORE", case.store)
        monkeypatch.setenv("MODELKEYGUARD_INIT_RESET_EXISTING", "1")
        monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(graph_path))
        monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", f"{case.name}-graph-key-A-32-bytes-minimum")
        assert graph_tools.init_graph(str(policy_path), str(graph_path)) == 0

        # Hard invariant: rerun with a different encryption key must not crash.
        monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", f"{case.name}-graph-key-B-32-bytes-minimum")
        assert graph_tools.init_graph(str(policy_path), str(graph_path)) == 0

    postgres_case_runs = sum(1 for c in cases if c.store == "postgres") * 2
    assert len(reset_calls) >= postgres_case_runs
