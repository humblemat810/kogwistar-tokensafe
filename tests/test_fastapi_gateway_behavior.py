import json

import pytest

from modelkeyguard.gateway import build_guard, create_app, process_chat_completion
from modelkeyguard.token_auth import TokenVerifier

EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


def _payload(model="gpt-4o-mini", system=EXPECTED_SYSTEM, max_tokens=16):
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Summarize this test document."},
        ],
        "max_tokens": max_tokens,
    }


def test_fastapi_gateway_core_allows_local_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, headers = process_chat_completion(
        _payload(),
        "Bearer kgw_demo_doc_ingestor",
        guard,
        policy,
        verifier,
        source_ip="127.0.0.1",
        raw_body=json.dumps(_payload()).encode(),
    )

    assert status == 200
    assert headers["content-type"] == "application/json"
    assert data["modelkeyguard"]["decision"] == "ALLOWED"
    assert data["choices"][0]["message"]["content"].startswith("ModelKeyGuard allowed")


def test_fastapi_gateway_core_denies_invalid_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(_payload(), "Bearer bad", guard, policy, verifier)

    assert status == 401
    assert data["error"]["message"] == "invalid_or_inactive_token"


def test_fastapi_gateway_core_denies_system_prompt_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(
        _payload(system="You are a different agent."),
        "Bearer kgw_demo_doc_ingestor",
        guard,
        policy,
        verifier,
    )

    assert status == 403
    assert data["error"]["message"] == "system_prompt_signature_mismatch"


def test_fastapi_gateway_core_returns_principal_capacity_429(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(
        _payload(),
        "Bearer kgw_demo_principal_busy",
        guard,
        policy,
        verifier,
    )

    assert status == 429
    assert data["error"]["message"] == "principal_capacity_exceeded"


def test_fastapi_gateway_core_returns_user_quota_429(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(
        _payload(),
        "Bearer kgw_demo_low_user",
        guard,
        policy,
        verifier,
    )

    assert status == 429
    assert data["error"]["message"] == "user_quota_exceeded"


def test_create_app_exposes_fastapi_routes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)

    assert client.get("/healthz").json()["server"] == "fastapi"
    assert client.get("/v1/models").status_code == 200
    response = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    assert response.status_code == 200
    assert response.json()["modelkeyguard"]["decision"] == "ALLOWED"
