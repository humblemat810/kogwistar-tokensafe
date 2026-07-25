from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import threading
import uuid

import pytest

from modelkeyguard.kogwistar_postgres_state import KogwistarPostgresGraphStateStore


pytestmark = pytest.mark.postgres


def test_kogwistar_postgres_multi_projection_cas_two_connections():
    required = ("kogwistar", "psycopg", "sqlalchemy", "pgvector", "docker", "testcontainers")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip("missing postgres integration dependencies: " + ", ".join(missing))
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        dsn = postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        app_key = "live-cas-test-key-32-bytes-minimum"
        first = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=app_key)
        second = KogwistarPostgresGraphStateStore(dsn=dsn, app_key=app_key)
        namespace = "cas-live-" + uuid.uuid4().hex

        def row(key: str, value: int, expected: int | None = None) -> dict:
            return {
                "namespace": namespace,
                "key": key,
                "payload": {"value": value},
                "expected_last_authoritative_seq": expected,
                "expected_last_materialized_seq": expected,
                "last_authoritative_seq": value + 1,
                "last_materialized_seq": value + 1,
            }

        assert first.compare_and_swap_named_projections([row("a", 0), row("b", 0)])
        barrier = threading.Barrier(2)

        def attempt(store, value: int) -> bool:
            barrier.wait(timeout=10)
            return store.compare_and_swap_named_projections([row("a", value, 1), row("b", value, 1)])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(attempt, first, 1), pool.submit(attempt, second, 2)]
            outcomes = [future.result(timeout=30) for future in results]
        assert sorted(outcomes) == [False, True]
        snapshot = {item["key"]: item for item in first.list_named_projections(namespace)}
        assert snapshot["a"]["payload"] == snapshot["b"]["payload"]
