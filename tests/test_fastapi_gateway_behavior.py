import json
import time
import urllib.error

import pytest

from modelkeyguard import gateway
from modelkeyguard.gateway import build_guard, create_app, forward_provider, process_chat_completion
from modelkeyguard.token_auth import TokenVerifier

EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."
ADMIN_SECRET = "dev-modelkeyguard-admin-secret"


@pytest.fixture(autouse=True)
def _force_jsonl_store(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-fastapi-gateway-key-32-bytes")


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


def test_forward_provider_joblib_cache_replays_real_mode_response(tmp_path, monkeypatch):
    pytest.importorskip("joblib")

    calls = []

    class _Resp:
        status = 200
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id":"real-once","choices":[{"message":{"content":"cached"}}]}'

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, timeout))
        return _Resp()

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MODELKEYGUARD_LLM_CALL_CACHE", "joblib")
    monkeypatch.setenv("MODELKEYGUARD_LLM_CALL_CACHE_DIR", str(tmp_path / "llm-cache"))

    body = json.dumps(_payload()).encode("utf-8")
    first = forward_provider("super-secret-provider-key", body, provider="openai", url="https://upstream.example/v1/chat/completions")
    second = forward_provider("super-secret-provider-key", body, provider="openai", url="https://upstream.example/v1/chat/completions")

    assert first[0] == 200
    assert second[0] == 200
    assert second[1]["x-modelkeyguard-llm-cache"] == "hit"
    assert second[2] == first[2]
    assert len(calls) == 1
    cache_files = list((tmp_path / "llm-cache").glob("*.joblib"))
    assert len(cache_files) == 1
    assert "super-secret-provider-key" not in cache_files[0].name


def test_forward_provider_uses_three_minute_default_timeout(monkeypatch):
    calls = []

    class _Resp:
        status = 200
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        return _Resp()

    monkeypatch.delenv("MODELKEYGUARD_PROVIDER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)

    forward_provider("super-secret-provider-key", b"{}", provider="ollama", url="http://ollama.example/api/chat")

    assert calls == [180.0]


def test_forward_provider_timeout_can_be_overridden(monkeypatch):
    calls = []

    class _Resp:
        status = 200
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        return _Resp()

    monkeypatch.setenv("MODELKEYGUARD_PROVIDER_TIMEOUT_SECONDS", "240")
    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)

    forward_provider("super-secret-provider-key", b"{}", provider="ollama", url="http://ollama.example/api/chat")

    assert calls == [240.0]


def test_forward_provider_maps_unreachable_upstream_to_502(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)

    status, headers, body = forward_provider(
        "super-secret-provider-key",
        b"{}",
        provider="ollama",
        url="http://127.0.0.1:11434/api/chat",
    )

    assert status == 502
    assert headers["content-type"] == "application/json"
    data = json.loads(body)
    assert data["error"]["message"] == "provider_upstream_unreachable"
    assert data["error"]["upstream_url"] == "http://127.0.0.1:11434/api/chat"


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


def test_keycloak_service_account_resource_role_counts_as_allowed_role(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-keycloak-token-key-32-bytes")

    from modelkeyguard import token_auth

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "active": True,
                    "client_id": "modelguard-usage-agent",
                    "jti": "test-jti",
                    "exp": int(time.time()) + 300,
                    "scope": "openid profile",
                    "realm_access": {"roles": []},
                    "resource_access": {"modelguard-usage-agent": {"roles": ["model.usage.read"]}},
                }
            ).encode("utf-8")

    def fake_urlopen(req, data=None, timeout=None):
        return _Resp()

    monkeypatch.setattr(token_auth.urllib.request, "urlopen", fake_urlopen)
    verifier = TokenVerifier("config/gateway_policy.json")
    principal = verifier.verify_keycloak_authorization_header("Bearer test-token")

    assert "model.usage.read" in principal.groups


def test_keycloak_service_account_realm_role_counts_as_allowed_role(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-keycloak-token-key-32-bytes")

    from modelkeyguard import token_auth

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "active": True,
                    "client_id": "modelguard-usage-agent",
                    "jti": "test-jti",
                    "exp": int(time.time()) + 300,
                    "scope": "openid profile",
                    "realm_access": {"roles": ["model.usage.read"]},
                    "resource_access": {},
                }
            ).encode("utf-8")

    def fake_urlopen(req, data=None, timeout=None):
        return _Resp()

    monkeypatch.setattr(token_auth.urllib.request, "urlopen", fake_urlopen)
    verifier = TokenVerifier("config/gateway_policy.json")
    principal = verifier.verify_keycloak_authorization_header("Bearer test-token")

    assert "model.usage.read" in principal.groups


def test_gateway_startup_explains_graph_key_mismatch(monkeypatch):
    def fake_from_policy(policy, **kwargs):
        raise ValueError("sealed graph payload authentication failed")

    monkeypatch.setattr(gateway.GraphStateStore, "from_policy", fake_from_policy)

    with pytest.raises(RuntimeError) as exc_info:
        build_guard("config/gateway_policy.json")

    msg = str(exc_info.value)
    assert "MODELKEYGUARD_GRAPH_KEY" in msg
    assert "restore the original graph key" in msg
    assert "reset the local state" in msg


