from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .graph_state import GraphStateStore, resolve_store_backend
from .policy_loader import load_policy_json
from .settings import read_env_or_file


@dataclass(frozen=True)
class TokenPrincipal:
    principal_id: str
    kind: str
    groups: tuple[str, ...]
    namespace: str
    scopes: tuple[str, ...]
    token_id: str
    expires_at: int | None
    on_behalf_of_user_id: str | None = None


class TokenAuthError(Exception):
    pass


class TokenVerifier:
    """Verifies either local development tokens or Keycloak-issued access tokens.

    Production mode should use Keycloak introspection over TLS. For a one-minute
    local start, the same gateway also accepts configured demo tokens from
    config/gateway_policy.json so the app can run before Docker finishes.
    """

    def __init__(self, policy_path: str | Path = "config/gateway_policy.json") -> None:
        self.policy_path = Path(policy_path)
        self.policy = load_policy_json(self.policy_path)
        if self.policy:
            self.graph_state = GraphStateStore.from_policy(self.policy)
        else:
            store = resolve_store_backend()
            if store == "postgres":
                from .postgres_state import PostgresGraphStateStore

                self.graph_state = PostgresGraphStateStore()
            elif store == "kogwistar_postgres":
                from .kogwistar_postgres_state import KogwistarPostgresGraphStateStore

                self.graph_state = KogwistarPostgresGraphStateStore()
            else:
                self.graph_state = GraphStateStore()
        self.keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
        self.realm = os.getenv("KEYCLOAK_REALM", "modelguard")
        self.introspection_client_id = os.getenv("KEYCLOAK_INTROSPECTION_CLIENT_ID", "modelguard-gateway")
        self.introspection_client_secret = read_env_or_file("KEYCLOAK_INTROSPECTION_CLIENT_SECRET", "gateway-secret") or "gateway-secret"
        self.require_keycloak = os.getenv("MODELKEYGUARD_REQUIRE_KEYCLOAK", "0") == "1"

    def verify_authorization_header(self, header: str | None) -> TokenPrincipal:
        return self.verify_token(self._bearer_token_from_header(header))

    def verify_keycloak_authorization_header(self, header: str | None) -> TokenPrincipal:
        token = self._bearer_token_from_header(header)
        principal = self._verify_keycloak_token(token)
        if principal:
            return principal
        raise TokenAuthError("invalid_or_inactive_keycloak_token")

    def _bearer_token_from_header(self, header: str | None) -> str:
        if not header or not header.lower().startswith("bearer "):
            raise TokenAuthError("missing_bearer_token")
        return header.split(" ", 1)[1].strip()

    def verify_token(self, token: str) -> TokenPrincipal:
        local = self._verify_local_token(token)
        if local and not self.require_keycloak:
            return local
        kc = self._verify_keycloak_token(token)
        if kc:
            return kc
        if local:
            return local
        raise TokenAuthError("invalid_or_inactive_token")

    def _verify_local_token(self, token: str) -> TokenPrincipal | None:
        # Graph-native lookup: token node -> AUTHENTICATES_AS edge -> principal node.
        # Production safe tokens are stored hash-only; legacy local demo tokens may
        # still keep a plaintext token in graph payload for compatibility.
        token_hash = "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
        for node in self.graph_state.nodes.values():
            if node.kind != "auth_token":
                continue
            if node.payload.get("token") != token and node.payload.get("safe_token_hash") != token_hash:
                continue
            exp = node.payload.get("expires_at_epoch")
            if exp is not None and int(exp) < int(time.time()):
                raise TokenAuthError("local_token_expired")
            principal_id = None
            for edge in self.graph_state.edges_from(node.id, "AUTHENTICATES_AS"):
                principal_id = edge.target
                break
            if not principal_id:
                raise TokenAuthError("token_without_principal_edge")
            principal = self.graph_state.nodes.get(principal_id)
            payload = principal.payload if principal else {}
            return TokenPrincipal(
                principal_id=principal_id,
                kind=payload.get("kind", "agent"),
                groups=tuple(payload.get("groups", [])),
                namespace=node.payload.get("namespace", "tenant:kogwistar"),
                scopes=tuple(node.payload.get("scopes", ["model.invoke"])),
                token_id=node.payload.get("jti", node.id.rsplit(":", 1)[-1]),
                on_behalf_of_user_id=node.payload.get("on_behalf_of_user_id"),
                expires_at=exp,
            )
        return None

    def _verify_keycloak_token(self, token: str) -> TokenPrincipal | None:
        url = f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token/introspect"
        data = urllib.parse.urlencode({
            "token": token,
            "client_id": self.introspection_client_id,
            "client_secret": self.introspection_client_secret,
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if not payload.get("active"):
            return None
        client_id = payload.get("client_id") or payload.get("azp") or payload.get("sub", "unknown")
        client_node = self.graph_state.nodes.get(f"keycloak_client:{client_id}")
        principal_id = None
        if client_node:
            for edge in self.graph_state.edges_from(client_node.id, "AUTHENTICATES_AS"):
                principal_id = edge.target
                break
        mapping = client_node.payload if client_node else self.policy.get("keycloak_clients", {}).get(client_id, {})
        principal = self.graph_state.nodes.get(principal_id or "")
        principal_payload = principal.payload if principal else {}
        roles = []
        realm_access = payload.get("realm_access", {})
        if isinstance(realm_access, dict):
            realm_roles = realm_access.get("roles", [])
            if isinstance(realm_roles, list):
                roles.extend(str(role) for role in realm_roles)
        resource_access = payload.get("resource_access", {})
        if isinstance(resource_access, dict):
            for value in resource_access.values():
                if not isinstance(value, dict):
                    continue
                resource_roles = value.get("roles", [])
                if isinstance(resource_roles, list):
                    roles.extend(str(role) for role in resource_roles)
        scope_string = payload.get("scope", "")
        scopes = tuple(sorted(set(mapping.get("scopes", []) + scope_string.split()))) or ("model.invoke",)
        groups = tuple(principal_payload.get("groups", mapping.get("groups", [])) + roles)
        return TokenPrincipal(
            principal_id=principal_id or mapping.get("principal_id", f"service:{client_id}"),
            kind=principal_payload.get("kind", mapping.get("kind", "service")),
            groups=groups,
            namespace=mapping.get("namespace", payload.get("namespace", "tenant:kogwistar")),
            scopes=scopes,
            on_behalf_of_user_id=mapping.get("on_behalf_of_user_id", payload.get("on_behalf_of_user_id")),
            token_id=payload.get("jti", payload.get("sub", token[-12:])),
            expires_at=payload.get("exp"),
        )
