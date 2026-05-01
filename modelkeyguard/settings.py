from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def read_env_or_file(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    file_name = f"{name}_FILE"
    if file_name in os.environ and os.environ[file_name]:
        value = Path(os.environ[file_name]).read_text(encoding="utf-8").strip()
    else:
        value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"missing required setting: {name} or {file_name}")
    return value


def bool_env(name: str, default: bool = False) -> bool:
    v = read_env_or_file(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _allow_dev_graph_key() -> bool:
    return os.getenv("MODELKEYGUARD_ALLOW_DEV_GRAPH_KEY", "").strip().lower() in {"1", "true", "yes", "on"}


def _warn_dev_graph_key() -> None:
    print(
        "WARNING: using dev fallback MODELKEYGUARD_GRAPH_KEY. "
        "Set MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY for real state.",
        file=sys.stderr,
    )


@dataclass(frozen=True)
class AppSettings:
    env: str
    host: str
    port: int
    gateway_public_url: str
    dry_run: bool
    graph_key: str
    auth_mode: str
    keycloak_url: str
    keycloak_public_url: str
    keycloak_realm: str
    audit_path: str
    policy_path: str
    provider_openai_key: str | None
    admin_api_secret: str
    admin_auth_mode: str
    admin_required_role: str
    usage_required_role: str
    browser_oidc_client_id: str
    admin_session_ttl_seconds: int
    require_model_list_auth: bool
    history_enabled: bool
    history_retention_days: int
    history_max_active_records: int
    history_max_active_bytes: int
    kogwistar_embed_dim: int
    kogwistar_enforce_installed_only: bool

    @classmethod
    def from_env(cls) -> "AppSettings":
        env = read_env_or_file("MODELKEYGUARD_ENV", "local") or "local"
        graph_key = read_env_or_file("MODELKEYGUARD_GRAPH_KEY")
        if not graph_key:
            if _allow_dev_graph_key():
                _warn_dev_graph_key()
                graph_key = "dev-modelkeyguard-change-me"
            else:
                graph_key = ""
        return cls(
            env=env,
            host=read_env_or_file("MODELKEYGUARD_HOST", "127.0.0.1") or "127.0.0.1",
            port=int(read_env_or_file("MODELKEYGUARD_PORT", "8789") or "8789"),
            gateway_public_url=read_env_or_file("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789") or "http://127.0.0.1:8789",
            dry_run=bool_env("MODELKEYGUARD_DRY_RUN", default=True),
            graph_key=graph_key,
            auth_mode=read_env_or_file("MODELKEYGUARD_AUTH_MODE", "local") or "local",
            keycloak_url=read_env_or_file("KEYCLOAK_URL", "http://localhost:8080") or "http://localhost:8080",
            keycloak_public_url=read_env_or_file("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL", "http://127.0.0.1:8080") or "http://127.0.0.1:8080",
            keycloak_realm=read_env_or_file("KEYCLOAK_REALM", "modelguard") or "modelguard",
            audit_path=read_env_or_file("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl") or "out/audit.jsonl",
            policy_path=read_env_or_file("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json") or "config/gateway_policy.json",
            provider_openai_key=read_env_or_file("MODELKEYGUARD_PROVIDER_KEY_OPENAI") or read_env_or_file("OPENAI_API_KEY"),
            admin_api_secret=read_env_or_file("MODELKEYGUARD_ADMIN_API_SECRET", "dev-modelkeyguard-admin-secret") or "dev-modelkeyguard-admin-secret",
            admin_auth_mode=read_env_or_file("MODELKEYGUARD_ADMIN_AUTH_MODE", "secret") or "secret",
            admin_required_role=read_env_or_file("MODELKEYGUARD_ADMIN_REQUIRED_ROLE", "model.admin") or "model.admin",
            usage_required_role=read_env_or_file("MODELKEYGUARD_USAGE_REQUIRED_ROLE", "model.usage.read") or "model.usage.read",
            browser_oidc_client_id=read_env_or_file("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID", "modelguard-admin-web") or "modelguard-admin-web",
            admin_session_ttl_seconds=int(read_env_or_file("MODELKEYGUARD_ADMIN_SESSION_TTL_SECONDS", "3600") or "3600"),
            require_model_list_auth=bool_env("MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH", default=False),
            history_enabled=bool_env("MODELKEYGUARD_HISTORY_ENABLED", default=True),
            history_retention_days=int(read_env_or_file("MODELKEYGUARD_HISTORY_RETENTION_DAYS", "30") or "30"),
            history_max_active_records=int(read_env_or_file("MODELKEYGUARD_HISTORY_MAX_ACTIVE_RECORDS", "10000") or "10000"),
            history_max_active_bytes=int(read_env_or_file("MODELKEYGUARD_HISTORY_MAX_ACTIVE_BYTES", "52428800") or "52428800"),
            kogwistar_embed_dim=int(read_env_or_file("MODELKEYGUARD_KOGWISTAR_EMBED_DIM", "2") or "2"),
            kogwistar_enforce_installed_only=bool_env("MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY", default=True),
        )

    def validate_for_startup(self) -> list[str]:
        errors: list[str] = []
        if self.env.lower() in {"prod", "production"}:
            if not self.graph_key:
                errors.append("MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY must be configured for production")
            elif self.graph_key == "dev-modelkeyguard-change-me":
                errors.append("MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY must be configured for production")
            if len(self.graph_key) < 32:
                errors.append("MODELKEYGUARD_GRAPH_KEY must be at least 32 characters")
            if self.auth_mode not in {"keycloak", "local_or_keycloak"}:
                errors.append("MODELKEYGUARD_AUTH_MODE must be keycloak or local_or_keycloak in production")
            if self.admin_auth_mode not in {"secret", "keycloak", "secret_or_keycloak"}:
                errors.append("MODELKEYGUARD_ADMIN_AUTH_MODE must be secret, keycloak, or secret_or_keycloak")
            if self.admin_auth_mode in {"secret", "secret_or_keycloak"} and self.admin_api_secret == "dev-modelkeyguard-admin-secret":
                errors.append("MODELKEYGUARD_ADMIN_API_SECRET_FILE or MODELKEYGUARD_ADMIN_API_SECRET must be configured for production")
            if self.admin_auth_mode in {"secret", "secret_or_keycloak"} and len(self.admin_api_secret) < 16:
                errors.append("MODELKEYGUARD_ADMIN_API_SECRET must be at least 16 characters")
        elif self.admin_auth_mode not in {"secret", "keycloak", "secret_or_keycloak"}:
            errors.append("MODELKEYGUARD_ADMIN_AUTH_MODE must be secret, keycloak, or secret_or_keycloak")
        if self.admin_session_ttl_seconds <= 0:
            errors.append("MODELKEYGUARD_ADMIN_SESSION_TTL_SECONDS must be > 0")
        if not self.gateway_public_url.startswith(("http://", "https://")):
            errors.append("MODELKEYGUARD_GATEWAY_PUBLIC_URL must start with http:// or https://")
        if not self.keycloak_url.startswith(("http://", "https://")):
            errors.append("KEYCLOAK_URL must start with http:// or https://")
        if not self.keycloak_public_url.startswith(("http://", "https://")):
            errors.append("MODELKEYGUARD_KEYCLOAK_PUBLIC_URL must start with http:// or https://")
        if self.admin_auth_mode in {"keycloak", "secret_or_keycloak"} and not self.admin_required_role:
            errors.append("MODELKEYGUARD_ADMIN_REQUIRED_ROLE must be configured when Keycloak admin auth is enabled")
        if self.admin_auth_mode in {"keycloak", "secret_or_keycloak"} and not self.usage_required_role:
            errors.append("MODELKEYGUARD_USAGE_REQUIRED_ROLE must be configured when Keycloak admin auth is enabled")
        if self.admin_auth_mode in {"keycloak", "secret_or_keycloak"} and not self.browser_oidc_client_id:
            errors.append("MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID must be configured when browser OIDC admin auth is enabled")
        if self.history_retention_days <= 0:
            errors.append("MODELKEYGUARD_HISTORY_RETENTION_DAYS must be > 0")
        if self.history_max_active_records <= 0:
            errors.append("MODELKEYGUARD_HISTORY_MAX_ACTIVE_RECORDS must be > 0")
        if self.history_max_active_bytes <= 0:
            errors.append("MODELKEYGUARD_HISTORY_MAX_ACTIVE_BYTES must be > 0")
        if self.kogwistar_embed_dim < 1 or self.kogwistar_embed_dim > 8:
            errors.append("MODELKEYGUARD_KOGWISTAR_EMBED_DIM must be between 1 and 8")
        return errors
