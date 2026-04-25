from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .core import ModelKey, ModelKeyGuard, Principal, Request as GuardRequest
from .graph_state import GraphStateStore
from .token_auth import TokenAuthError, TokenVerifier, TokenPrincipal

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


def build_base_event(
    principal_token: TokenPrincipal,
    payload: dict[str, Any],
    key_id: str,
    decision: str,
    reason: str,
    system_hash: str | None,
    source_ip: str = "unknown",
) -> dict[str, Any]:
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
        "source_ip": source_ip,
    }


def dry_run_response(model: str, principal_id: str, key_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-modelkeyguard-dryrun",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"ModelKeyGuard allowed {principal_id} to use {model} via {key_id}."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "modelkeyguard": {"audit_token_id": event["token_id"], "decision": event["decision"], "remaining": event.get("remaining", {})},
    }


def forward_openai(secret: str, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    url = os.getenv("OPENAI_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions")
    req = urllib.request.Request(url, data=raw, headers={"authorization": f"Bearer {secret}", "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, {"content-type": resp.headers.get("content-type", "application/json")}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {"content-type": e.headers.get("content-type", "application/json")}, e.read()


def process_chat_completion(
    payload: dict[str, Any],
    authorization_header: str | None,
    guard: ModelKeyGuard,
    policy: dict[str, Any],
    verifier: TokenVerifier,
    source_ip: str = "unknown",
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    try:
        principal_token = verifier.verify_authorization_header(authorization_header)
    except TokenAuthError as e:
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event("auth-failed", "AUTH_TOKEN_DENIED", {"reason": str(e)})
        return 401, {"error": {"message": str(e)}}, {"content-type": "application/json"}

    model = payload.get("model")
    key_id = select_key(policy, model)
    if not key_id:
        return 403, {"error": {"message": "model_not_registered"}}, {"content-type": "application/json"}

    messages = payload.get("messages", [])
    system_prompt = extract_system_prompt(messages) if isinstance(messages, list) else ""
    system_hash = sha256_text(system_prompt) if system_prompt else None
    profile = policy.get("usage_profiles", {}).get(principal_token.principal_id, {})
    expected_hashes = set(profile.get("system_prompt_hashes", []))
    if expected_hashes and system_hash not in expected_hashes:
        event = build_base_event(principal_token, payload, key_id, "BLOCKED", "system_prompt_signature_mismatch", system_hash, source_ip)
        append_audit(event)
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(event["request_id"], "ACL_DECISION_DENY", event)
        return 403, {"error": {"message": "system_prompt_signature_mismatch", "system_prompt_hash": system_hash}}, {"content-type": "application/json"}

    cost, estimated_tokens = estimate_cost_and_tokens(payload, policy)
    base_event = build_base_event(principal_token, payload, key_id, "PENDING", "request_received", system_hash, source_ip)
    decision = guard.check(GuardRequest(
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
        return decision.http_status, {"error": {"message": decision.reason, "acl_reason": decision.acl_reason, "remaining": decision.remaining}}, {"content-type": "application/json"}

    secret = resolve_secret(decision.secret_ref or "")
    if not secret or os.getenv("MODELKEYGUARD_DRY_RUN", "1") == "1":
        guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
        return 200, dry_run_response(model, principal_token.principal_id, key_id, event), {"content-type": "application/json"}

    status, headers, body = forward_openai(secret, raw_body or json.dumps(payload).encode("utf-8"))
    # A forwarded request has passed authorization. Record a best-effort usage estimate; production can
    # replace this with provider-returned exact usage once response parsing is added per endpoint.
    guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
    return status, body, headers


def create_app(policy_path: str | Path = DEFAULT_POLICY):
    """Create the production FastAPI application.

    FastAPI is imported lazily so pure policy/graph tests can still import this
    module before optional web dependencies are installed.
    """
    from fastapi import FastAPI, Header, Request as FastAPIRequest
    from fastapi.responses import JSONResponse, Response
    globals()["FastAPIRequest"] = FastAPIRequest

    guard, policy = build_guard(policy_path)
    verifier = TokenVerifier(policy_path)
    app = FastAPI(title="Kogwistar ModelKeyGuard", version="0.4.0")
    app.state.guard = guard
    app.state.policy = policy
    app.state.verifier = verifier

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "modelkeyguard-gateway", "server": "fastapi"}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        items = []
        for key in app.state.policy["model_keys"]:
            items.extend({"id": m, "object": "model", "owned_by": key["provider"]} for m in key["models"])
        return {"object": "list", "data": items}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: FastAPIRequest, authorization: str | None = Header(default=None)):
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "invalid_json"}})
        status, data, headers = process_chat_completion(
            payload=payload,
            authorization_header=authorization,
            guard=app.state.guard,
            policy=app.state.policy,
            verifier=app.state.verifier,
            source_ip=request.client.host if request.client else "unknown",
            raw_body=raw,
        )
        content_type = headers.get("content-type", "application/json")
        if isinstance(data, bytes):
            return Response(content=data, status_code=status, media_type=content_type)
        return JSONResponse(status_code=status, content=data)

    @app.post("/v1/responses")
    async def responses(request: FastAPIRequest, authorization: str | None = Header(default=None)):
        # Minimal OpenAI-compatible shim: process with the same guard path. A production version can add
        # response-endpoint-specific cost estimation and upstream routing.
        return await chat_completions(request, authorization)

    return app


def serve(host: str = "127.0.0.1", port: int = 8789, policy_path: str | Path = DEFAULT_POLICY) -> None:
    try:
        import uvicorn
    except Exception as e:  # pragma: no cover - exercised only when dependency missing.
        raise RuntimeError("FastAPI gateway requires uvicorn. Install with `pip install -e .`.") from e
    app = create_app(policy_path)
    guard = app.state.guard
    print(f"ModelKeyGuard FastAPI gateway listening on http://{host}:{port}")
    print(f"ACL backend: {guard.adapter_info.backend} — {guard.adapter_info.detail}")
    uvicorn.run(app, host=host, port=port)
