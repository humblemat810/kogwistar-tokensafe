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
from .key_manager import KeyLifecycleError, KeyManager, html_escape
from .settings import AppSettings, read_env_or_file
from .token_auth import TokenAuthError, TokenVerifier, TokenPrincipal

DEFAULT_POLICY = Path(os.getenv("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
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
    for item in policy.get("model_keys", []):
        secret_ref = item.get("secret_ref") or item.get("active_secret_ref")
        if not secret_ref and item.get("sealed_secret_payload"):
            secret_ref = f"secret:{item['id']}:policy"
        guard.register_key(ModelKey(id=item["id"], provider=item["provider"], models=tuple(item["models"]), secret_ref=secret_ref, display_name=item.get("display_name", item["id"]), approval_threshold_usd=float(item.get("approval_threshold_usd", 999999.0))))
        acl = item.get("acl", {})
        guard.grant(key_id=item["id"], mode=acl.get("mode", "scope"), created_by=acl.get("created_by", "human:platform-admin"), owner_id=acl.get("owner_id"), namespace=acl.get("namespace"), shared_with_principals=tuple(acl.get("shared_with_principals", [])), shared_with_groups=tuple(acl.get("shared_with_groups", [])))
    return guard, policy


def select_key(policy: dict[str, Any], model: str, guard: ModelKeyGuard | None = None) -> str | None:
    for key in policy.get("model_keys", []):
        if model in key.get("models", []):
            return key["id"]
    if guard and guard.graph_state:
        for node in guard.graph_state.nodes.values():
            if node.kind == "model_key" and node.payload.get("status", "active") == "active" and model in node.payload.get("models", []):
                return node.id
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
        safe = {k: v for k, v in event.items() if "secret" not in k.lower() and "provider_key" not in k.lower()}
        f.write(json.dumps(safe, sort_keys=True) + "\n")


def resolve_secret(secret_ref: str, guard: ModelKeyGuard | None = None) -> str | None:
    if not secret_ref:
        return None
    if secret_ref.startswith("env://"):
        return read_env_or_file(secret_ref.removeprefix("env://"))
    if secret_ref.startswith("secret:") and guard and guard.graph_state:
        return KeyManager(guard.graph_state, guard.graph_state.app_key).resolve_provider_secret(secret_ref)
    return None


def build_base_event(principal_token: TokenPrincipal, payload: dict[str, Any], key_id: str, decision: str, reason: str, system_hash: str | None, source_ip: str = "unknown") -> dict[str, Any]:
    request_id = hashlib.sha256(f"{time.time_ns()}:{principal_token.token_id}".encode()).hexdigest()[:24]
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request_id": request_id, "event_type": "MODEL_CALL_DECISION", "decision": decision, "reason": reason, "principal_id": principal_token.principal_id, "principal_kind": principal_token.kind, "groups": list(principal_token.groups), "namespace": principal_token.namespace, "on_behalf_of_user_id": principal_token.on_behalf_of_user_id, "token_id": principal_token.token_id, "model": payload.get("model"), "key_id": key_id, "system_prompt_hash": system_hash, "prompt_hash": sha256_text(json.dumps(payload.get("messages", payload), sort_keys=True)), "source_ip": source_ip}


def dry_run_response(model: str, principal_id: str, key_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"id": "chatcmpl-modelkeyguard-dryrun", "object": "chat.completion", "created": int(time.time()), "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": f"ModelKeyGuard allowed {principal_id} to use {model} via {key_id}."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "modelkeyguard": {"audit_token_id": event["token_id"], "decision": event["decision"], "remaining": event.get("remaining", {})}}


def forward_openai(secret: str, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    url = os.getenv("OPENAI_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions")
    req = urllib.request.Request(url, data=raw, headers={"authorization": f"Bearer {secret}", "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, {"content-type": resp.headers.get("content-type", "application/json")}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {"content-type": e.headers.get("content-type", "application/json")}, e.read()


def process_chat_completion(payload: dict[str, Any], authorization_header: str | None, guard: ModelKeyGuard, policy: dict[str, Any], verifier: TokenVerifier, source_ip: str = "unknown", raw_body: bytes | None = None) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    try:
        principal_token = verifier.verify_authorization_header(authorization_header)
    except TokenAuthError as e:
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event("auth-failed", "AUTH_TOKEN_DENIED", {"reason": str(e)})
        return 401, {"error": {"message": str(e)}}, {"content-type": "application/json"}
    model = payload.get("model")
    key_id = select_key(policy, model, guard)
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
    decision = guard.check(GuardRequest(principal=Principal(principal_token.principal_id, principal_token.kind, principal_token.groups), key_id=key_id, model=model, namespace=principal_token.namespace, estimated_cost_usd=cost, estimated_tokens=estimated_tokens, reason="gateway proxy request", request_id=base_event["request_id"], token_id=principal_token.token_id, on_behalf_of_user_id=principal_token.on_behalf_of_user_id))
    event = dict(base_event)
    event.update({"decision": "APPROVAL_REQUIRED" if decision.requires_approval else ("ALLOWED" if decision.allowed else "BLOCKED"), "reason": decision.reason, "estimated_cost_usd": cost, "estimated_tokens": estimated_tokens, "acl_reason": decision.acl_reason, "remaining": decision.remaining})
    append_audit(event)
    if not decision.allowed:
        return decision.http_status, {"error": {"message": decision.reason, "acl_reason": decision.acl_reason, "remaining": decision.remaining}}, {"content-type": "application/json"}
    try:
        secret = resolve_secret(decision.secret_ref or "", guard)
    except KeyLifecycleError as e:
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(decision.request_id, "SECRET_RESOLUTION_DENIED", {"reason": str(e), "key_id": decision.key_id})
        return 403, {"error": {"message": str(e)}}, {"content-type": "application/json"}
    if not secret or os.getenv("MODELKEYGUARD_DRY_RUN", "1") == "1":
        guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
        return 200, dry_run_response(model, principal_token.principal_id, key_id, event), {"content-type": "application/json"}
    status, headers, body = forward_openai(secret, raw_body or json.dumps(payload).encode("utf-8"))
    guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
    return status, body, headers


def _admin_html(views: list[Any]) -> str:
    rows = []
    for v in views:
        rows.append(f"<tr><td><code>{html_escape(v.key_id)}</code></td><td>{html_escape(v.provider)}</td><td>{html_escape(', '.join(v.models))}</td><td>{html_escape(v.status)}</td><td><code>{html_escape(v.active_secret_ref or '')}</code></td><td>{html_escape(v.expires_at_epoch or '')}</td><td><form method='post' action='/admin/keys/{html_escape(v.key_id)}/revoke'><input name='reason' placeholder='reason'><button>Revoke</button></form></td></tr>")
    body = "".join(rows) or "<tr><td colspan='7'>No managed keys</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>ModelKeyGuard Keys</title><style>body{{font-family:system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.5rem}}input{{margin:.2rem}}code{{background:#f5f5f5;padding:.1rem .25rem}}</style></head><body><h1>ModelKeyGuard Key Management</h1><p>Raw provider keys are accepted only through password fields and are never rendered back.</p><h2>Create sealed key</h2><form method='post' action='/admin/keys'><input name='key_id' placeholder='key:openai:prod' required><input name='provider' placeholder='openai' required><input name='models' placeholder='gpt-4o-mini,gpt-5.3-mini' required><input name='display_name' placeholder='OpenAI production'><input type='password' name='provider_secret' placeholder='provider key' autocomplete='off' required><input name='expires_at_epoch' placeholder='optional epoch expiry'><button>Create sealed key</button></form><h2>Keys</h2><table><thead><tr><th>Key</th><th>Provider</th><th>Models</th><th>Status</th><th>Secret ref</th><th>Secret expiry</th><th>Action</th></tr></thead><tbody>{body}</tbody></table><h2>Rotate key</h2><form method='post' action='/admin/keys/rotate'><input name='key_id' placeholder='key:openai:prod' required><input type='password' name='provider_secret' placeholder='new provider key' autocomplete='off' required><input name='expires_at_epoch' placeholder='optional epoch expiry'><button>Rotate</button></form></body></html>"""


def create_app(policy_path: str | Path = DEFAULT_POLICY):
    from fastapi import FastAPI, Header, Request as FastAPIRequest
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    # `from __future__ import annotations` stores this as a string; expose it in
    # module globals so FastAPI can resolve `request: FastAPIRequest` correctly.
    globals()["FastAPIRequest"] = FastAPIRequest

    settings = AppSettings.from_env()
    errors = settings.validate_for_startup()
    if errors:
        raise RuntimeError("; ".join(errors))
    guard, policy = build_guard(policy_path)
    verifier = TokenVerifier(policy_path)
    key_manager = KeyManager(guard.graph_state, guard.graph_state.app_key) if guard.graph_state else None
    app = FastAPI(title="Kogwistar ModelKeyGuard", version="0.5.0")
    app.state.guard = guard
    app.state.policy = policy
    app.state.verifier = verifier
    app.state.settings = settings
    app.state.key_manager = key_manager

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "modelkeyguard-gateway", "server": "fastapi", "env": settings.env}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        items = []
        seen = set()
        for key in app.state.policy.get("model_keys", []):
            for m in key.get("models", []):
                items.append({"id": m, "object": "model", "owned_by": key["provider"]}); seen.add(m)
        if app.state.key_manager:
            for v in app.state.key_manager.list_key_views():
                if v.status == "active":
                    for m in v.models:
                        if m not in seen:
                            items.append({"id": m, "object": "model", "owned_by": v.provider}); seen.add(m)
        return {"object": "list", "data": items}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: FastAPIRequest, authorization: str | None = Header(default=None)):
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "invalid_json"}})
        status, data, headers = process_chat_completion(payload, authorization, app.state.guard, app.state.policy, app.state.verifier, source_ip=request.client.host if request.client else "unknown", raw_body=raw)
        content_type = headers.get("content-type", "application/json")
        if isinstance(data, bytes):
            return Response(content=data, status_code=status, media_type=content_type)
        return JSONResponse(status_code=status, content=data)

    @app.get("/admin/keys")
    def admin_keys():
        return HTMLResponse(_admin_html(app.state.key_manager.list_key_views() if app.state.key_manager else []))

    @app.get("/admin/keys.json")
    def admin_keys_json():
        return {"data": [v.__dict__ for v in (app.state.key_manager.list_key_views() if app.state.key_manager else [])]}

    @app.post("/admin/keys")
    async def admin_create_key(request: FastAPIRequest):
        form = await request.form()
        try:
            view = app.state.key_manager.create_key(key_id=str(form.get("key_id", "")), provider=str(form.get("provider", "")), models=[m.strip() for m in str(form.get("models", "")).split(",") if m.strip()], display_name=str(form.get("display_name", "")), provider_secret=str(form.get("provider_secret", "")), created_by="admin:web", expires_at_epoch=int(form["expires_at_epoch"]) if form.get("expires_at_epoch") else None)
            app.state.guard.register_key(ModelKey(id=view.key_id, provider=view.provider, models=view.models, secret_ref=view.active_secret_ref, display_name=view.display_name))
            app.state.guard.grant(key_id=view.key_id, mode="scope", created_by="admin:web", owner_id="admin:web", namespace="tenant:kogwistar")
            return {"ok": True, "key_id": view.key_id, "secret_ref": view.active_secret_ref, "secret_value": None}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    async def _rotate(key_id: str, provider_secret: str, expires_at_epoch: Any):
        try:
            view = app.state.key_manager.rotate_key(key_id=key_id, provider_secret=provider_secret, rotated_by="admin:web", expires_at_epoch=int(expires_at_epoch) if expires_at_epoch else None)
            if key_id in app.state.guard.keys:
                old = app.state.guard.keys[key_id]
                app.state.guard.register_key(ModelKey(id=old.id, provider=old.provider, models=old.models, secret_ref=view.active_secret_ref, display_name=old.display_name))
            return {"ok": True, "key_id": view.key_id, "secret_ref": view.active_secret_ref, "secret_value": None}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    @app.post("/admin/keys/rotate")
    async def admin_rotate_key_form(request: FastAPIRequest):
        form = await request.form()
        return await _rotate(str(form.get("key_id", "")), str(form.get("provider_secret", "")), form.get("expires_at_epoch"))

    @app.post("/admin/keys/{key_id:path}/rotate")
    async def admin_rotate_key(key_id: str, request: FastAPIRequest):
        form = await request.form()
        return await _rotate(key_id, str(form.get("provider_secret", "")), form.get("expires_at_epoch"))

    @app.post("/admin/keys/{key_id:path}/revoke")
    async def admin_revoke_key(key_id: str, request: FastAPIRequest):
        form = await request.form()
        try:
            view = app.state.key_manager.revoke_key(key_id=key_id, revoked_by="admin:web", reason=str(form.get("reason", "")))
            app.state.guard.keys.pop(key_id, None)
            return {"ok": True, "key_id": view.key_id, "status": view.status}
        except KeyLifecycleError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    @app.post("/v1/responses")
    async def responses(request: FastAPIRequest, authorization: str | None = Header(default=None)):
        return await chat_completions(request, authorization)

    return app


def serve(host: str | None = None, port: int | None = None, policy_path: str | Path = DEFAULT_POLICY) -> None:
    try:
        import uvicorn
    except Exception as e:
        raise RuntimeError("FastAPI gateway requires uvicorn. Install with `pip install -e .`.") from e
    settings = AppSettings.from_env()
    host = host or settings.host
    port = port or settings.port
    app = create_app(policy_path)
    print(f"ModelKeyGuard FastAPI gateway listening on http://{host}:{port}")
    print(f"ACL backend: {app.state.guard.adapter_info.backend} — {app.state.guard.adapter_info.detail}")
    uvicorn.run(app, host=host, port=port)
