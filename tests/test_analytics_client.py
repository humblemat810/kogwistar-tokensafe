from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from modelkeyguard.analytics import KeycloakServiceAccount, UsageAnalyticsClient
from modelkeyguard.usage_agent import UsageAnalysisAgent


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_usage_analytics_client_mints_token_and_queries_subjects(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        calls.append(req.full_url)
        if req.full_url.endswith("/protocol/openid-connect/token"):
            return _FakeResponse({"access_token": "kc-token-123"})
        if req.full_url.startswith("http://gateway.example/admin/usage.json"):
            auth = req.headers.get("Authorization") or req.headers.get("authorization")
            assert auth == "Bearer kc-token-123"
            query = parse_qs(urlparse(req.full_url).query)
            return _FakeResponse(
                {
                    "overview": {"requests": 7},
                    "filters": {
                        "subject_type": query.get("subject_type", [None])[0],
                        "subject_id": query.get("subject_id", [None])[0],
                    },
                }
            )
        raise AssertionError(f"unexpected url: {req.full_url}")

    monkeypatch.setattr("modelkeyguard.analytics.urllib.request.urlopen", fake_urlopen)

    account = KeycloakServiceAccount(
        keycloak_url="http://keycloak.example",
        realm="modelguard",
        client_id="modelguard-usage-agent",
        client_secret="usage-secret",
    )
    client = UsageAnalyticsClient(base_url="http://gateway.example", keycloak=account)

    assert account.mint_access_token() == "kc-token-123"
    user = client.for_user("user:alice")
    principal = client.for_principal("agent:doc-ingestor")
    key = client.for_key("key:openai:prod")

    assert user["filters"]["subject_id"] == "user:alice"
    assert principal["filters"]["subject_id"] == "agent:doc-ingestor"
    assert key["filters"]["subject_id"] == "key:openai:prod"
    assert any(url.endswith("/protocol/openid-connect/token") for url in calls)
    assert any("subject_type=user" in url and "subject_id=user%3Aalice" in url for url in calls)
    assert any("subject_type=principal" in url and "subject_id=agent%3Adoc-ingestor" in url for url in calls)
    assert any("subject_type=key" in url and "subject_id=key%3Aopenai%3Aprod" in url for url in calls)


def test_usage_analysis_agent_script_runs_without_compose(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "usage_analysis_agent.py"
    monkeypatch.delenv("MODELKEYGUARD_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_ANALYTICS_SUBJECT_USER", raising=False)
    monkeypatch.delenv("MODELKEYGUARD_ANALYTICS_SUBJECT_KEY", raising=False)
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPAL", "agent:doc-ingestor")

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        parsed = urlparse(req.full_url)
        query = parse_qs(parsed.query)
        if parsed.path == "/admin/usage.json" and query.get("subject_type", [None])[0] == "principal" and query.get("subject_id", [None])[0] == "agent:doc-ingestor":
            return _FakeResponse({"overview": {"requests": 3}, "filters": {"subject_type": "principal", "subject_id": "agent:doc-ingestor"}})
        raise AssertionError(f"unexpected url: {req.full_url}")

    monkeypatch.setattr("modelkeyguard.analytics.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", [str(script), "--bearer-token", "test-bearer-token", "--principal", "agent:doc-ingestor"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script), run_name="__main__")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert '"principal"' in out
    assert '"agent:doc-ingestor"' in out
    assert '"requests": 3' in out


def test_usage_analysis_agent_from_env_builds_real_scaffold(monkeypatch):
    monkeypatch.setenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://gateway.example")
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "modelguard")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_ID", "modelguard-usage-agent")
    monkeypatch.setenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", "usage-secret")
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_TIME_RANGE", "7d")
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_BUCKET", "day")
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_SUBJECT_USER", "user:alice")
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPAL", "agent:doc-ingestor")
    monkeypatch.setenv("MODELKEYGUARD_ANALYTICS_SUBJECT_KEY", "key:openai:prod")
    monkeypatch.delenv("MODELKEYGUARD_BEARER_TOKEN", raising=False)

    sentinel_account = object()

    def fake_from_env():
        return sentinel_account

    monkeypatch.setattr("modelkeyguard.usage_agent.KeycloakServiceAccount.from_env", fake_from_env)
    monkeypatch.setattr(
        "modelkeyguard.analytics.UsageAnalyticsClient.analyze",
        lambda self, subject_type=None, subject_id=None, time_range="24h", bucket="hour": {
            "filters": {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "time_range": time_range,
                "bucket": bucket,
            }
        },
    )

    agent = UsageAnalysisAgent.from_env()
    assert agent.client.base_url == "http://gateway.example"
    assert agent.client.keycloak is sentinel_account
    assert agent.time_range == "7d"
    assert agent.bucket == "day"
    assert agent.user_subjects == ["user:alice"]
    assert agent.principal_subjects == ["agent:doc-ingestor"]
    assert agent.key_subjects == ["key:openai:prod"]

    report = agent.run()
    assert report["results"]["user"]["user:alice"]["filters"]["subject_id"] == "user:alice"
    assert report["results"]["principal"]["agent:doc-ingestor"]["filters"]["bucket"] == "day"
    assert report["results"]["key"]["key:openai:prod"]["filters"]["time_range"] == "7d"


@pytest.mark.skipif(not os.getenv("MODELKEYGUARD_ANALYTICS_LIVE_SMOKE"), reason="opt-in live smoke only")
def test_usage_analysis_agent_live_smoke_from_env():
    client = UsageAnalyticsClient.from_env()
    if client.keycloak is None:
        pytest.skip("set MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET to run the live smoke")
    data = client.for_principal("agent:doc-ingestor")
    assert data["overview"]["requests"] >= 0
    assert data["filters"]["subject_type"] == "principal"
