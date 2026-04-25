from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .core import ModelKey, ModelKeyGuard, Principal, Request
from .graph_state import GraphStateStore
from .token_auth import TokenAuthError, TokenVerifier

DEFAULT_POLICY = Path("config/gateway_policy.json")
AUDIT_PATH = Path(os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_system_prompt(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")


def build_guard(policy_path: str | Path = DEFAULT_POLICY) -> tuple[ModelKeyGuard, dict[str, Any]]:
    policy = json.loads(Path(policy_path).read_text())
    graph_state = GraphStateStore.from_policy(policy)
    guard = ModelKeyGuard.create()
    guard.graph_state = graph_state
    for item in policy["model_keys"]:
        guard.register_key(ModelKey(
            id=item["id"],
            provider=item["provider"],
            models=tuple(item["models"]),
            secret_ref=item.get("secret_ref"),
            display_name=item.get("display_name", item["id"]),
            approval_threshold_usd=float(item.get("approval_threshold_usd", 999999.0)),
        ))
        acl = item["acl"]
        guard.grant(
            key_id=item["id"],
            mode=acl.get("mode", "scope"),
            created_by=acl.get("created_by", "human:platform-admin"),
            owner_id=acl.get("owner_id"),
            namespace=acl.get("namespace"),
            shared_with_principals=tuple(acl.get("shared_with_principals", [])),
            shared_with_groups=tuple(acl.get("shared_with_groups", [])),
        )
    return guard, policy


def select_key(policy: dict[str, Any], model: str) -> str | None:
    for key in policy["model_keys"]:
        if model in key["models"]:
            return key["id"]
    return None


def estimate_cost_and_tokens(payload: dict[str, Any], policy: dict[str, Any]) -> tuple[float, int]:
    model = payload.get("model", "")
    price = float(policy.get("model_price_per_1k_tokens_usd", {}).get(model, 0.002))
    messages = payload.get("messages", [])
    chars = len(json.dumps(messages))
    max_tokens = int(payload.get("max_tokens", payload.get("max_completion_tokens", 512)) or 512)
    estimated_tokens = max(1, chars // 4 + max_tokens)
    return round((estimated_tokens / 1000.0) * price, 6), estimated_tokens


def append_audit(event: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def resolve_secret(secret_ref: str) -> str | None:
    if secret_ref.startswith("env://"):
        return os.getenv(secret_ref.removeprefix("env://"))
    return None


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ModelKeyGuardGateway/0.3"

    def _json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True, "service": "modelkeyguard-gateway"})
            return
        if self.path == "/v1/models":
            models = []
            for key in self.server.policy["model_keys"]:  # type: ignore[attr-defined]
                models.extend({"id": m, "object": "model", "owned_by": key["provider"]} for m in key["models"])
            self._json(200, {"object": "list", "data": models})
            return
        self._json(404, {"error": {"message": "not_found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": {"message": "invalid_json"}})
            return
        if self.path not in {"/v1/chat/completions", "/v1/responses"}:
            self._json(404, {"error": {"message": "unsupported_endpoint"}})
            return
        try:
            principal_token = self.server.verifier.verify_authorization_header(self.headers.get("authorization"))  # type: ignore[attr-defined]
        except TokenAuthError as e:
            self._json(401, {"error": {"message": str(e)}})
            if hasattr(self.server, "graph_state"):
                self.server.graph_state.append_access_conversation_event("auth-failed", "AUTH_TOKEN_DENIED", {"reason": str(e)})  # type: ignore[attr-defined]
            return

        model = payload.get("model")
        key_id = select_key(self.server.policy, model)  # type: ignore[attr-defined]
        if not key_id:
            self._json(403, {"error": {"message": "model_not_registered"}})
            return

        messages = payload.get("messages", [])
        system_prompt = extract_system_prompt(messages) if isinstance(messages, list) else ""
        system_hash = sha256_text(system_prompt) if system_prompt else None
        profile = self.server.policy.get("usage_profiles", {}).get(principal_token.principal_id, {})  # type: ignore[attr-defined]
        expected_hashes = set(profile.get("system_prompt_hashes", []))
        if expected_hashes and system_hash not in expected_hashes:
            event = self._base_event(principal_token, payload, key_id, "BLOCKED", "system_prompt_signature_mismatch", system_hash)
            append_audit(event)
            self.server.graph_state.append_access_conversation_event(event["request_id"], "ACL_DECISION_DENY", event)  # type: ignore[attr-defined]
            self._json(403, {"error": {"message": "system_prompt_signature_mismatch", "system_prompt_hash": system_hash}})
            return

        cost, estimated_tokens = estimate_cost_and_tokens(payload, self.server.policy)  # type: ignore[attr-defined]
        base_event = self._base_event(principal_token, payload, key_id, "PENDING", "request_received", system_hash)
        decision = self.server.guard.check(Request(  # type: ignore[attr-defined]
            principal=Principal(principal_token.principal_id, principal_token.kind, principal_token.groups),
            key_id=key_id,
            model=model,
            namespace=principal_token.namespace,
            estimated_cost_usd=cost,
            estimated_tokens=estimated_tokens,
            reason="gateway proxy request",
            request_id=base_event["request_id"],
            token_id=principal_token.token_id,
            on_behalf_of_user_id=principal_token.on_behalf_of_user_id,
        ))
        event = dict(base_event)
        event.update({
            "decision": "APPROVAL_REQUIRED" if decision.requires_approval else ("ALLOWED" if decision.allowed else "BLOCKED"),
            "reason": decision.reason,
            "estimated_cost_usd": cost,
            "estimated_tokens": estimated_tokens,
            "acl_reason": decision.acl_reason,
            "remaining": decision.remaining,
            "on_behalf_of_user_id": principal_token.on_behalf_of_user_id,
        })
        append_audit(event)
        if not decision.allowed:
            self._json(decision.http_status, {"error": {"message": decision.reason, "acl_reason": decision.acl_reason, "remaining": decision.remaining}})
            return

        secret = resolve_secret(decision.secret_ref or "")
        if not secret or os.getenv("MODELKEYGUARD_DRY_RUN", "1") == "1":
            self.server.guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)  # type: ignore[attr-defined]
            self._json(200, self._dry_run_response(model, principal_token.principal_id, key_id, event))
            return
        self._forward_openai(secret, raw)

    def _base_event(self, principal_token: Any, payload: dict[str, Any], key_id: str, decision: str, reason: str, system_hash: str | None) -> dict[str, Any]:
        request_id = hashlib.sha256(f"{time.time_ns()}:{principal_token.token_id}".encode()).hexdigest()[:24]
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "event_type": "MODEL_CALL_DECISION",
            "decision": decision,
            "reason": reason,
            "principal_id": principal_token.principal_id,
            "principal_kind": principal_token.kind,
            "groups": list(principal_token.groups),
            "namespace": principal_token.namespace,
            "on_behalf_of_user_id": principal_token.on_behalf_of_user_id,
            "token_id": principal_token.token_id,
            "model": payload.get("model"),
            "key_id": key_id,
            "system_prompt_hash": system_hash,
            "prompt_hash": sha256_text(json.dumps(payload.get("messages", payload), sort_keys=True)),
            "source_ip": self.client_address[0],
        }

    def _dry_run_response(self, model: str, principal_id: str, key_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "chatcmpl-modelkeyguard-dryrun",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"ModelKeyGuard allowed {principal_id} to use {model} via {key_id}."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "modelkeyguard": {"audit_token_id": event["token_id"], "decision": event["decision"], "remaining": event.get("remaining", {})},
        }

    def _forward_openai(self, secret: str, raw: bytes) -> None:
        url = os.getenv("OPENAI_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions")
        req = urllib.request.Request(url, data=raw, headers={"authorization": f"Bearer {secret}", "content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("content-type", resp.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("content-type", e.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8789, policy_path: str | Path = DEFAULT_POLICY) -> None:
    guard, policy = build_guard(policy_path)
    verifier = TokenVerifier(policy_path)
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.guard = guard  # type: ignore[attr-defined]
    server.policy = policy  # type: ignore[attr-defined]
    server.graph_state = guard.graph_state  # type: ignore[attr-defined]
    server.verifier = verifier  # type: ignore[attr-defined]
    print(f"ModelKeyGuard gateway listening on http://{host}:{port}")
    print(f"ACL backend: {guard.adapter_info.backend} — {guard.adapter_info.detail}")
    server.serve_forever()
