from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class AnalyticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeycloakServiceAccount:
    keycloak_url: str
    realm: str
    client_id: str
    client_secret: str
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls, *, prefix: str = "MODELKEYGUARD_OIDC_USAGE_") -> "KeycloakServiceAccount":
        return cls(
            keycloak_url=os.getenv("KEYCLOAK_URL", "http://localhost:8080"),
            realm=os.getenv("KEYCLOAK_REALM", "modelguard"),
            client_id=os.getenv(f"{prefix}CLIENT_ID", "modelguard-usage-agent"),
            client_secret=os.getenv(f"{prefix}CLIENT_SECRET", ""),
            timeout_seconds=int(os.getenv("MODELKEYGUARD_KEYCLOAK_TOKEN_TIMEOUT_SECONDS", "10")),
        )

    def mint_access_token(self) -> str:
        if not self.client_secret:
            raise AnalyticsError("missing Keycloak service-account secret")
        payload = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        url = f"{self.keycloak_url.rstrip('/')}/realms/{self.realm}/protocol/openid-connect/token"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = str(data.get("access_token") or "")
        if not token:
            raise AnalyticsError("Keycloak did not return an access token")
        return token


@dataclass
class UsageAnalyticsClient:
    base_url: str
    bearer_token: str | None = None
    keycloak: KeycloakServiceAccount | None = None
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls) -> "UsageAnalyticsClient":
        token = os.getenv("MODELKEYGUARD_BEARER_TOKEN", "").strip() or None
        keycloak = None
        if not token:
            client_secret = os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", "").strip()
            if client_secret:
                keycloak = KeycloakServiceAccount.from_env()
        return cls(
            base_url=os.getenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"),
            bearer_token=token,
            keycloak=keycloak,
            timeout_seconds=int(os.getenv("MODELKEYGUARD_ANALYTICS_TIMEOUT_SECONDS", "10")),
        )

    def analyze(
        self,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        time_range: str = "24h",
        bucket: str = "hour",
    ) -> dict[str, Any]:
        params: dict[str, str] = {"time_range": time_range, "bucket": bucket}
        if subject_type:
            params["subject_type"] = subject_type
        if subject_id:
            params["subject_id"] = subject_id
        return self._get_json("/admin/usage.json", params=params)

    def for_user(self, user_id: str, *, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return self.analyze(subject_type="user", subject_id=user_id, time_range=time_range, bucket=bucket)

    def for_principal(self, principal_id: str, *, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return self.analyze(subject_type="principal", subject_id=principal_id, time_range=time_range, bucket=bucket)

    def for_key(self, key_id: str, *, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return self.analyze(subject_type="key", subject_id=key_id, time_range=time_range, bucket=bucket)

    def for_token(self, token_id: str, *, time_range: str = "24h", bucket: str = "hour") -> dict[str, Any]:
        return self.analyze(subject_type="token", subject_id=token_id, time_range=time_range, bucket=bucket)

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = self._bearer_token()
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": f"Bearer {token}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise AnalyticsError("usage endpoint did not return a JSON object")
        return payload

    def _bearer_token(self) -> str:
        if self.bearer_token:
            return self.bearer_token
        if not self.keycloak:
            raise AnalyticsError("missing bearer token or Keycloak service account")
        return self.keycloak.mint_access_token()
