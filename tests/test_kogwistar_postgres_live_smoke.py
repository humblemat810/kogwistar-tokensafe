from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres


def _require_live_smoke_deps() -> None:
    missing = [name for name in ("kogwistar", "psycopg", "psycopg2", "sqlalchemy", "pgvector") if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip(f"install with: pip install -e .[postgres] (missing: {', '.join(missing)})")


def _require_docker_daemon() -> None:
    if importlib.util.find_spec("docker") is None:
        pytest.skip("install with: pip install docker")
    import docker
    try:
        client = docker.from_env()
        client.ping()
        client.close()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable for testcontainers: {exc}")


def _require_reachable_postgres(dsn: str) -> None:
    try:
        import psycopg  # type: ignore
    except Exception:
        pytest.skip("psycopg is not available")
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return
    except Exception as exc:
        pytest.skip(f"postgres is not reachable for live smoke: {exc}")


def test_kogwistar_postgres_live_smoke_creates_no_jsonl_graph_artifacts():
    _require_live_smoke_deps()
    _require_docker_daemon()
    script = Path(__file__).resolve().parents[1] / "scripts" / "kogwistar_postgres_no_jsonl_smoke.py"
    env = os.environ.copy()
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        dsn = postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        _require_reachable_postgres(dsn)
        env["MODELKEYGUARD_POSTGRES_DSN"] = dsn
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_kogwistar_postgres_live_smoke_requires_explicit_dsn():
    _require_live_smoke_deps()
    script = Path(__file__).resolve().parents[1] / "scripts" / "kogwistar_postgres_no_jsonl_smoke.py"
    env = os.environ.copy()
    env.pop("MODELKEYGUARD_POSTGRES_DSN", None)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert result.returncode != 0
    assert "MODELKEYGUARD_POSTGRES_DSN must be set explicitly" in (result.stdout + result.stderr)
