from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterable

from .sealed_payload import (
    GRAPH_KEY_SENTINEL_KIND,
    GRAPH_KEY_SENTINEL_NODE_ID,
    GRAPH_KEY_SENTINEL_TEXT,
    build_graph_key_sentinel_storage_payload,
    ensure_graph_key_sentinel,
    seed_graph_key_sentinel_from_storage_payload,
)

GRAPH_KEY_ENV_NAMES = {"MODELKEYGUARD_GRAPH_KEY", "MODELKEYGUARD_GRAPH_KEY_FILE"}
CANONICAL_GRAPH_KEY_READERS = {
    Path("modelkeyguard/settings.py"),
    Path("modelkeyguard/graph_state.py"),
}


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_getenv_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    )


def _is_os_environ_method_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "pop", "setdefault"}
        and _is_os_environ(node.func.value)
    )


def _graph_key_env_accesses(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_is_os_getenv_call(node) or _is_os_environ_method_call(node)):
            name = _constant_string(node.args[0]) if node.args else None
            if name in GRAPH_KEY_ENV_NAMES:
                hits.append((node.lineno, name))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name = _constant_string(node.slice)
            if name in GRAPH_KEY_ENV_NAMES:
                hits.append((node.lineno, name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_os_environ(target.value):
                    name = _constant_string(target.slice)
                    if name in GRAPH_KEY_ENV_NAMES:
                        hits.append((node.lineno, name))
    return hits


def scan_graph_key_env_contract(root: str | Path | None = None, *, package_name: str = "modelkeyguard") -> list[str]:
    root_path = Path(root or Path(__file__).resolve().parents[1])
    package_root = root_path / package_name
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        rel = path.relative_to(root_path)
        if rel in CANONICAL_GRAPH_KEY_READERS:
            continue
        for line, name in _graph_key_env_accesses(path):
            violations.append(f"{rel}:{line} directly accesses {name}")
    return violations


def ensure_graph_key_sentinel_node(store: Any, graph_key: str) -> dict[str, object]:
    sentinel = store.nodes.get(GRAPH_KEY_SENTINEL_NODE_ID)
    other_nodes = [node_id for node_id in store.nodes if node_id != GRAPH_KEY_SENTINEL_NODE_ID]
    if sentinel is not None:
        if sentinel.kind != GRAPH_KEY_SENTINEL_KIND:
            raise ValueError("graph key sentinel node has unexpected kind")
        if sentinel.payload.get("sentinel_text") != GRAPH_KEY_SENTINEL_TEXT:
            raise ValueError("graph key sentinel node has unexpected plaintext")
        seed_graph_key_sentinel_from_storage_payload(sentinel.payload, graph_key)
        return dict(sentinel.payload)
    if other_nodes:
        raise ValueError("graph key sentinel node missing from existing graph state")
    payload = build_graph_key_sentinel_storage_payload(graph_key)
    store.put_node(GRAPH_KEY_SENTINEL_NODE_ID, GRAPH_KEY_SENTINEL_KIND, payload)
    seed_graph_key_sentinel_from_storage_payload(payload, graph_key)
    return payload


def graph_key_startup_self_test(graph_key: str) -> str | None:
    try:
        ensure_graph_key_sentinel(graph_key)
    except Exception as exc:
        return f"MODELKEYGUARD_GRAPH_KEY startup self-test failed: {exc}"
    return None
