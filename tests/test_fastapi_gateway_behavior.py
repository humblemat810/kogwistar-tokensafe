import json

import pytest

from modelkeyguard.gateway import build_guard, create_app, process_chat_completion
from modelkeyguard.token_auth import TokenVerifier

EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."
ADMIN_SECRET = "dev-modelkeyguard-admin-secret"


def _admin_headers():
    return {"x-modelkeyguard-admin-secret": ADMIN_SECRET}


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


def _register_provider_key(client, key_id: str, provider: str, model: str):
    r = client.post(
        "/admin/keys",
        data={
            "key_id": key_id,
            "provider": provider,
            "models": model,
            "display_name": f"{provider}-{model}",
            "provider_secret": f"fake-real-{provider}-key",
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200


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
    responses = client.post("/v1/responses", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    assert responses.status_code == 200
    assert responses.json()["modelkeyguard"]["decision"] == "ALLOWED"


def test_openai_streaming_chat_completions_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/v1/chat/completions",
        json={**_payload(), "stream": True},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert "data: [DONE]" in response.text


def test_openai_responses_input_format_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-4o-mini",
            "input": [
                {"type": "message", "role": "system", "content": EXPECTED_SYSTEM},
                {"type": "message", "role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["model"] == "gpt-4o-mini"
    assert body["output"][0]["content"][0]["text"]
    assert body["modelkeyguard"]["decision"] == "ALLOWED"


def test_openai_responses_streaming_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-4o-mini",
            "input": [
                {"type": "message", "role": "system", "content": EXPECTED_SYSTEM},
                {"type": "message", "role": "user", "content": "hello"},
            ],
            "stream": True,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert "response.completed" in response.text
    assert "data: [DONE]" in response.text


def test_gemini_native_client_can_call_generate_content_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)
    _register_provider_key(client, "key:gemini:test", "gemini", "gemini-2.0-flash")

    response = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "system_instruction": {"parts": [{"text": EXPECTED_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        },
        headers={"x-goog-api-key": "kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidates"][0]["content"]["role"] == "model"


def test_gemini_streaming_generate_content_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:gemini:stream", "gemini", "gemini-2.0-flash")

    response = client.post(
        "/v1beta/models/gemini-2.0-flash:streamGenerateContent",
        json={
            "system_instruction": {"parts": [{"text": EXPECTED_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        },
        headers={"x-goog-api-key": "kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert "candidates" in response.text


def test_gemini_native_supports_camel_case_system_instruction(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:gemini:camel", "gemini", "gemini-2.0-flash")

    response = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "systemInstruction": {"parts": [{"text": EXPECTED_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        },
        headers={"x-goog-api-key": "kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["content"]["role"] == "model"


def test_azure_native_chat_completions_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:azure:test", "azure_openai", "azure-mini")

    response = client.post(
        "/openai/deployments/azure-mini/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-mini")["messages"], "max_tokens": 16},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert response.json()["modelkeyguard"]["decision"] == "ALLOWED"


def test_azure_native_responses_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:azure:responses", "azure_openai", "gpt-5.3-codex")

    response = client.post(
        "/openai/responses?api-version=2025-04-01-preview",
        json={
            "model": "gpt-5.3-codex",
            "input": [
                {"type": "message", "role": "system", "content": EXPECTED_SYSTEM},
                {"type": "message", "role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["model"] == "gpt-5.3-codex"
    assert body["output"][0]["content"][0]["text"]


def test_azure_native_streaming_chat_completions_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:azure:stream", "azure_openai", "azure-mini")

    response = client.post(
        "/openai/deployments/azure-mini/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-mini")["messages"], "max_tokens": 16, "stream": True},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert "data: [DONE]" in response.text


def test_azure_provider_mismatch_rejected(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="gpt-4o-mini")["messages"], "max_tokens": 16},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "model_key_provider_mismatch"


def test_ollama_provider_mismatch_rejected(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/api/chat",
        json={"model": "gpt-4o-mini", "messages": [{"role": "system", "content": EXPECTED_SYSTEM}, {"role": "user", "content": "hello"}]},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "model_key_provider_mismatch"


def test_ollama_native_chat_non_stream_and_stream_supported(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:ollama:test", "ollama", "llama3.1")

    non_stream = client.post(
        "/api/chat",
        json={
            "model": "llama3.1",
            "messages": [
                {"role": "system", "content": EXPECTED_SYSTEM},
                {"role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert non_stream.status_code == 200
    assert non_stream.json()["done"] is True

    stream = client.post(
        "/api/chat",
        json={
            "model": "llama3.1",
            "messages": [
                {"role": "system", "content": EXPECTED_SYSTEM},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert stream.status_code == 200
    assert "\"done\": false" in stream.text


def test_provider_native_routes_require_auth(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    _register_provider_key(client, "key:gemini:auth", "gemini", "gemini-2.0-flash")
    _register_provider_key(client, "key:azure:auth", "azure_openai", "azure-mini")
    _register_provider_key(client, "key:ollama:auth", "ollama", "llama3.1")

    gemini = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    )
    azure = client.post(
        "/openai/deployments/azure-mini/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-mini")["messages"]},
    )
    azure_responses = client.post(
        "/openai/responses?api-version=2025-04-01-preview",
        json={"model": "azure-mini", "input": [{"type": "message", "role": "user", "content": "hello"}]},
    )
    ollama = client.post(
        "/api/chat",
        json={"model": "llama3.1", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert gemini.status_code == 401
    assert azure.status_code == 401
    assert azure_responses.status_code == 401
    assert ollama.status_code == 401


def test_request_rehydrates_runtime_key_and_acl_from_graph(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from modelkeyguard.kogwistar_acl_adapter import MiniACLGraph

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    app = create_app("config/gateway_policy.json")
    client = TestClient(app)
    _register_provider_key(client, "key:openai:rehydrate", "openai", "gpt-rehydrate")

    # Simulate cold runtime key/ACL cache loss after startup.
    app.state.guard.keys.pop("key:openai:rehydrate", None)
    app.state.guard.acl_graph = MiniACLGraph()
    app.state.guard._versions.clear()

    response = client.post(
        "/v1/chat/completions",
        json=_payload(model="gpt-rehydrate"),
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200
    assert response.json()["modelkeyguard"]["decision"] == "ALLOWED"


def test_forwarded_secret_replacement_capture_for_all_providers(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-real-openai-key")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:gemini:capture", "gemini", "gemini-2.0-flash")
    _register_provider_key(client, "key:azure:capture", "azure_openai", "azure-mini")
    _register_provider_key(client, "key:ollama:capture", "ollama", "llama3.1")

    openai = client.post(
        "/v1/chat/completions",
        json=_payload(model="gpt-4o-mini"),
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    gemini = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "system_instruction": {"parts": [{"text": EXPECTED_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        },
        headers={"x-goog-api-key": "kgw_demo_doc_ingestor"},
    )
    azure = client.post(
        "/openai/deployments/azure-mini/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-mini")["messages"], "max_tokens": 16},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    ollama = client.post(
        "/api/chat",
        json={
            "model": "llama3.1",
            "messages": [
                {"role": "system", "content": EXPECTED_SYSTEM},
                {"role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )

    assert openai.status_code == 200
    assert gemini.status_code == 200
    assert azure.status_code == 200
    assert ollama.status_code == 200

    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    by_provider = {r["provider"]: r for r in records}

    assert by_provider["openai"]["headers"]["authorization"] == "Bearer fake-real-openai-key"
    assert by_provider["azure_openai"]["headers"]["api-key"] == "fake-real-azure_openai-key"
    assert by_provider["gemini"]["headers"]["x-goog-api-key"] == "fake-real-gemini-key"
    assert by_provider["ollama"]["headers"]["authorization"] == "Bearer fake-real-ollama-key"
    assert "kgw_demo_doc_ingestor" not in json.dumps(by_provider)


def test_azure_native_responses_forwards_replaced_secret(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:azure:responses:capture", "azure_openai", "gpt-5.3-codex")

    response = client.post(
        "/openai/responses?api-version=2025-04-01-preview",
        json={
            "model": "gpt-5.3-codex",
            "input": [
                {"type": "message", "role": "system", "content": EXPECTED_SYSTEM},
                {"type": "message", "role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200

    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    azure_records = [r for r in records if r.get("provider") == "azure_openai"]
    assert azure_records
    last = azure_records[-1]
    assert "/openai/responses?api-version=2025-04-01-preview" in str(last.get("url"))
    assert last["headers"]["api-key"] == "fake-real-azure_openai-key"
    assert "kgw_demo_doc_ingestor" not in json.dumps(last)


def test_openai_responses_forwards_to_openai_responses_path(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-real-openai-key")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-4o-mini",
            "input": [
                {"type": "message", "role": "system", "content": EXPECTED_SYSTEM},
                {"type": "message", "role": "user", "content": "hello"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200

    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    openai_records = [r for r in records if r.get("provider") == "openai"]
    assert openai_records
    last = openai_records[-1]
    assert str(last.get("url")).endswith("/v1/responses")
    assert last["headers"]["authorization"] == "Bearer fake-real-openai-key"
