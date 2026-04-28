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
    KogwistarPostgresGraphStateStore,
    QUOTA_POLICY_PROJECTION_NAMESPACE,
)


DEFAULT_DSN = "postgresql://modelguard:modelguard@localhost:5432/modelguard"


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
    dsn = os.getenv("MODELKEYGUARD_POSTGRES_DSN", DEFAULT_DSN)

    with tempfile.TemporaryDirectory(prefix="mkg-kogwistar-pg-") as run_dir_raw:
        run_dir = Path(run_dir_raw)
        graph_path = run_dir / "must_not_exist_graph.jsonl"
        os.chdir(run_dir)
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

        quota_node = next((n for n in store.nodes.values() if n.kind == "quota_policy"), None)
        if quota_node is None:
            raise RuntimeError("kogwistar_postgres smoke initialized no quota policy nodes")
        lane = str(quota_node.payload["lane"])
        subject_id = str(quota_node.payload["subject_id"])
        store.rebuild_quota_policy_projection(lane, subject_id)
        reloaded = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=os.environ["MODELKEYGUARD_GRAPH_KEY"])
        if not reloaded.list_named_projections(QUOTA_POLICY_PROJECTION_NAMESPACE):
            raise RuntimeError("kogwistar_postgres smoke did not materialize quota policy projections")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
