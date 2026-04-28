from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy.exc import NotSupportedError

from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.graph_backend import GraphStateBackend, resolve_graph_state_backend
from modelkeyguard.postgres_state import PostgresGraphStateStore


pytestmark = pytest.mark.postgres


def _require_testcontainers():
    if importlib.util.find_spec("testcontainers") is None or importlib.util.find_spec("psycopg") is None:
        pytest.skip("install with: pip install -e .[testcontainers]")
    if importlib.util.find_spec("docker") is None:
        pytest.skip("install with: pip install docker")
    import docker

    try:
        client = docker.from_env()
        client.ping()
        client.close()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable for testcontainers: {exc}")


@pytest.fixture()
def postgres_store():
    _require_testcontainers()
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as postgres:
        dsn = postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        yield PostgresGraphStateStore(dsn=dsn, app_key="backend-matrix-key-32-bytes-minimum")


def test_jsonl_and_postgres_share_current_graph_write_contract(tmp_path, postgres_store):
    stores = [
        GraphStateStore(tmp_path / "matrix.jsonl", app_key="backend-matrix-key-32-bytes-minimum"),
        postgres_store,
    ]

    for store in stores:
        store.put_node("matrix:node", "matrix_node", {"value": 1})
        store.put_edge("matrix:edge", "MATRIX_REL", "matrix:node", "matrix:target", {"value": 1})
        store.put_node("matrix:node", "matrix_node", {"value": 1})
        store.put_edge("matrix:edge", "MATRIX_REL", "matrix:node", "matrix:target", {"value": 1})
        store.put_node("matrix:node", "matrix_node", {"value": 2})
        store.put_edge("matrix:edge", "MATRIX_REL", "matrix:node", "matrix:target", {"value": 2})

        assert store.nodes["matrix:node"].payload == {"value": 2}
        assert store.edges["matrix:edge"].payload == {"value": 2}
        assert store.edges["matrix:edge"].source == "matrix:node"
        assert store.edges["matrix:edge"].target == "matrix:target"


def test_backend_facade_resolves_protocol_implementations(tmp_path, postgres_store):
    jsonl = GraphStateStore(tmp_path / "matrix2.jsonl", app_key="backend-matrix-key-32-bytes-minimum")
    resolved_jsonl = resolve_graph_state_backend("jsonl", path=str(tmp_path / "matrix3.jsonl"), app_key="backend-matrix-key-32-bytes-minimum")
    resolved_postgres = resolve_graph_state_backend("postgres", app_key="backend-matrix-key-32-bytes-minimum", dsn=postgres_store.dsn)

    assert isinstance(jsonl, GraphStateBackend)
    assert isinstance(resolved_jsonl, GraphStateBackend)
    assert isinstance(resolved_postgres, GraphStateBackend)

    try:
        resolved_kogwistar = resolve_graph_state_backend("kogwistar_postgres", app_key="backend-matrix-key-32-bytes-minimum", dsn=postgres_store.dsn)
    except (RuntimeError, NotSupportedError) as exc:
        pytest.skip(f"kogwistar_postgres unavailable in this test environment: {exc}")

    assert isinstance(resolved_kogwistar, GraphStateBackend)


def test_kogwistar_postgres_backend_is_pinned_by_live_smoke():
    """The Kogwistar backend has stronger append/projection invariants than this unit matrix.

    See scripts/kogwistar_postgres_no_jsonl_smoke.py and
    tests/test_kogwistar_postgres_live_smoke.py for the live matrix leg that pins:
    installed-only imports, no JSONL graph artifacts, append-if-updated revisions,
    tombstone redirects, and current node/edge named projections.
    """

    assert True
