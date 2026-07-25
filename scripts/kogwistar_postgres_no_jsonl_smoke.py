#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modelkeyguard.graph_tools import init_graph, inspect_graph
from modelkeyguard.kogwistar_import_guard import enforce_installed_kogwistar_only
from modelkeyguard.kogwistar_postgres_state import (
    CURRENT_EDGE_PROJECTION_NAMESPACE,
    CURRENT_NODE_PROJECTION_NAMESPACE,
    KogwistarPostgresGraphStateStore,
    QUOTA_POLICY_PROJECTION_NAMESPACE,
)


def _require_module(name: str) -> None:
    if importlib.util.find_spec(name) is None:
        raise RuntimeError(f"missing required module for kogwistar_postgres smoke: {name}")


def _assert_installed_kogwistar() -> str:
    enforce_installed_kogwistar_only()
    spec = importlib.util.find_spec("kogwistar")
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    if origin is None:
        raise RuntimeError("kogwistar import did not resolve")
    if "site-packages" not in str(origin):
        raise RuntimeError(f"kogwistar did not resolve from site-packages: {origin}")
    return str(origin)


def main() -> int:
    for module in ("kogwistar", "psycopg", "psycopg2", "sqlalchemy", "pgvector"):
        _require_module(module)
    kogwistar_origin = _assert_installed_kogwistar()

    policy_path = REPO_ROOT / "config" / "gateway_policy.json"
    dsn = os.getenv("MODELKEYGUARD_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "MODELKEYGUARD_POSTGRES_DSN must be set explicitly for the kogwistar_postgres smoke. "
            "Use a disposable testcontainer or a dedicated throwaway database."
        )

    with tempfile.TemporaryDirectory(prefix="mkg-kogwistar-pg-") as run_dir_raw:
        run_dir = Path(run_dir_raw)
        graph_path = run_dir / "must_not_exist_graph.jsonl"
        # Keep caller cwd unchanged: Windows cannot remove a directory that is
        # still the process current directory when TemporaryDirectory exits.
        os.environ.update(
            {
                "MODELKEYGUARD_STORE": "kogwistar_postgres",
                "MODELKEYGUARD_POSTGRES_DSN": dsn,
                "MODELKEYGUARD_GRAPH_PATH": str(graph_path),
                "MODELKEYGUARD_GRAPH_KEY": "kogwistar-postgres-smoke-key-32-bytes-minimum",
                "MODELKEYGUARD_INIT_RESET_EXISTING": "1",
                "MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY": "1",
                "MODELKEYGUARD_USE_INSTALLED_KOGWISTAR": "1",
                "MODELKEYGUARD_KOGWISTAR_EMBED_DIM": os.getenv("MODELKEYGUARD_KOGWISTAR_EMBED_DIM", "2"),
            }
        )

        init_graph(str(policy_path), str(graph_path))
        store = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=os.environ["MODELKEYGUARD_GRAPH_KEY"])
        if not store.nodes:
            raise RuntimeError("kogwistar_postgres smoke initialized zero graph nodes")
        if not store.edges:
            raise RuntimeError("kogwistar_postgres smoke initialized zero graph edges")
        if not store.events:
            raise RuntimeError("kogwistar_postgres smoke initialized zero graph events")
        if store._list_nodes("node"):
            raise RuntimeError("current node serving state leaked into Kogwistar graph nodes")
        if store._rt.engine.backend.edge_get(where={"doc_id": "modelkeyguard.graph"}, include=["documents"], limit=1).get("ids"):
            raise RuntimeError("current edge serving state leaked into Kogwistar graph edges")
        if not store.list_named_projections(CURRENT_NODE_PROJECTION_NAMESPACE):
            raise RuntimeError("current nodes were not materialized as named projections")
        if not store.list_named_projections(CURRENT_EDGE_PROJECTION_NAMESPACE):
            raise RuntimeError("current edges were not materialized as named projections")

        quota_node = next((n for n in store.nodes.values() if n.kind == "quota_policy"), None)
        if quota_node is None:
            raise RuntimeError("kogwistar_postgres smoke initialized no quota policy nodes")
        lane = str(quota_node.payload["lane"])
        subject_id = str(quota_node.payload["subject_id"])
        store.rebuild_quota_policy_projection(lane, subject_id)
        reloaded = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=os.environ["MODELKEYGUARD_GRAPH_KEY"])
        if not reloaded.list_named_projections(QUOTA_POLICY_PROJECTION_NAMESPACE):
            raise RuntimeError("kogwistar_postgres smoke did not materialize quota policy projections")

        node_revision_count = len(reloaded._list_nodes("node_revision"))
        edge_revision_count = len(reloaded._list_nodes("edge_revision"))
        os.environ["MODELKEYGUARD_INIT_RESET_EXISTING"] = "0"
        init_graph(str(policy_path), str(graph_path))
        os.environ["MODELKEYGUARD_INIT_RESET_EXISTING"] = "1"
        retry_loaded = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=os.environ["MODELKEYGUARD_GRAPH_KEY"])
        if len(retry_loaded._list_nodes("node_revision")) != node_revision_count:
            raise RuntimeError("same-policy init retry appended node revisions")
        if len(retry_loaded._list_nodes("edge_revision")) != edge_revision_count:
            raise RuntimeError("same-policy init retry appended edge revisions")
        if len(retry_loaded.nodes) != len(reloaded.nodes) or len(retry_loaded.edges) != len(reloaded.edges):
            raise RuntimeError("same-policy init retry changed current serving projection cardinality")
        reloaded = retry_loaded

        if not reloaded.append_node_if_updated("smoke:node", "smoke_node", {"value": 1}):
            raise RuntimeError("first append_node_if_updated should append")
        after_node_create = len(reloaded._list_nodes("node_revision"))
        if after_node_create != node_revision_count + 1:
            raise RuntimeError("node create did not append exactly one revision")
        if reloaded.append_node_if_updated("smoke:node", "smoke_node", {"value": 1}):
            raise RuntimeError("identical append_node_if_updated should be a no-op")
        if len(reloaded._list_nodes("node_revision")) != after_node_create:
            raise RuntimeError("identical node write appended a revision")
        if not reloaded.append_node_if_updated("smoke:node", "smoke_node", {"value": 2}):
            raise RuntimeError("changed append_node_if_updated should append")
        node_revisions = reloaded._list_nodes("node_revision")
        if len(node_revisions) != after_node_create + 2:
            raise RuntimeError("changed node write should append revision plus tombstone redirect")
        node_revision_payloads = [reloaded._decode_payload_from_meta(r.metadata) for r in node_revisions]
        if not any(p.get("tombstone") and p.get("redirects_to_revision_id") for p in node_revision_payloads):
            raise RuntimeError("changed node write did not append tombstone redirect")

        if not reloaded.append_edge_if_updated("smoke:edge", "SMOKE_REL", "smoke:node", "policy:version:0001", {"value": 1}):
            raise RuntimeError("first append_edge_if_updated should append")
        after_edge_create = len(reloaded._list_nodes("edge_revision"))
        if after_edge_create != edge_revision_count + 1:
            raise RuntimeError("edge create did not append exactly one revision")
        if reloaded.append_edge_if_updated("smoke:edge", "SMOKE_REL", "smoke:node", "policy:version:0001", {"value": 1}):
            raise RuntimeError("identical append_edge_if_updated should be a no-op")
        if len(reloaded._list_nodes("edge_revision")) != after_edge_create:
            raise RuntimeError("identical edge write appended a revision")
        if not reloaded.append_edge_if_updated("smoke:edge", "SMOKE_REL", "smoke:node", "policy:version:0001", {"value": 2}):
            raise RuntimeError("changed append_edge_if_updated should append")
        edge_revisions = reloaded._list_nodes("edge_revision")
        if len(edge_revisions) != after_edge_create + 2:
            raise RuntimeError("changed edge write should append revision plus tombstone redirect")
        edge_revision_payloads = [reloaded._decode_payload_from_meta(r.metadata) for r in edge_revisions]
        if not any(p.get("tombstone") and p.get("redirects_to_revision_id") for p in edge_revision_payloads):
            raise RuntimeError("changed edge write did not append tombstone redirect")

        reloaded.load()
        if reloaded.nodes["smoke:node"].payload != {"value": 2}:
            raise RuntimeError("current node projection did not materialize changed payload")
        if reloaded.edges["smoke:edge"].payload != {"value": 2}:
            raise RuntimeError("current edge projection did not materialize changed payload")

        if graph_path.exists():
            raise RuntimeError(f"serious backend created MODELKEYGUARD_GRAPH_PATH JSONL: {graph_path}")

        jsonl_files = sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*.jsonl"))
        if jsonl_files:
            raise RuntimeError(f"serious backend smoke created JSONL files in run dir: {jsonl_files}")

        inspect_graph(str(graph_path))
        print(
            json.dumps(
                {
                    "ok": True,
                    "store": "kogwistar_postgres",
                    "dsn": dsn,
                    "kogwistar_origin": kogwistar_origin,
                    "run_dir": str(run_dir),
                    "jsonl_files": jsonl_files,
                    "nodes": len(reloaded.nodes),
                    "edges": len(reloaded.edges),
                    "events": len(reloaded.events),
                    "projections": len(reloaded.projections),
                },
                indent=2,
                sort_keys=True,
            )
        )
        # Windows keeps SQLite/temp paths locked while SQLAlchemy pools live;
        # release every graph runtime before TemporaryDirectory cleanup.
        for candidate in (store, reloaded, retry_loaded):
            close = getattr(candidate, "close", None)
            if callable(close):
                close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
