import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GRAPH_KEY_ENV_NAMES = {"MODELKEYGUARD_GRAPH_KEY", "MODELKEYGUARD_GRAPH_KEY_FILE"}
ALLOWED_ENV_READERS = {
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


def test_graph_key_env_is_read_only_by_canonical_resolvers():
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "modelkeyguard").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel in ALLOWED_ENV_READERS:
            continue
        for line, name in _graph_key_env_accesses(path):
            violations.append(f"{rel}:{line} directly accesses {name}")

    assert violations == []
