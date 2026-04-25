from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from modelkeyguard.core import ModelKey, ModelKeyGuard, Principal, Request
from modelkeyguard.postgres_state import PostgresGraphStateStore

pytestmark = pytest.mark.postgres


def _require_testcontainers():
    if importlib.util.find_spec("testcontainers") is None or importlib.util.find_spec("psycopg") is None:
        pytest.skip("install with: pip install -e .[testcontainers]")


def _policy() -> dict:
    return {
        "users": {
            "user:alice": {"quotas": {"hour": {"period": "hour", "max_requests": 10, "max_tokens": 10000, "max_usd": 10.0}}},
            "user:tiny": {"quotas": {"hour": {"period": "hour", "max_requests": 1, "max_tokens": 10, "max_usd": 0.00001}}},
        },
        "principal_quotas": {
            "agent:doc": {"hour": {"period": "hour", "max_requests": 10, "max_tokens": 10000, "max_usd": 10.0}},
            "agent:busy": {"hour": {"period": "hour", "max_requests": 1, "max_tokens": 10, "max_usd": 0.00001}},
        },
        "local_tokens": {
            "kgw_doc": {"principal_id": "agent:doc", "kind": "agent", "groups": ["agent-dev"], "namespace": "tenant:kogwistar", "on_behalf_of_user_id": "user:alice"},
            "kgw_tiny_user": {"principal_id": "agent:doc", "kind": "agent", "groups": ["agent-dev"], "namespace": "tenant:kogwistar", "on_behalf_of_user_id": "user:tiny"},
            "kgw_busy": {"principal_id": "agent:busy", "kind": "agent", "groups": ["agent-dev"], "namespace": "tenant:kogwistar", "on_behalf_of_user_id": "user:alice"},
        },
        "model_keys": [{"id": "key:openai:prod", "provider": "openai", "models": ["gpt-4o-mini"], "secret_ref": "env://OPENAI_API_KEY", "quotas": {"hour": {"period": "hour", "max_requests": 100, "max_tokens": 100000, "max_usd": 100.0}}, "acl": {"mode": "scope", "namespace": "tenant:kogwistar"}}],
    }


@pytest.fixture()
def pg_store(monkeypatch):
    _require_testcontainers()
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as postgres:
        dsn = postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-postgres-secret-key")
        store = PostgresGraphStateStore.from_policy(_policy(), dsn=dsn, app_key="test-postgres-secret-key")
        yield store


def _guard(store: PostgresGraphStateStore) -> ModelKeyGuard:
    guard = ModelKeyGuard.create()
    guard.graph_state = store
    guard.register_key(ModelKey("key:openai:prod", "openai", ("gpt-4o-mini",), "env://OPENAI_API_KEY", "OpenAI prod"))
    guard.grant("key:openai:prod", mode="scope", created_by="human:admin", namespace="tenant:kogwistar")
    return guard


def test_postgres_records_allow_and_strict_usage_lane(pg_store):
    guard = _guard(pg_store)
    req = Request(Principal("agent:doc", "agent", ("agent-dev",)), "key:openai:prod", "gpt-4o-mini", "tenant:kogwistar", estimated_tokens=100, estimated_cost_usd=0.01, token_id="kgw_doc", on_behalf_of_user_id="user:alice")
    decision = guard.check(req)
    assert decision.allowed
    guard.record_usage(decision, 0.01, 0.01, actual_tokens=100)
    assert pg_store.get_quota_used("principal", "agent:doc", "hour")["requests"] == 1
    assert pg_store.get_quota_used("user", "user:alice", "hour")["requests"] == 1
    # Reload from Postgres: the lane remains sequential and reconstructable.
    reloaded = PostgresGraphStateStore(pg_store.dsn, app_key="test-postgres-secret-key")
    assert reloaded.nodes["usage_head:user:alice"].payload["seq"] == 1


def test_postgres_distinguishes_principal_429_from_user_429(pg_store):
    guard = _guard(pg_store)
    principal_busy = Request(Principal("agent:busy", "agent", ("agent-dev",)), "key:openai:prod", "gpt-4o-mini", "tenant:kogwistar", estimated_tokens=100, estimated_cost_usd=0.01, token_id="kgw_busy", on_behalf_of_user_id="user:alice")
    d1 = guard.check(principal_busy)
    assert d1.http_status == 429
    assert d1.reason == "principal_capacity_exceeded"

    user_tiny = Request(Principal("agent:doc", "agent", ("agent-dev",)), "key:openai:prod", "gpt-4o-mini", "tenant:kogwistar", estimated_tokens=100, estimated_cost_usd=0.01, token_id="kgw_tiny_user", on_behalf_of_user_id="user:tiny")
    d2 = guard.check(user_tiny)
    assert d2.http_status == 429
    assert d2.reason == "user_quota_exceeded"


def test_postgres_sealed_payload_does_not_store_plaintext(pg_store):
    secret = "sk-this-should-not-appear"
    pg_store.put_node("sealed:test", "secret_payload", {"secret": secret})
    assert pg_store.secret_payload_plaintext_is_not_stored(secret)


def test_postgres_uses_named_projections_not_bespoke_quota_tables(pg_store):
    import psycopg
    with psycopg.connect(pg_store.dsn) as conn, conn.cursor() as cur:
        cur.execute("select to_regclass('public.named_projections') is not null")
        assert cur.fetchone()[0] is True
        cur.execute("select to_regclass('public.quota_projections')")
        assert cur.fetchone()[0] is None
        cur.execute("select to_regclass('public.usage_lanes')")
        assert cur.fetchone()[0] is None
