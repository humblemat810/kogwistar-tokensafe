from __future__ import annotations

import builtins
import importlib.machinery
from pathlib import Path

import pytest

from modelkeyguard import kogwistar_postgres_state
from modelkeyguard.kogwistar_import_guard import enforce_installed_kogwistar_only
from modelkeyguard.kogwistar_postgres_state import KogwistarPostgresGraphStateStore, _stable_embedding, resolve_kogwistar_embed_dim


def test_kogwistar_embed_dim_default(monkeypatch):
    monkeypatch.delenv("MODELKEYGUARD_KOGWISTAR_EMBED_DIM", raising=False)
    assert resolve_kogwistar_embed_dim() == 2


@pytest.mark.parametrize("value", ["0", "9", "-1", "not-int"])
def test_kogwistar_embed_dim_invalid(value: str):
    with pytest.raises(ValueError):
        resolve_kogwistar_embed_dim(value)


def test_kogwistar_embedding_is_deterministic_and_space_separated():
    a1 = _stable_embedding("hello", dim=2, space="policy")
    a2 = _stable_embedding("hello", dim=2, space="policy")
    b1 = _stable_embedding("hello", dim=2, space="event")
    assert a1 == a2
    assert a1 != b1


def test_kogwistar_postgres_missing_optional_import_fails_loudly(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "kogwistar.engine_core.engine_postgres":
            raise ImportError("simulated missing sqlalchemy/pgvector stack")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(kogwistar_postgres_state, "enforce_installed_kogwistar_only", lambda: None)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    store = KogwistarPostgresGraphStateStore.__new__(KogwistarPostgresGraphStateStore)
    store.dsn = "postgresql://modelguard:modelguard@localhost:5432/modelguard"
    store.embed_dim = 2

    with pytest.raises(RuntimeError, match="requires installed optional dependencies"):
        store._build_runtime()


def test_installed_only_guard_allows_repo_local_venv_site_packages(monkeypatch):
    origin = Path.cwd() / ".venv" / "lib" / "python3.12" / "site-packages" / "kogwistar" / "__init__.py"
    spec = importlib.machinery.ModuleSpec("kogwistar", loader=None, origin=str(origin))

    monkeypatch.setenv("MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY", "1")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: spec if name == "kogwistar" else None)

    enforce_installed_kogwistar_only()
