from __future__ import annotations

import os
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


@dataclass(frozen=True)
class AppSettings:
    env: str
    host: str
    port: int
    dry_run: bool
    graph_key: str
    auth_mode: str
    audit_path: str
    policy_path: str
    provider_openai_key: str | None

    @classmethod
    def from_env(cls) -> "AppSettings":
        env = read_env_or_file("MODELKEYGUARD_ENV", "local") or "local"
        graph_key = read_env_or_file("MODELKEYGUARD_GRAPH_KEY")
        if not graph_key:
            # local-only fallback; production validation rejects it below.
            graph_key = "dev-modelkeyguard-change-me"
        return cls(
            env=env,
            host=read_env_or_file("MODELKEYGUARD_HOST", "127.0.0.1") or "127.0.0.1",
            port=int(read_env_or_file("MODELKEYGUARD_PORT", "8789") or "8789"),
            dry_run=bool_env("MODELKEYGUARD_DRY_RUN", default=True),
            graph_key=graph_key,
            auth_mode=read_env_or_file("MODELKEYGUARD_AUTH_MODE", "local") or "local",
            audit_path=read_env_or_file("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl") or "out/audit.jsonl",
            policy_path=read_env_or_file("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json") or "config/gateway_policy.json",
            provider_openai_key=read_env_or_file("MODELKEYGUARD_PROVIDER_KEY_OPENAI") or read_env_or_file("OPENAI_API_KEY"),
        )

    def validate_for_startup(self) -> list[str]:
        errors: list[str] = []
        if self.env.lower() in {"prod", "production"}:
            if self.graph_key == "dev-modelkeyguard-change-me":
                errors.append("MODELKEYGUARD_GRAPH_KEY_FILE or MODELKEYGUARD_GRAPH_KEY must be configured for production")
            if len(self.graph_key) < 32:
                errors.append("MODELKEYGUARD_GRAPH_KEY must be at least 32 characters")
            if self.auth_mode not in {"keycloak", "local_or_keycloak"}:
                errors.append("MODELKEYGUARD_AUTH_MODE must be keycloak or local_or_keycloak in production")
        return errors
