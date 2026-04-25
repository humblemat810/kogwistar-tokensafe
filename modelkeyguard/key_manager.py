from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import time

from .graph_state import GraphStateStore
from .sealed_payload import open_json, seal_json


@dataclass(frozen=True)
class KeyView:
    key_id: str
    provider: str
    models: tuple[str, ...]
    display_name: str
    status: str
    active_secret_ref: str | None
    expires_at_epoch: int | None


def now_epoch() -> int:
    return int(time.time())


class KeyLifecycleError(Exception):
    pass


class KeyManager:
    """Graph-native key lifecycle manager.

    Raw provider keys enter only through create/rotate methods. They are sealed
    immediately into graph payload nodes and are never returned by any view/API.
    """

    def __init__(self, graph_state: GraphStateStore, app_key: str) -> None:
        self.graph_state = graph_state
        self.app_key = app_key

    def create_key(
        self,
        *,
        key_id: str,
        provider: str,
        models: list[str],
        display_name: str,
        provider_secret: str,
        created_by: str,
        expires_at_epoch: int | None = None,
    ) -> KeyView:
        if not key_id.startswith("key:"):
            raise KeyLifecycleError("key_id_must_start_with_key_colon")
        if not provider_secret:
            raise KeyLifecycleError("provider_secret_required")
        if key_id in self.graph_state.nodes:
            raise KeyLifecycleError("model_key_already_exists")
        secret_ref = self._create_secret_payload(key_id, provider_secret, created_by, expires_at_epoch)
        self.graph_state.put_node(key_id, "model_key", {
            "provider": provider,
            "models": models,
            "display_name": display_name or key_id,
            "status": "active",
            "active_secret_ref": secret_ref,
            "created_by": created_by,
            "created_at_epoch": now_epoch(),
        })
        self.graph_state.put_edge(f"edge:{key_id}:HAS_ACTIVE_SECRET:{secret_ref}", "HAS_ACTIVE_SECRET", key_id, secret_ref, {})
        self.graph_state.append_event("MODEL_KEY_CREATED", key_id, {"key_id": key_id, "provider": provider, "models": models, "created_by": created_by})
        return self.get_key_view(key_id)  # type: ignore[return-value]

    def rotate_key(self, *, key_id: str, provider_secret: str, rotated_by: str, expires_at_epoch: int | None = None) -> KeyView:
        node = self.graph_state.nodes.get(key_id)
        if not node or node.kind != "model_key":
            raise KeyLifecycleError("model_key_not_found")
        if node.payload.get("status") == "revoked":
            raise KeyLifecycleError("model_key_revoked")
        old = node.payload.get("active_secret_ref")
        secret_ref = self._create_secret_payload(key_id, provider_secret, rotated_by, expires_at_epoch)
        payload = dict(node.payload)
        payload["active_secret_ref"] = secret_ref
        payload["rotated_at_epoch"] = now_epoch()
        payload["rotated_by"] = rotated_by
        self.graph_state.put_node(key_id, "model_key", payload)
        self.graph_state.put_edge(f"edge:{key_id}:HAS_ACTIVE_SECRET:{secret_ref}", "HAS_ACTIVE_SECRET", key_id, secret_ref, {"previous": old})
        if old and old in self.graph_state.nodes:
            old_payload = dict(self.graph_state.nodes[old].payload)
            old_payload["status"] = "rotated"
            old_payload["rotated_at_epoch"] = now_epoch()
            self.graph_state.put_node(old, "sealed_secret_payload", old_payload)
        self.graph_state.append_event("MODEL_KEY_ROTATED", key_id, {"key_id": key_id, "rotated_by": rotated_by, "old_secret_ref": old, "new_secret_ref": secret_ref})
        return self.get_key_view(key_id)  # type: ignore[return-value]

    def revoke_key(self, *, key_id: str, revoked_by: str, reason: str = "") -> KeyView:
        node = self.graph_state.nodes.get(key_id)
        if not node or node.kind != "model_key":
            raise KeyLifecycleError("model_key_not_found")
        payload = dict(node.payload)
        payload["status"] = "revoked"
        payload["revoked_at_epoch"] = now_epoch()
        payload["revoked_by"] = revoked_by
        payload["revoked_reason"] = reason
        self.graph_state.put_node(key_id, "model_key", payload)
        secret_ref = payload.get("active_secret_ref")
        if secret_ref and secret_ref in self.graph_state.nodes:
            sp = dict(self.graph_state.nodes[secret_ref].payload)
            sp["status"] = "revoked"
            self.graph_state.put_node(secret_ref, "sealed_secret_payload", sp)
        self.graph_state.append_event("MODEL_KEY_REVOKED", key_id, {"key_id": key_id, "revoked_by": revoked_by, "reason": reason})
        return self.get_key_view(key_id)  # type: ignore[return-value]

    def list_key_views(self) -> list[KeyView]:
        return [self._view_from_node(n.id, n.payload) for n in self.graph_state.nodes.values() if n.kind == "model_key"]

    def get_key_view(self, key_id: str) -> KeyView | None:
        node = self.graph_state.nodes.get(key_id)
        if not node or node.kind != "model_key":
            return None
        return self._view_from_node(key_id, node.payload)

    def resolve_provider_secret(self, secret_ref: str) -> str:
        node = self.graph_state.nodes.get(secret_ref)
        if not node or node.kind != "sealed_secret_payload":
            raise KeyLifecycleError("secret_payload_not_found")
        payload = node.payload
        status = payload.get("status", "active")
        if status != "active":
            raise KeyLifecycleError(f"secret_payload_{status}")
        exp = payload.get("expires_at_epoch")
        if exp is not None and int(exp) < now_epoch():
            raise KeyLifecycleError("secret_payload_expired")
        sealed = payload.get("sealed")
        if not isinstance(sealed, dict):
            raise KeyLifecycleError("secret_payload_corrupt")
        opened = open_json(sealed, self.app_key)
        secret = opened.get("provider_secret")
        if not secret:
            raise KeyLifecycleError("provider_secret_missing")
        return str(secret)

    def _create_secret_payload(self, key_id: str, provider_secret: str, actor: str, expires_at_epoch: int | None) -> str:
        secret_ref = f"secret:{key_id}:{now_epoch()}:{len(self.graph_state.nodes)+1}"
        sealed = seal_json({"provider_secret": provider_secret}, self.app_key)
        self.graph_state.put_node(secret_ref, "sealed_secret_payload", {
            "key_id": key_id,
            "status": "active",
            "sealed": sealed,
            "created_by": actor,
            "created_at_epoch": now_epoch(),
            "expires_at_epoch": expires_at_epoch,
        })
        self.graph_state.append_event("SEALED_SECRET_PAYLOAD_CREATED", secret_ref, {"secret_ref": secret_ref, "key_id": key_id, "created_by": actor, "expires_at_epoch": expires_at_epoch})
        return secret_ref

    def _view_from_node(self, key_id: str, payload: dict[str, Any]) -> KeyView:
        secret_ref = payload.get("active_secret_ref") or payload.get("secret_ref")
        exp = None
        if secret_ref and secret_ref in self.graph_state.nodes:
            exp = self.graph_state.nodes[secret_ref].payload.get("expires_at_epoch")
        return KeyView(
            key_id=key_id,
            provider=str(payload.get("provider", "unknown")),
            models=tuple(payload.get("models", [])),
            display_name=str(payload.get("display_name", key_id)),
            status=str(payload.get("status", "active")),
            active_secret_ref=secret_ref,
            expires_at_epoch=exp,
        )


def html_escape(s: object) -> str:
    import html
    return html.escape(str(s), quote=True)
