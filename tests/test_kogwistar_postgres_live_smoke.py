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


def test_kogwistar_postgres_live_smoke_creates_no_jsonl_graph_artifacts():
    _require_live_smoke_deps()
    script = Path(__file__).resolve().parents[1] / "scripts" / "kogwistar_postgres_no_jsonl_smoke.py"
    env = os.environ.copy()
    env.setdefault("MODELKEYGUARD_POSTGRES_DSN", "postgresql://modelguard:modelguard@localhost:5432/modelguard")
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
