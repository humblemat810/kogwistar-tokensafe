from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .analytics import KeycloakServiceAccount, UsageAnalyticsClient
from .governance_runtime import run_usage_analysis_runtime


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


@dataclass
class UsageAnalysisAgent:
    """Debugger-friendly usage analysis workflow scaffold.

    The object acquires its runtime configuration from environment variables by
    default, can mint its own service-account token, and keeps the reusable
    analytics client available on ``client`` for interactive debugging.

    Edit the environment variable names in :meth:`from_env` if your deployment
    uses different gateway, Keycloak, or subject conventions. Put your actual
    scoring, enrichment, or decision logic in :meth:`run` or :meth:`collect`.
    """

    client: UsageAnalyticsClient
    time_range: str = "24h"
    bucket: str = "hour"
    user_subjects: list[str] = field(default_factory=list)
    principal_subjects: list[str] = field(default_factory=list)
    key_subjects: list[str] = field(default_factory=list)
    runtime_mode: str = "sync"

    @classmethod
    def from_env(cls) -> "UsageAnalysisAgent":
        # These are the knobs most people will edit first:
        # - MODELKEYGUARD_GATEWAY_PUBLIC_URL: where the gateway lives
        # - KEYCLOAK_URL / KEYCLOAK_REALM: where token minting happens
        # - MODELKEYGUARD_OIDC_USAGE_CLIENT_ID / CLIENT_SECRET: service account
        # - MODELKEYGUARD_ANALYTICS_SUBJECT_*: the subjects you want to inspect
        bearer_token = _env("MODELKEYGUARD_BEARER_TOKEN")
        client_secret = _env("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET")
        keycloak = None
        if not bearer_token and client_secret:
            keycloak = KeycloakServiceAccount.from_env()
        client = UsageAnalyticsClient(
            base_url=_env("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"),
            bearer_token=bearer_token or None,
            keycloak=keycloak,
            timeout_seconds=int(_env("MODELKEYGUARD_ANALYTICS_TIMEOUT_SECONDS", "10")),
        )
        return cls(
            client=client,
            time_range=_env("MODELKEYGUARD_ANALYTICS_TIME_RANGE", "24h"),
            bucket=_env("MODELKEYGUARD_ANALYTICS_BUCKET", "hour"),
            user_subjects=_csv_env(
                "MODELKEYGUARD_ANALYTICS_SUBJECT_USERS",
                _env("MODELKEYGUARD_ANALYTICS_SUBJECT_USER", "user:alice"),
            ),
            principal_subjects=_csv_env(
                "MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPALS",
                _env("MODELKEYGUARD_ANALYTICS_SUBJECT_PRINCIPAL", "agent:doc-ingestor"),
            ),
            key_subjects=_csv_env(
                "MODELKEYGUARD_ANALYTICS_SUBJECT_KEYS",
                _env("MODELKEYGUARD_ANALYTICS_SUBJECT_KEY", "key:openai:prod"),
            ),
            runtime_mode=_env("MODELKEYGUARD_RUNTIME_MODE", "sync"),
        )

    def run(self) -> dict[str, Any]:
        # Runtime-native execution path backed by Kogwistar WorkflowRuntime /
        # AsyncWorkflowRuntime primitives.
        return run_usage_analysis_runtime(
            client=self.client,
            base_url=self.client.base_url,
            time_range=self.time_range,
            bucket=self.bucket,
            user_subjects=list(self.user_subjects),
            principal_subjects=list(self.principal_subjects),
            key_subjects=list(self.key_subjects),
            runtime_mode=self.runtime_mode,
        )

    def render(self) -> str:
        return json.dumps(self.run(), indent=2, sort_keys=True)

    def collect(self) -> dict[str, dict[str, Any]]:
        # Edit the subject lists above if you want a different set of lanes or
        # subject IDs to probe by default.
        results: dict[str, dict[str, Any]] = {}
        for subject_type, subjects in (
            ("user", self.user_subjects),
            ("principal", self.principal_subjects),
            ("key", self.key_subjects),
        ):
            lane_results: dict[str, Any] = {}
            for subject_id in subjects:
                lane_results[subject_id] = self.client.analyze(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    time_range=self.time_range,
                    bucket=self.bucket,
                )
            results[subject_type] = lane_results
        return results


def _csv_env(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    values = [item.strip() for item in raw.split(",")]
    return [item for item in values if item]
