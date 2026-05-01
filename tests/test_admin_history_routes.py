from __future__ import annotations

import json

import pytest

from modelkeyguard.gateway import create_app

ADMIN_HEADERS = {"x-modelkeyguard-admin-secret": "dev-modelkeyguard-admin-secret"}
EXPECTED_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


@pytest.fixture(autouse=True)
def _history_routes_test_env(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_STORE", "jsonl")
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_KEY", "test-history-key-32-bytes-minimum!")


def _payload(stream: bool = False):
    return {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": EXPECTED_SYSTEM},
            {"role": "user", "content": "Summarize this test document."},
        ],
        "max_tokens": 8,
        "stream": stream,
    }


def test_history_capture_for_success_error_and_stream(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    ok = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    assert ok.status_code == 200

    denied = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer bad-token"})
    assert denied.status_code == 401

    stream = client.post(
        "/v1/chat/completions",
        json=_payload(stream=True),
        headers={"Authorization": "Bearer kgw_demo_doc_ingestor"},
    )
    assert stream.status_code == 200
    assert "data: [DONE]" in stream.text

    rows = client.get("/admin/history.json", headers=ADMIN_HEADERS, params={"time_range": "24h", "page_size": 200}).json()["data"]
    assert len(rows) >= 3

    allowed_rows = [r for r in rows if r.get("http_status") == 200]
    assert allowed_rows

    stream_row = next((r for r in rows if int(r.get("stream_chunk_count") or 0) > 0), None)
    assert stream_row is not None

    detail = client.get(f"/admin/history/{stream_row['request_id']}.json", headers=ADMIN_HEADERS)
    assert detail.status_code == 200
    detail_json = detail.json()
    assert detail_json["request_body_text"]
    assert detail_json["response_body_text"]
    assert detail_json["stream_chunks"]


def test_history_filtering_and_pagination_are_stable(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    client = TestClient(create_app("config/gateway_policy.json"))

    for _ in range(3):
        resp = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
        assert resp.status_code == 200

    page1 = client.get(
        "/admin/history.json",
        headers=ADMIN_HEADERS,
        params={"provider": "openai", "decision": "ALLOWED", "page": 1, "page_size": 1, "time_range": "24h"},
    ).json()
    page2 = client.get(
        "/admin/history.json",
        headers=ADMIN_HEADERS,
        params={"provider": "openai", "decision": "ALLOWED", "page": 2, "page_size": 1, "time_range": "24h"},
    ).json()

    assert page1["total"] >= 3
    assert len(page1["data"]) == 1
    assert len(page2["data"]) == 1
    assert page1["data"][0]["request_id"] != page2["data"][0]["request_id"]


def test_history_config_update_and_window_enforcement(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "1")
    monkeypatch.setenv("MODELKEYGUARD_HISTORY_MAX_ACTIVE_RECORDS", "100")
    client = TestClient(create_app("config/gateway_policy.json"))

    cfg = client.get("/admin/history/config", headers=ADMIN_HEADERS)
    assert cfg.status_code == 200
    assert cfg.json()["retention_days"] == 30

    updated = client.post(
        "/admin/history/config",
        headers=ADMIN_HEADERS,
        json={"max_active_records": 1, "max_active_bytes": 10_000_000, "retention_days": 30, "enabled": True},
    )
    assert updated.status_code == 200
    assert updated.json()["max_active_records"] == 1

    first = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    second = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    assert first.status_code == 200 and second.status_code == 200

    rows = client.get("/admin/history.json", headers=ADMIN_HEADERS, params={"time_range": "24h", "page_size": 200}).json()
    assert rows["active_window"]["max_active_records"] == 1
    assert len(rows["data"]) == 1


def test_history_capture_never_leaks_provider_secret(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    capture_path = tmp_path / "capture.jsonl"
    monkeypatch.setenv("MODELKEYGUARD_GRAPH_PATH", str(tmp_path / "graph.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODELKEYGUARD_DRY_RUN", "0")
    monkeypatch.setenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-real-upstream-key")
    client = TestClient(create_app("config/gateway_policy.json"))

    response = client.post("/v1/chat/completions", json=_payload(), headers={"Authorization": "Bearer kgw_demo_doc_ingestor"})
    assert response.status_code == 200

    rows = client.get("/admin/history.json", headers=ADMIN_HEADERS, params={"time_range": "24h", "page_size": 20}).json()["data"]
    assert rows
    detail = client.get(f"/admin/history/{rows[0]['request_id']}.json", headers=ADMIN_HEADERS)
    assert detail.status_code == 200
    serialized = json.dumps(detail.json(), sort_keys=True)
    assert "super-secret-real-upstream-key" not in serialized
