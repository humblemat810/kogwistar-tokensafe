from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from collections import Counter

from .graph_state import GraphStateStore


def init_graph(policy_path: str = "config/gateway_policy.json", graph_path: str = "out/modelkeyguard_graph.jsonl") -> int:
    policy = json.loads(Path(policy_path).read_text())
    path = Path(graph_path)
    if os.getenv("MODELKEYGUARD_STORE", "postgres").lower() != "postgres" and path.exists():
        path.unlink()
    graph = GraphStateStore.from_policy(policy, path=path)
    target = os.getenv("MODELKEYGUARD_POSTGRES_DSN") if os.getenv("MODELKEYGUARD_STORE", "postgres").lower() == "postgres" else str(path)
    print(f"initialized encrypted graph: {target}")
    print(f"nodes={len(graph.nodes)} edges={len(graph.edges)} events={len(graph.events)} projections={len(graph.projections)}")
    return 0


def inspect_graph(graph_path: str = "out/modelkeyguard_graph.jsonl") -> int:
    if os.getenv("MODELKEYGUARD_STORE", "postgres").lower() == "postgres":
        from .postgres_state import PostgresGraphStateStore
        graph = PostgresGraphStateStore()
    else:
        graph = GraphStateStore(graph_path)
    node_kinds = Counter(n.kind for n in graph.nodes.values())
    edge_kinds = Counter(e.kind for e in graph.edges.values())
    decisions = Counter(n.payload.get("reason") or n.payload.get("event_type") for n in graph.nodes.values() if n.kind == "access_conversation_event")
    usage_heads = {nid: n.payload for nid, n in graph.nodes.items() if n.kind == "usage_lane_head"}
    print(json.dumps({
        "store": os.getenv("MODELKEYGUARD_STORE", "postgres"),
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