def test_create_app_passes_graph_key_file_value_to_store(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph_key_file = tmp_path / "graph_key"
    graph_key_file.write_text("file-backed-gateway-key-32-bytes-minimum", encoding="utf-8")
    captured = []
    real_from_policy = gateway.GraphStateStore.from_policy.__func__

    def recording_from_policy(cls, policy, path=None, app_key=None):
        captured.append(app_key)
        return real_from_policy(cls, policy, path=path, app_key=app_key)

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.delenv("MODELKEYGUARD_GRAPH_KEY", raising=False)
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY_FILE", str(graph_key_file))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setattr(gateway.GraphStateStore, "from_policy", classmethod(recording_from_policy))

    TestClient(create_app("config/gateway_policy.json"))

    assert captured[0] == "file-backed-gateway-key-32-bytes-minimum"


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


def test_fastapi_gateway_core_denies_missing_provider_secret_in_real_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    guard, policy = build_guard("config/gateway_policy.json")
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(_payload(), "Bearer kgw_demo_doc_ingestor", guard, policy, verifier)

    assert status == 403
    assert data["error"]["message"] == "provider_secret_missing"
    assert data["error"]["key_id"] == "key:openai:prod"


def test_fastapi_gateway_core_rejects_ambiguous_model_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    guard, policy = build_guard("config/gateway_policy.json")
    policy = dict(policy)
    policy["model_keys"] = [
        {
            "id": "key:ollama:localhost",
            "provider": "ollama",
            "models": ["gemma4:e2b"],
            "secret_ref": "env://OLLAMA_PLACEHOLDER",
        },
        {
            "id": "key:ollama:forwarded",
            "provider": "ollama",
            "models": ["gemma4:e2b"],
            "secret_ref": "env://OLLAMA_PLACEHOLDER",
        },
    ]
    verifier = TokenVerifier("config/gateway_policy.json")

    status, data, _headers = process_chat_completion(
        _payload(model="gemma4:e2b"),
        "Bearer kgw_demo_doc_ingestor",
        guard,
        policy,
        verifier,
    )

    assert status == 403
    assert data["error"]["message"] == "model_key_ambiguous"


def test_openai_compatible_route_rejects_ambiguous_model_keys(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:ollama:a", "ollama", "gemma4:e2b", upstream_url="http://hardware-a:11434/api/chat")
    _register_provider_key(client, "key:ollama:b", "ollama", "gemma4:e2b", upstream_url="http://hardware-b:11434/api/chat")

    response = client.post(
        "/v1/chat/completions",
        json=_payload(model="gemma4:e2b"),
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "model_key_ambiguous"


def test_openai_compatible_route_can_select_explicit_duplicate_model_key(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(client, "key:ollama:a", "ollama", "gemma4:e2b", upstream_url="http://hardware-a:11434/api/chat")
    _register_provider_key(client, "key:ollama:b", "ollama", "gemma4:e2b", upstream_url="http://hardware-b:11434/api/chat")

    payload = _payload(model="gemma4:e2b")
    payload["modelkeyguard"] = {"key_id": "key:ollama:b"}
    response = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )

    assert response.status_code == 200
    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    assert records[-1]["url"] == "http://hardware-b:11434/api/chat"
    forwarded = json.loads(records[-1]["body"])
    assert "modelkeyguard" not in forwarded


def _register_provider_key(client, key_id: str, provider: str, model: str, *, upstream_url: str = ""):
    r = client.post(
        "/admin/keys",
        data={
            "key_id": key_id,
            "provider": provider,
            "models": model,
            "display_name": f"{provider}-{model}",
            "upstream_url": upstream_url,
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


def test_azure_keys_can_use_different_upstream_bases(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("AZURE_OPENAI_UPSTREAM_URL", "https://default-resource.openai.azure.com")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(
        client,
        "key:azure:tenant-a",
        "azure_openai",
        "azure-a",
        upstream_url="https://resource-a.openai.azure.com",
    )
    _register_provider_key(
        client,
        "key:azure:tenant-b",
        "azure_openai",
        "azure-b",
        upstream_url="https://resource-b.openai.azure.com",
    )

    resp_a = client.post(
        "/openai/deployments/azure-a/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-a")["messages"], "max_tokens": 16},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    resp_b = client.post(
        "/openai/deployments/azure-b/chat/completions?api-version=2024-10-21",
        json={"messages": _payload(model="azure-b")["messages"], "max_tokens": 16},
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    azure_records = [r for r in records if r.get("provider") == "azure_openai"]
    assert len(azure_records) >= 2

    urls = [str(r.get("url")) for r in azure_records[-2:]]
    assert any(u.startswith("https://resource-a.openai.azure.com/openai/deployments/azure-a/chat/completions") for u in urls)
    assert any(u.startswith("https://resource-b.openai.azure.com/openai/deployments/azure-b/chat/completions") for u in urls)


def test_openai_key_specific_upstream_base_overrides_global(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("OPENAI_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions")
    client = TestClient(create_app("config/gateway_policy.json"))
    _register_provider_key(
        client,
        "key:openai:custom-upstream",
        "openai",
        "gpt-custom-upstream",
        upstream_url="https://openai-proxy.example",
    )

    response = client.post(
        "/v1/chat/completions",
        json=_payload(model="gpt-custom-upstream"),
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert response.status_code == 200

    records = [json.loads(line) for line in capture_path.read_text().splitlines() if line.strip()]
    openai_records = [r for r in records if r.get("provider") == "openai"]
    assert openai_records
    last = openai_records[-1]
    assert str(last.get("url")).startswith("https://openai-proxy.example/v1/chat/completions")
