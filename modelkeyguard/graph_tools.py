from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from collections import Counter

from .graph_state import GraphStateStore, resolve_store_backend
from .policy_loader import load_policy_json


def init_graph(policy_path: str = "config/gateway_policy.json", graph_path: str = "out/modelkeyguard_graph.jsonl") -> int:
    policy = load_policy_json(policy_path)
    path = Path(graph_path)
    store_kind = resolve_store_backend()
    if store_kind == "jsonl" and path.exists():
        path.unlink()

    reset_existing = _bool_env(
        "MODELKEYGUARD_INIT_RESET_EXISTING",
        default=False,
    )
    if store_kind in {"postgres", "kogwistar_postgres"} and reset_existing:
        _reset_postgres_graph_state(os.getenv("MODELKEYGUARD_POSTGRES_DSN"))

    graph = _init_with_self_heal(
        policy=policy,
        graph_path=path,
        store_kind=store_kind,
        reset_existing=reset_existing,
    )
    target = os.getenv("MODELKEYGUARD_POSTGRES_DSN") if store_kind in {"postgres", "kogwistar_postgres"} else str(path)
    print(f"initialized encrypted graph: {target}")
    print(f"nodes={len(graph.nodes)} edges={len(graph.edges)} events={len(graph.events)} projections={len(graph.projections)}")
    return 0


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _init_with_self_heal(*, policy: dict, graph_path: Path, store_kind: str, reset_existing: bool):
    try:
        return GraphStateStore.from_policy(policy, path=graph_path)
    except ValueError as exc:
        msg = str(exc)
        if "sealed graph payload authentication failed" not in msg or not reset_existing:
            raise
        if store_kind in {"postgres", "kogwistar_postgres"}:
            _reset_postgres_graph_state(os.getenv("MODELKEYGUARD_POSTGRES_DSN"))
        else:
            graph_path.unlink(missing_ok=True)
        return GraphStateStore.from_policy(policy, path=graph_path)


def _reset_postgres_graph_state(dsn: str | None = None) -> None:
    dsn_value = dsn or os.getenv("MODELKEYGUARD_POSTGRES_DSN", "postgresql://modelguard:modelguard@localhost:5432/modelguard")
    try:
        import psycopg  # type: ignore
    except Exception:
        return
    with psycopg.connect(dsn_value) as conn, conn.cursor() as cur:
        # Reset authoritative append-only records and current projections so
        # init_graph is idempotent across key changes in local/dev workflows.
        for table in (
            # local postgres backend tables
            "graph_records",
            "graph_events",
            "graph_edges",
            "graph_nodes",
            "named_projections",
            # kogwistar pgvector backend tables
            "gke_nodes",
            "gke_edges",
            "gke_documents",
            "gke_domains",
            "gke_edge_endpoints",
            "gke_edge_refs",
            "gke_node_docs",
            "gke_node_refs",
            # kogwistar meta-store tables
            "global_seq",
            "user_seq",
            "index_jobs",
            "index_applied_state",
            "projected_lane_messages",
            "scoped_seq",
            "runtime_cursor_projection",
            "workflow_design_delta",
            "workflow_design_snapshot",
            "workflow_steps",
            "workflow_step_edges",
            "run_registry",
        ):
            try:
                cur.execute(f"truncate table {table} restart identity cascade")
                conn.commit()
            except Exception:
                conn.rollback()
                continue


def inspect_graph(graph_path: str = "out/modelkeyguard_graph.jsonl") -> int:
    store_kind = resolve_store_backend()
    if store_kind == "postgres":
        from .postgres_state import PostgresGraphStateStore
        graph = PostgresGraphStateStore()
    elif store_kind == "kogwistar_postgres":
        from .kogwistar_postgres_state import KogwistarPostgresGraphStateStore

        graph = KogwistarPostgresGraphStateStore()
    else:
        graph = GraphStateStore(graph_path)
    node_kinds = Counter(n.kind for n in graph.nodes.values())
    edge_kinds = Counter(e.kind for e in graph.edges.values())
    decisions = Counter(n.payload.get("reason") or n.payload.get("event_type") for n in graph.nodes.values() if n.kind == "access_conversation_event")
    usage_heads = {nid: n.payload for nid, n in graph.nodes.items() if n.kind == "usage_lane_head"}
    print(json.dumps({
        "store": store_kind,
        "graph_path": graph_path,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "events": len(graph.events),
        "projections": len(graph.projections),
        "node_kinds": dict(node_kinds),
        "edge_kinds": dict(edge_kinds),
        "access_decisions": dict(decisions),
        "usage_heads": usage_heads,
        "named_projections": graph.projections,
    }, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    init = sub.add_parser("init")
    init.add_argument("--policy", default="config/gateway_policy.json")
    init.add_argument("--graph", default="out/modelkeyguard_graph.jsonl")
    insp = sub.add_parser("inspect")
    insp.add_argument("--graph", default="out/modelkeyguard_graph.jsonl")
    args = p.parse_args(argv)
    if args.cmd == "init":
        return init_graph(args.policy, args.graph)
    if args.cmd == "inspect":
        return inspect_graph(args.graph)
    p.print_help(); return 2
