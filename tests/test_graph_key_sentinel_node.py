from __future__ import annotations

import pytest

from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.sealed_payload import (
    GRAPH_KEY_SENTINEL_KIND,
    GRAPH_KEY_SENTINEL_NODE_ID,
    GRAPH_KEY_SENTINEL_TEXT,
    open_json,
    seal_json,
)


@pytest.fixture(autouse=True)
def _jsonl_graph_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-graph-sentinel-key-32-bytes-minimum")


def test_graph_sentinel_node_is_created_once_and_reused():
    store = GraphStateStore.from_policy({})
    sentinel = store.nodes[GRAPH_KEY_SENTINEL_NODE_ID]

    assert sentinel.kind == GRAPH_KEY_SENTINEL_KIND
    assert sentinel.payload["sentinel_text"] == GRAPH_KEY_SENTINEL_TEXT
    assert isinstance(sentinel.payload["sealed_sentinel"], dict)
    assert open_json(sentinel.payload["sealed_sentinel"], store.app_key) == {"sentinel_text": GRAPH_KEY_SENTINEL_TEXT}

    reloaded = GraphStateStore.from_policy({})
    assert reloaded.nodes[GRAPH_KEY_SENTINEL_NODE_ID].payload == sentinel.payload


def test_graph_sentinel_node_cannot_be_overwritten():
    store = GraphStateStore.from_policy({})

    with pytest.raises(ValueError, match="graph key sentinel node cannot be overwritten"):
        store.put_node(
            GRAPH_KEY_SENTINEL_NODE_ID,
            GRAPH_KEY_SENTINEL_KIND,
            {"sentinel_text": "tampered", "sealed_sentinel": {"alg": "AES-256-GCM"}},
        )


def test_graph_state_without_sentinel_fails_on_reuse():
    store = GraphStateStore()
    store.put_node("node:regular", "regular", {"ok": True})

    with pytest.raises(ValueError, match="graph key sentinel node missing from existing graph state"):
        GraphStateStore.from_policy({})


def test_seal_json_uses_graph_seeded_sentinel_cache(monkeypatch):
    store = GraphStateStore.from_policy({})
    assert GRAPH_KEY_SENTINEL_NODE_ID in store.nodes

    def broken_bootstrap(*args, **kwargs):
        raise RuntimeError("should not bootstrap")

    monkeypatch.setattr("modelkeyguard.sealed_payload._bootstrap_graph_key_sentinel", broken_bootstrap)

    sealed = seal_json({"hello": "world"}, store.app_key)
    assert open_json(sealed, store.app_key) == {"hello": "world"}
