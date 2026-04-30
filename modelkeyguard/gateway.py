from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .core import ModelKey, ModelKeyGuard, Principal, Request as GuardRequest
from .graph_state import GraphStateStore, resolve_store_backend
from .key_manager import KeyLifecycleError, KeyManager
from .providers import AzureOpenAIAdapter, GeminiAdapter, OllamaAdapter, OpenAIAdapter, ProviderAdapter, default_upstream_url
from .services import derive_prompt_heuristics, get_static_dir, render_admin_keys_page, render_admin_policy_page
from .services.admin_auth import ADMIN_COOKIE_NAME, ADMIN_HEADER_NAME, admin_html_login_response, decode_admin_session, verify_admin_session
from .services.history_ops import capture_history_record
from .settings import AppSettings, read_env_or_file
from .token_auth import TokenAuthError, TokenVerifier, TokenPrincipal
from .policy_loader import load_policy_json

DEFAULT_POLICY = Path(os.getenv("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
AUDIT_PATH = Path(os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_system_prompt(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")


def build_guard(policy_path: str | Path = DEFAULT_POLICY, *, app_key: str | None = None) -> tuple[ModelKeyGuard, dict[str, Any]]:
    policy = load_policy_json(policy_path)
    try:
        graph_state = GraphStateStore.from_policy(policy, app_key=app_key)
    except ValueError as exc:
        if "sealed graph payload authentication failed" not in str(exc):
            raise
        raise RuntimeError(
            "Graph state could not be decrypted with the configured MODELKEYGUARD_GRAPH_KEY. "
            "This usually means the Postgres/jsonl state was sealed with a different graph key. "
            "For production, restore the original graph key from your secret manager. "
            "For local rehearsal only, reset the local state with ./scripts/reset_local_e2e_state.sh "
            "or run ./scripts/production_compose.sh fresh-up."
        ) from exc
    guard = ModelKeyGuard.create()
    if resolve_store_backend() == "kogwistar_postgres" and guard.adapter_info.backend != "kogwistar-package":
        raise RuntimeError(
            "kogwistar_postgres requires installed Kogwistar ACLGraph; "
            "set MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1 and ensure kogwistar package extras are installed."
        )
    guard.graph_state = graph_state
    policy_acl_by_key: dict[str, dict[str, Any]] = {}
    for item in policy.get("model_keys", []):
        policy_acl_by_key[str(item["id"])] = dict(item.get("acl", {}))
        secret_ref = item.get("secret_ref") or item.get("active_secret_ref")
        if not secret_ref and item.get("sealed_secret_payload"):
            secret_ref = f"secret:{item['id']}:policy"
        guard.register_key(
            ModelKey(
                id=item["id"],
                provider=item["provider"],
                models=tuple(item["models"]),
                secret_ref=secret_ref,
                display_name=item.get("display_name", item["id"]),
                upstream_url=str(item.get("upstream_url", "")),
                approval_threshold_usd=float(item.get("approval_threshold_usd", 999999.0)),
                intended_use=str(item.get("intended_use", "")),
            )
        )
        acl = item.get("acl", {})
        guard.grant(
            key_id=item["id"],
            mode=acl.get("mode", "scope"),
            created_by=acl.get("created_by", "human:platform-admin"),
            owner_id=acl.get("owner_id"),
            namespace=acl.get("namespace"),
            shared_with_principals=tuple(acl.get("shared_with_principals", [])),
            shared_with_groups=tuple(acl.get("shared_with_groups", [])),
        )

    # Rehydrate runtime-managed keys from graph state so admin-created/rotated keys
    # remain usable after gateway restarts. Graph is the latest mutable source.
    for node in graph_state.nodes.values():
        if node.kind != "model_key":
            continue
        if str(node.payload.get("status", "active")) != "active":
            continue
        existing = guard.keys.get(node.id)
        provider = str(node.payload.get("provider") or (existing.provider if existing else "openai"))
        models = tuple(str(m) for m in (node.payload.get("models") or (existing.models if existing else ())) if str(m))
        if not models:
            continue
        secret_ref = node.payload.get("active_secret_ref") or node.payload.get("secret_ref")
        if not secret_ref and node.payload.get("sealed_secret_payload"):
            secret_ref = f"secret:{node.id}:policy"
        guard.register_key(
            ModelKey(
                id=node.id,
                provider=provider,
                models=models,
                secret_ref=str(secret_ref) if secret_ref else None,
                display_name=str(node.payload.get("display_name") or (existing.display_name if existing else node.id)),
                upstream_url=str(node.payload.get("upstream_url") or (existing.upstream_url if existing else "")),
                approval_threshold_usd=float(node.payload.get("approval_threshold_usd", existing.approval_threshold_usd if existing else 999999.0)),
                intended_use=str(node.payload.get("intended_use") or (existing.intended_use if existing else "")),
            )
        )
        if node.id in policy_acl_by_key:
            continue
        acl_mode = str(node.payload.get("acl_mode", "") or "")
        if acl_mode:
            guard.grant(
                key_id=node.id,
                mode=acl_mode,
                created_by="graph:rehydrate",
                owner_id=None,
                namespace=str(node.payload.get("namespace", "tenant:kogwistar")),
                shared_with_principals=tuple(str(x) for x in node.payload.get("shared_with_principals", [])),
                shared_with_groups=tuple(str(x) for x in node.payload.get("shared_with_groups", [])),
            )
        else:
            scope_edges = [e for e in graph_state.edges_from(node.id, "AVAILABLE_IN") if str(e.target).startswith("tenant:")]
            if scope_edges:
                for edge in scope_edges:
                    guard.grant(
                        key_id=node.id,
                        mode=str(edge.payload.get("acl_mode", "scope")),
                        created_by="graph:rehydrate",
                        owner_id=None,
                        namespace=str(edge.target),
                        shared_with_principals=tuple(str(x) for x in edge.payload.get("shared_with_principals", [])),
                        shared_with_groups=tuple(str(x) for x in edge.payload.get("shared_with_groups", [])),
                    )
            else:
                guard.grant(
                    key_id=node.id,
                    mode="scope",
                    created_by="graph:rehydrate",
                    owner_id=None,
                    namespace="tenant:kogwistar",
                )
    return guard, policy


def _iter_model_keys(policy: dict[str, Any], guard: ModelKeyGuard | None = None) -> list[tuple[str, str, tuple[str, ...]]]:
    out: list[tuple[str, str, tuple[str, ...]]] = []
    seen: set[str] = set()

    for key in policy.get("model_keys", []):
        key_id = str(key.get("id", ""))
        if not key_id:
            continue
        out.append((key_id, str(key.get("provider", "openai")), tuple(str(m) for m in key.get("models", []))))
        seen.add(key_id)

    if guard and guard.graph_state:
        for node in guard.graph_state.nodes.values():
            if node.kind != "model_key":
                continue
            if node.payload.get("status", "active") != "active":
                continue
            if node.id in seen:
                continue
            out.append(
                (
                    node.id,
                    str(node.payload.get("provider", "openai")),
                    tuple(str(m) for m in node.payload.get("models", [])),
                )
            )
            seen.add(node.id)

    return out


def select_key(policy: dict[str, Any], model: str, guard: ModelKeyGuard | None = None) -> str | None:
    for key_id, _provider, models in _iter_model_keys(policy, guard):
        if model in models:
            return key_id
    return None


def select_key_with_error(policy: dict[str, Any], model: str, guard: ModelKeyGuard | None = None) -> tuple[str | None, str | None]:
    matches = [key_id for key_id, _provider, models in _iter_model_keys(policy, guard) if model in models]
    if not matches:
        return None, "model_not_registered"
    if len(matches) > 1:
        return None, "model_key_ambiguous"
    return matches[0], None


def _requested_modelkeyguard_key_id(payload: dict[str, Any]) -> str:
    direct = str(payload.get("modelkeyguard_key_id") or "").strip()
    if direct:
        return direct
    meta = payload.get("modelkeyguard")
    if isinstance(meta, dict):
        return str(meta.get("key_id") or "").strip()
    return ""


def _strip_modelkeyguard_control_fields(payload: dict[str, Any]) -> bytes:
    clean = dict(payload)
    clean.pop("modelkeyguard_key_id", None)
    meta = clean.get("modelkeyguard")
    if isinstance(meta, dict) and "key_id" in meta:
        remaining = dict(meta)
        remaining.pop("key_id", None)
        if remaining:
            clean["modelkeyguard"] = remaining
        else:
            clean.pop("modelkeyguard", None)
    return json.dumps(clean).encode("utf-8")


def select_requested_key(
    policy: dict[str, Any],
    model: str,
    provider: str,
    requested_key_id: str,
    *,
    enforce_provider: bool,
    guard: ModelKeyGuard | None = None,
) -> tuple[str | None, str | None]:
    for key_id, key_provider, models in _iter_model_keys(policy, guard):
        if key_id != requested_key_id:
            continue
        if model not in models:
            return None, "model_not_allowed_for_key"
        if enforce_provider and key_provider != provider:
            return None, "model_key_provider_mismatch"
        return key_id, None
    return None, "model_key_not_registered"


def select_key_for_provider(
    policy: dict[str, Any],
    model: str,
    provider: str,
    guard: ModelKeyGuard | None = None,
) -> tuple[str | None, str | None]:
    found_model_mismatch_provider = False
    matches: list[str] = []
    for key_id, key_provider, models in _iter_model_keys(policy, guard):
        if model not in models:
            continue
        if key_provider == provider:
            matches.append(key_id)
            continue
        found_model_mismatch_provider = True
    if len(matches) > 1:
        return None, "model_key_ambiguous"
    if matches:
        return matches[0], None
    if found_model_mismatch_provider:
        return None, "model_key_provider_mismatch"
    return None, "model_not_registered"


def rehydrate_runtime_key(guard: ModelKeyGuard, key_id: str) -> bool:
    if key_id in guard.keys:
        return True
    graph_state = guard.graph_state
    if not graph_state:
        return False
    node = graph_state.nodes.get(key_id)
    if not node or node.kind != "model_key":
        return False
    if str(node.payload.get("status", "active")) != "active":
        return False

    models = tuple(str(m) for m in (node.payload.get("models") or []) if str(m))
    if not models:
        return False

    secret_ref = node.payload.get("active_secret_ref") or node.payload.get("secret_ref")
    if not secret_ref and node.payload.get("sealed_secret_payload"):
        secret_ref = f"secret:{key_id}:policy"
    guard.register_key(
        ModelKey(
            id=key_id,
            provider=str(node.payload.get("provider", "openai")),
            models=models,
            secret_ref=str(secret_ref) if secret_ref else None,
            display_name=str(node.payload.get("display_name", key_id)),
            upstream_url=str(node.payload.get("upstream_url", "")),
            approval_threshold_usd=float(node.payload.get("approval_threshold_usd", 999999.0)),
            intended_use=str(node.payload.get("intended_use", "")),
        )
    )

    acl_mode = str(node.payload.get("acl_mode", "") or "")
    if acl_mode:
        guard.grant(
            key_id=key_id,
            mode=acl_mode,
            created_by="graph:runtime_lookup",
            owner_id=None,
            namespace=str(node.payload.get("namespace", "tenant:kogwistar")),
            shared_with_principals=tuple(str(x) for x in node.payload.get("shared_with_principals", [])),
            shared_with_groups=tuple(str(x) for x in node.payload.get("shared_with_groups", [])),
        )
    else:
        scope_edges = [e for e in graph_state.edges_from(key_id, "AVAILABLE_IN") if str(e.target).startswith("tenant:")]
        if scope_edges:
            for edge in scope_edges:
                guard.grant(
                    key_id=key_id,
                    mode=str(edge.payload.get("acl_mode", "scope")),
                    created_by="graph:runtime_lookup",
                    owner_id=None,
                    namespace=str(edge.target),
                    shared_with_principals=tuple(str(x) for x in edge.payload.get("shared_with_principals", [])),
                    shared_with_groups=tuple(str(x) for x in edge.payload.get("shared_with_groups", [])),
                )
        else:
            guard.grant(
                key_id=key_id,
                mode="scope",
                created_by="graph:runtime_lookup",
                owner_id=None,
                namespace="tenant:kogwistar",
            )
    return True


def _key_upstream_override(policy: dict[str, Any], guard: ModelKeyGuard, key_id: str) -> str | None:
    key = guard.keys.get(key_id)
    if key and key.upstream_url.strip():
        return key.upstream_url.strip()
    graph_state = guard.graph_state
    if graph_state:
        node = graph_state.nodes.get(key_id)
        if node and node.kind == "model_key":
            upstream = str(node.payload.get("upstream_url", "")).strip()
            if upstream:
                return upstream
    for item in policy.get("model_keys", []):
        if str(item.get("id")) == key_id:
            upstream = str(item.get("upstream_url", "")).strip()
            if upstream:
                return upstream
    return None


def _merge_upstream_base(upstream_url: str | None, custom_base: str | None) -> str | None:
    if not custom_base:
        return upstream_url
    base = custom_base.strip().rstrip("/")
    if not base:
        return upstream_url
    if not upstream_url:
        return base

    route_url = urllib.parse.urlsplit(upstream_url)
    base_url = urllib.parse.urlsplit(base)
    scheme = base_url.scheme or route_url.scheme
    netloc = base_url.netloc or route_url.netloc
    base_path = base_url.path.rstrip("/")
    full_endpoint_suffixes = (
        "/api/chat",
        "/api/generate",
        "/chat/completions",
        "/responses",
        ":generateContent",
        ":streamGenerateContent",
    )
    if any(base_path.endswith(suffix) for suffix in full_endpoint_suffixes):
        return urllib.parse.urlunsplit((scheme, netloc, base_path, base_url.query, ""))
    route_path = route_url.path or ""
    if base_path and (route_path == base_path or route_path.startswith(f"{base_path}/")):
        merged_path = route_path
    else:
        merged_path = f"{base_path}{route_path}" if base_path else route_path
    return urllib.parse.urlunsplit((scheme, netloc, merged_path, route_url.query, ""))


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
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"ModelKeyGuard allowed {principal_id} to use {model} via {key_id}.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "modelkeyguard": {
            "audit_token_id": event["token_id"],
            "decision": event["decision"],
            "remaining": event.get("remaining", {}),
        },
    }


def _principal_has_admin_role(principal: TokenPrincipal, required_role: str) -> bool:
    allowed = set(principal.scopes) | set(principal.groups)
    return required_role in allowed


def forward_provider(
    secret: str,
    raw: bytes,
    *,
    provider: str,
    url: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, str], bytes]:
    target_url = url or default_upstream_url(provider)
    timeout_seconds = float(os.getenv("MODELKEYGUARD_PROVIDER_TIMEOUT_SECONDS", "180"))
    headers = {"content-type": content_type}
    if provider == "azure_openai":
        headers["api-key"] = secret
    elif provider == "gemini":
        headers["x-goog-api-key"] = secret
    else:
        headers["authorization"] = f"Bearer {secret}"

    capture_path = os.getenv("MODELKEYGUARD_MOCK_UPSTREAM_CAPTURE_PATH")
    if capture_path:
        _capture_forward_record(capture_path, provider, target_url, headers, raw)
        return 200, {"content-type": "application/json"}, _mock_upstream_response(provider, raw)

    cached = _forward_provider_from_joblib_cache(secret, raw, provider=provider, target_url=target_url, content_type=content_type)
    if cached is not None:
        status, cached_headers, body = cached
        cached_headers = dict(cached_headers)
        cached_headers["x-modelkeyguard-llm-cache"] = "hit"
        return status, cached_headers, body

    req = urllib.request.Request(target_url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            result = resp.status, {"content-type": resp.headers.get("content-type", "application/json")}, resp.read()
    except urllib.error.HTTPError as e:
        result = e.code, {"content-type": e.headers.get("content-type", "application/json")}, e.read()
    except urllib.error.URLError as e:
        result = 502, {"content-type": "application/json"}, json.dumps(
            {"error": {"message": "provider_upstream_unreachable", "upstream_url": target_url, "detail": str(e.reason)}}
        ).encode("utf-8")

    _store_provider_joblib_cache(secret, raw, result, provider=provider, target_url=target_url, content_type=content_type)
    return result


def _llm_joblib_cache_enabled() -> bool:
    return os.getenv("MODELKEYGUARD_LLM_CALL_CACHE", "0").strip().lower() in {"1", "true", "yes", "joblib"}


def _llm_joblib_cache_path(secret: str, raw: bytes, *, provider: str, target_url: str, content_type: str) -> Path:
    cache_dir = Path(os.getenv("MODELKEYGUARD_LLM_CALL_CACHE_DIR", "out/llm_call_cache"))
    key_payload = {
        "provider": provider,
        "target_url": target_url,
        "content_type": content_type,
        "secret_sha256": sha256_text(secret),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "schema": 1,
    }
    key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.joblib"


def _joblib_module():
    try:
        import joblib  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime extra
        raise RuntimeError("MODELKEYGUARD_LLM_CALL_CACHE=joblib requires `pip install joblib`") from exc
    return joblib


def _forward_provider_from_joblib_cache(
    secret: str,
    raw: bytes,
    *,
    provider: str,
    target_url: str,
    content_type: str,
) -> tuple[int, dict[str, str], bytes] | None:
    if not _llm_joblib_cache_enabled():
        return None
    path = _llm_joblib_cache_path(secret, raw, provider=provider, target_url=target_url, content_type=content_type)
    if not path.exists():
        return None
    cached = _joblib_module().load(path)
    if not isinstance(cached, tuple) or len(cached) != 3:
        return None
    status, headers, body = cached
    if not isinstance(status, int) or not isinstance(headers, dict) or not isinstance(body, bytes):
        return None
    return status, {str(k): str(v) for k, v in headers.items()}, body


def _store_provider_joblib_cache(
    secret: str,
    raw: bytes,
    result: tuple[int, dict[str, str], bytes],
    *,
    provider: str,
    target_url: str,
    content_type: str,
) -> None:
    if not _llm_joblib_cache_enabled():
        return
    path = _llm_joblib_cache_path(secret, raw, provider=provider, target_url=target_url, content_type=content_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _joblib_module().dump(result, path)


def _capture_forward_record(path: str, provider: str, url: str, headers: dict[str, str], raw: bytes) -> None:
    body_text: str
    try:
        body_text = raw.decode("utf-8")
    except Exception:
        body_text = ""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": provider,
        "url": url,
        "headers": headers,
        "body": body_text,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _mock_upstream_response(provider: str, raw: bytes) -> bytes:
    model = "mock-model"
    try:
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            model = str(payload.get("model") or model)
    except Exception:
        payload = {}

    if provider == "gemini":
        return json.dumps(
            {
                "candidates": [{"content": {"role": "model", "parts": [{"text": "mock-gemini-response"}]}, "finishReason": "STOP", "index": 0}],
                "modelVersion": model,
            }
        ).encode("utf-8")

    if provider == "ollama":
        return json.dumps(
            {
                "model": model,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": {"role": "assistant", "content": "mock-ollama-response"},
                "done": True,
            }
        ).encode("utf-8")

    return json.dumps(
        {
            "id": "chatcmpl-modelkeyguard-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock-openai-response"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    ).encode("utf-8")

def process_chat_completion(
    payload: dict[str, Any],
    authorization_header: str | None,
    guard: ModelKeyGuard,
    policy: dict[str, Any],
    verifier: TokenVerifier,
    source_ip: str = "unknown",
    raw_body: bytes | None = None,
    *,
    provider: str = "openai",
    enforce_provider: bool = False,
    model_override: str | None = None,
    upstream_url: str | None = None,
    forward_body: bytes | None = None,
    forward_content_type: str = "application/json",
    history_meta: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    meta = history_meta if history_meta is not None else {}

    def _set_meta(**kwargs: Any) -> None:
        for key, value in kwargs.items():
            if value is not None:
                meta[key] = value

    try:
        principal_token = verifier.verify_authorization_header(authorization_header)
    except TokenAuthError as e:
        request_id = f"auth-{time.time_ns()}"
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(request_id, "AUTH_TOKEN_DENIED", {"reason": str(e)})
        _set_meta(
            request_id=request_id,
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            decision="BLOCKED",
            reason=str(e),
            http_status=401,
            provider=provider,
        )
        return 401, {"error": {"message": str(e)}}, {"content-type": "application/json"}

    _set_meta(
        principal_id=principal_token.principal_id,
        on_behalf_of_user_id=principal_token.on_behalf_of_user_id,
        token_id=principal_token.token_id,
        provider=provider,
    )

    model = model_override or payload.get("model")
    if not model:
        _set_meta(
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            decision="BLOCKED",
            reason="model_not_registered",
            http_status=403,
        )
        return 403, {"error": {"message": "model_not_registered"}}, {"content-type": "application/json"}

    requested_key_id = _requested_modelkeyguard_key_id(payload)
    if requested_key_id:
        key_id, key_error = select_requested_key(
            policy,
            str(model),
            provider,
            requested_key_id,
            enforce_provider=enforce_provider,
            guard=guard,
        )
        if not key_id:
            _set_meta(
                model=str(model),
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                decision="BLOCKED",
                reason=key_error or "model_key_not_registered",
                http_status=403,
            )
            return 403, {"error": {"message": key_error or "model_key_not_registered"}}, {"content-type": "application/json"}
    elif enforce_provider:
        key_id, key_error = select_key_for_provider(policy, str(model), provider, guard)
        if not key_id:
            _set_meta(
                model=str(model),
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                decision="BLOCKED",
                reason=key_error or "model_not_registered",
                http_status=403,
            )
            return 403, {"error": {"message": key_error or "model_not_registered"}}, {"content-type": "application/json"}
    else:
        key_id, key_error = select_key_with_error(policy, str(model), guard)
        if not key_id:
            _set_meta(
                model=str(model),
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                decision="BLOCKED",
                reason=key_error or "model_not_registered",
                http_status=403,
            )
            return 403, {"error": {"message": key_error or "model_not_registered"}}, {"content-type": "application/json"}

    # Runtime fallback: if key is not currently in-memory, try persistent graph
    # rehydrate at request time before guard.check. This keeps restart/eviction
    # behavior robust while preserving graph as source of truth.
    rehydrate_runtime_key(guard, key_id)

    messages = payload.get("messages", [])
    system_prompt = extract_system_prompt(messages) if isinstance(messages, list) else ""
    system_hash = sha256_text(system_prompt) if system_prompt else None
    profile = policy.get("usage_profiles", {}).get(principal_token.principal_id, {})
    profile_data = profile if isinstance(profile, dict) else {}
    prompt_heuristics = derive_prompt_heuristics(payload, profile_data)
    application_id = principal_token.principal_id if principal_token.principal_id.startswith(("app:", "service:")) else None
    expected_hashes = set(profile_data.get("system_prompt_hashes", []))
    if expected_hashes and system_hash not in expected_hashes:
        event = build_base_event(principal_token, payload, key_id, "BLOCKED", "system_prompt_signature_mismatch", system_hash, source_ip)
        event["provider"] = provider
        event["application_id"] = application_id
        event["prompt_heuristics"] = prompt_heuristics
        append_audit(event)
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(event["request_id"], "ACL_DECISION_DENY", event)
        _set_meta(
            request_id=event["request_id"],
            ts=event["ts"],
            principal_id=event["principal_id"],
            on_behalf_of_user_id=event.get("on_behalf_of_user_id"),
            token_id=event.get("token_id"),
            key_id=event.get("key_id"),
            model=event.get("model"),
            decision=event.get("decision"),
            reason=event.get("reason"),
            http_status=403,
        )
        return 403, {"error": {"message": "system_prompt_signature_mismatch", "system_prompt_hash": system_hash}}, {"content-type": "application/json"}

    cost, estimated_tokens = estimate_cost_and_tokens(payload, policy)
    base_event = build_base_event(principal_token, payload, key_id, "PENDING", "request_received", system_hash, source_ip)
    decision = guard.check(
        GuardRequest(
            principal=Principal(principal_token.principal_id, principal_token.kind, principal_token.groups),
            key_id=key_id,
            model=str(model),
            namespace=principal_token.namespace,
            estimated_cost_usd=cost,
            estimated_tokens=estimated_tokens,
            reason="gateway proxy request",
            request_id=base_event["request_id"],
            token_id=principal_token.token_id,
            on_behalf_of_user_id=principal_token.on_behalf_of_user_id,
        )
    )
    event = dict(base_event)
    event.update(
        {
            "decision": "APPROVAL_REQUIRED" if decision.requires_approval else ("ALLOWED" if decision.allowed else "BLOCKED"),
            "reason": decision.reason,
            "estimated_cost_usd": cost,
            "estimated_tokens": estimated_tokens,
            "acl_reason": decision.acl_reason,
            "remaining": decision.remaining,
            "provider": provider,
            "application_id": application_id,
            "prompt_heuristics": prompt_heuristics,
        }
    )
    _set_meta(
        request_id=event["request_id"],
        ts=event["ts"],
        principal_id=event["principal_id"],
        on_behalf_of_user_id=event.get("on_behalf_of_user_id"),
        token_id=event.get("token_id"),
        key_id=event.get("key_id"),
        model=event.get("model"),
        decision=event.get("decision"),
        reason=event.get("reason"),
    )
    append_audit(event)
    if not decision.allowed:
        _set_meta(http_status=decision.http_status)
        return decision.http_status, {"error": {"message": decision.reason, "acl_reason": decision.acl_reason, "remaining": decision.remaining}}, {"content-type": "application/json"}

    try:
        secret = resolve_secret(decision.secret_ref or "", guard)
    except KeyLifecycleError as e:
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(decision.request_id, "SECRET_RESOLUTION_DENIED", {"reason": str(e), "key_id": decision.key_id})
        _set_meta(
            request_id=decision.request_id,
            key_id=decision.key_id,
            decision="BLOCKED",
            reason=str(e),
            http_status=403,
        )
        return 403, {"error": {"message": str(e)}}, {"content-type": "application/json"}

    dry_run_mode = os.getenv("MODELKEYGUARD_DRY_RUN", "1") == "1"
    if not secret:
        if dry_run_mode:
            guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
            _set_meta(http_status=200)
            return 200, dry_run_response(str(model), principal_token.principal_id, key_id, event), {"content-type": "application/json"}
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(
                decision.request_id,
                "SECRET_RESOLUTION_DENIED",
                {"reason": "provider_secret_missing", "key_id": decision.key_id},
            )
        _set_meta(
            http_status=403,
            decision="BLOCKED",
            reason="provider_secret_missing",
        )
        return 403, {"error": {"message": "provider_secret_missing", "key_id": decision.key_id}}, {"content-type": "application/json"}

    if dry_run_mode:
        guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
        _set_meta(http_status=200)
        return 200, dry_run_response(str(model), principal_token.principal_id, key_id, event), {"content-type": "application/json"}

    resolved_upstream_url = _merge_upstream_base(upstream_url, _key_upstream_override(policy, guard, key_id))
    provider_body = forward_body or raw_body or json.dumps(payload).encode("utf-8")
    if requested_key_id:
        provider_body = _strip_modelkeyguard_control_fields(payload)

    status, headers, body = forward_provider(
        secret,
        provider_body,
        provider=provider,
        url=resolved_upstream_url,
        content_type=forward_content_type,
    )
    guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens)
    _set_meta(http_status=status, decision="ALLOWED")
    return status, body, headers


def create_app(policy_path: str | Path = DEFAULT_POLICY):
    from fastapi import FastAPI, Request as FastAPIRequest
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    from .routers import (
        create_admin_history_router,
        create_admin_keys_router,
        create_admin_policy_router,
        create_admin_security_router,
        create_admin_oidc_router,
        create_admin_session_router,
        create_admin_usage_router,
        create_provider_azure_router,
        create_provider_gemini_router,
        create_provider_ollama_router,
        create_provider_openai_router,
    )

    # `from __future__ import annotations` stores this as a string; expose it in
    # module globals so FastAPI can resolve `request: FastAPIRequest` correctly.
    globals()["FastAPIRequest"] = FastAPIRequest

    settings = AppSettings.from_env()
    errors = settings.validate_for_startup()
    if errors:
        raise RuntimeError("; ".join(errors))
    guard, policy = build_guard(policy_path, app_key=settings.graph_key)
    verifier = TokenVerifier(policy_path)
    verifier.graph_state = guard.graph_state
    key_manager = KeyManager(guard.graph_state, guard.graph_state.app_key) if guard.graph_state else None
    app = FastAPI(title="Kogwistar ModelKeyGuard", version="0.6.0")
    app.state.guard = guard
    app.state.policy = policy
    app.state.verifier = verifier
    app.state.settings = settings
    app.state.key_manager = key_manager
    app.mount("/static", StaticFiles(directory=str(get_static_dir())), name="static")

    @app.middleware("http")
    async def admin_route_auth_middleware(request: FastAPIRequest, call_next):
        path = request.url.path
        if not path.startswith("/admin/"):
            return await call_next(request)
        if path in {"/admin/session", "/admin/oidc/login", "/admin/oidc/callback"}:
            return await call_next(request)

        # Host security watcher keeps using its shared secret while all admin
        # pages/APIs are additionally protected by admin session/header auth.
        if path == "/admin/security-events":
            required = os.getenv("SECURITY_EVENT_SHARED_SECRET", "").strip()
            provided = request.headers.get("x-modelkeyguard-security-secret", "")
            if required and provided == required:
                return await call_next(request)

        is_usage_route = path in {"/admin/usage", "/admin/usage.json"}

        if settings.admin_auth_mode in {"keycloak", "secret_or_keycloak"}:
            try:
                admin_principal = app.state.verifier.verify_keycloak_authorization_header(request.headers.get("authorization"))
            except TokenAuthError:
                admin_principal = None
            if admin_principal is not None:
                if _principal_has_admin_role(admin_principal, settings.admin_required_role):
                    request.state.admin_principal = admin_principal
                    return await call_next(request)
                if is_usage_route and _principal_has_admin_role(admin_principal, settings.usage_required_role):
                    request.state.admin_principal = admin_principal
                    return await call_next(request)
                return JSONResponse(status_code=403, content={"error": {"message": "admin_role_required"}})

        admin_cookie = request.cookies.get(ADMIN_COOKIE_NAME)
        admin_session = decode_admin_session(settings.admin_api_secret, admin_cookie)
        if settings.admin_auth_mode in {"secret", "secret_or_keycloak"}:
            header_val = request.headers.get(ADMIN_HEADER_NAME, "")
            if header_val and header_val == settings.admin_api_secret:
                return await call_next(request)
            if admin_session and str(admin_session.get("source") or "secret") in {"secret", "oidc"}:
                return await call_next(request)

        if settings.admin_auth_mode == "keycloak":
            if admin_session and str(admin_session.get("source") or "secret") == "oidc":
                return await call_next(request)

        if is_usage_route and settings.admin_auth_mode in {"secret", "secret_or_keycloak"}:
            if admin_session and str(admin_session.get("source") or "secret") in {"secret", "oidc"}:
                return await call_next(request)

        wants_html = (
            request.method.upper() == "GET"
            and not path.endswith(".json")
            and "text/html" in (request.headers.get("accept") or "").lower()
        )
        if wants_html and settings.admin_auth_mode != "keycloak":
            return admin_html_login_response(path)
        return JSONResponse(status_code=401, content={"error": {"message": "admin_auth_required"}})

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "modelkeyguard-gateway", "server": "fastapi", "env": settings.env}

    @app.get("/v1/models")
    def models(request: FastAPIRequest):
        if settings.require_model_list_auth:
            try:
                app.state.verifier.verify_authorization_header(request.headers.get("authorization"))
            except TokenAuthError as exc:
                return JSONResponse(status_code=401, content={"error": {"message": str(exc)}})
        items = []
        seen = set()
        for key in app.state.policy.get("model_keys", []):
            for m in key.get("models", []):
                items.append({"id": m, "object": "model", "owned_by": key["provider"]})
                seen.add(m)
        if app.state.key_manager:
            for v in app.state.key_manager.list_key_views():
                if v.status == "active":
                    for m in v.models:
                        if m not in seen:
                            items.append({"id": m, "object": "model", "owned_by": v.provider})
                            seen.add(m)
        return {"object": "list", "data": items}

    provider_adapters: dict[str, ProviderAdapter] = {
        "openai": OpenAIAdapter(),
        "azure_openai": AzureOpenAIAdapter(),
        "ollama": OllamaAdapter(),
    }

    def _resolve_adapter(provider: str, stream: bool = False) -> ProviderAdapter | None:
        if provider == "gemini":
            return GeminiAdapter(stream=stream)
        return provider_adapters.get(provider)

    async def _load_json_body(request: FastAPIRequest) -> tuple[dict[str, Any] | None, bytes]:
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return None, raw
        return payload, raw

    async def _handle_adapter_route(
        request: FastAPIRequest,
        *,
        provider: str,
        authorization: str | None = None,
        route_model: str | None = None,
        deployment: str | None = None,
        x_goog_api_key: str | None = None,
        stream: bool = False,
        operation: str | None = None,
    ):
        route_family = {
            "openai": "openai_v1",
            "azure_openai": "azure_native",
            "ollama": "ollama_native",
            "gemini": "gemini_native",
        }.get(provider, provider)

        def _capture_history(
            *,
            metadata: dict[str, Any],
            response_raw: bytes,
            stream_chunks: list[bytes] | None = None,
        ) -> None:
            if not app.state.guard.graph_state:
                return
            safe_meta = {k: v for k, v in metadata.items() if "secret" not in str(k).lower() and "authorization" not in str(k).lower()}
            safe_meta.setdefault("provider", provider)
            safe_meta.setdefault("route_family", route_family)
            safe_meta.setdefault("route", request.url.path)
            safe_meta.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            capture_history_record(
                app.state.guard.graph_state,
                app.state.settings,
                request_raw=raw,
                response_raw=response_raw,
                stream_chunks=stream_chunks,
                metadata=safe_meta,
            )

        payload, raw = await _load_json_body(request)
        if payload is None:
            body = {"error": {"message": "invalid_json"}}
            response = JSONResponse(status_code=400, content=body)
            _capture_history(
                metadata={
                    "request_id": f"invalid-json-{time.time_ns()}",
                    "decision": "BLOCKED",
                    "reason": "invalid_json",
                    "http_status": 400,
                },
                response_raw=response.body,
            )
            return response

        adapter = _resolve_adapter(provider, stream=stream)
        if adapter is None:
            body = {"error": {"message": "unknown_provider"}}
            response = JSONResponse(status_code=400, content=body)
            _capture_history(
                metadata={
                    "request_id": f"unknown-provider-{time.time_ns()}",
                    "decision": "BLOCKED",
                    "reason": "unknown_provider",
                    "http_status": 400,
                    "provider": provider,
                    "route_family": route_family,
                },
                response_raw=response.body,
            )
            return response

        model = adapter.model_name(payload, route_model=route_model, deployment=deployment, operation=operation)
        canonical = adapter.canonical_payload(payload, model, route_model=route_model, deployment=deployment, operation=operation)
        auth_header = adapter.auth_header(authorization, x_goog_api_key=x_goog_api_key)
        history_meta: dict[str, Any] = {
            "provider": adapter.provider,
            "route_family": route_family,
            "route": request.url.path,
            "model": model or "",
        }
        status, data, headers = process_chat_completion(
            canonical,
            auth_header,
            app.state.guard,
            app.state.policy,
            app.state.verifier,
            source_ip=request.client.host if request.client else "unknown",
            raw_body=raw,
            provider=adapter.provider,
            enforce_provider=adapter.enforce_provider,
            model_override=model or None,
            upstream_url=adapter.upstream_url(request, model, route_model=route_model, deployment=deployment, operation=operation),
            forward_body=adapter.forward_body(raw, payload, route_model=route_model, deployment=deployment, operation=operation),
            forward_content_type=adapter.forward_content_type(payload, route_model=route_model, deployment=deployment, operation=operation),
            history_meta=history_meta,
        )

        content_type = headers.get("content-type", "application/json")
        if isinstance(data, bytes):
            _capture_history(metadata={**history_meta, "http_status": status}, response_raw=data)
            return Response(content=data, status_code=status, media_type=content_type)
        if status != 200:
            response = JSONResponse(status_code=status, content=data)
            _capture_history(metadata={**history_meta, "http_status": status}, response_raw=response.body)
            return response

        if adapter.should_stream(payload, route_model=route_model, deployment=deployment, operation=operation):
            chunks = list(adapter.stream_chunks(data, model, payload, route_model=route_model, deployment=deployment, operation=operation))
            reconstructed = adapter.success_payload(data, model, payload, route_model=route_model, deployment=deployment, operation=operation)
            reconstructed_raw = json.dumps(reconstructed, sort_keys=True).encode("utf-8")
            _capture_history(
                metadata={**history_meta, "http_status": status},
                response_raw=reconstructed_raw,
                stream_chunks=chunks,
            )
            return StreamingResponse(
                iter(chunks),
                media_type=adapter.stream_media_type,
            )
        response_payload = adapter.success_payload(data, model, payload, route_model=route_model, deployment=deployment, operation=operation)
        response = JSONResponse(
            status_code=status,
            content=response_payload,
        )
        _capture_history(metadata={**history_meta, "http_status": status}, response_raw=response.body)
        return response

    app.include_router(create_admin_session_router())
    app.include_router(create_provider_openai_router(_handle_adapter_route))
    app.include_router(create_provider_azure_router(_handle_adapter_route))
    app.include_router(create_provider_ollama_router(_handle_adapter_route))
    app.include_router(create_provider_gemini_router(_handle_adapter_route))
    app.include_router(create_admin_keys_router(render_admin_keys_page))
    app.include_router(create_admin_policy_router(render_admin_policy_page))
    app.include_router(create_admin_usage_router())
    app.include_router(create_admin_history_router())
    app.include_router(create_admin_security_router())
    app.include_router(create_admin_oidc_router())

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
