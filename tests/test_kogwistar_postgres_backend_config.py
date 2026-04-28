from __future__ import annotations

import pytest

from modelkeyguard.kogwistar_postgres_state import _stable_embedding, resolve_kogwistar_embed_dim


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
