from __future__ import annotations

import hashlib
import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
import httpx
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .core import ModelKey, ModelKeyGuard, Principal, Request as GuardRequest
from .graph_state import GraphStateStore, resolve_store_backend
from .key_manager import KeyLifecycleError, KeyManager
from .providers import AzureOpenAIAdapter, GeminiAdapter, OllamaAdapter, OpenAIAdapter, ProviderAdapter, default_upstream_url
from .services import derive_prompt_heuristics, get_static_dir, render_admin_keys_page, render_admin_policy_page
from .services.admin_auth import ADMIN_COOKIE_NAME, ADMIN_HEADER_NAME, admin_html_login_response, decode_admin_session, verify_admin_session
from .services.history_ops import capture_history_record
from .services.pricing_ops import resolve_price_per_1k_tokens_usd
from .settings import AppSettings, read_env_or_file
from .token_auth import TokenAuthError, TokenVerifier, TokenPrincipal
from .policy_loader import load_policy_json

DEFAULT_POLICY = Path(os.getenv("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
AUDIT_PATH = Path(os.getenv("MODELKEYGUARD_AUDIT_PATH", "out/audit.jsonl"))


@dataclass
class UpstreamStream:
    secret: str
    raw: bytes
    provider: str
    target_url: str
    content_type: str
    request_id: str
    key_id: str
    model: str
    decision: Any
    estimated_cost: float
    estimated_tokens: int
    policy: dict[str, Any]
    graph_state: GraphStateStore | None
    idempotency_scope: str | None = None
    opened_client: Any = None
    opened_response: Any = None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_IDEMPOTENCY_NAMESPACE = "modelkeyguard_idempotency"


def _idempotency_read(guard: ModelKeyGuard, scope: str) -> dict[str, Any] | None:
    cache = getattr(guard, "_idempotency_cache", {})
    local = cache.get(scope)
    if isinstance(local, dict) and float(local.get("expires", 0)) > time.time():
        return dict(local)
    state = getattr(guard, "graph_state", None)
    getter = getattr(state, "get_named_projection", None)
    if callable(getter):
        row = getter(_IDEMPOTENCY_NAMESPACE, sha256_text(scope))
        payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
        if isinstance(payload, dict) and float(payload.get("expires", 0)) > time.time():
            result = dict(payload)
            encoded = result.pop("body_b64", None)
            if isinstance(encoded, str):
                result["body"] = base64.b64decode(encoded.encode("ascii"))
            cache[scope] = result
            setattr(guard, "_idempotency_cache", cache)
            return result
    return None


def _idempotency_write(guard: ModelKeyGuard, scope: str, record: dict[str, Any], *, claim: bool = False) -> bool:
    state = getattr(guard, "graph_state", None)
    if claim:
        cas = getattr(state, "compare_and_swap_named_projections", None)
        getter = getattr(state, "get_named_projection", None)
        if callable(cas) and callable(getter):
            namespace_key = sha256_text(scope)
            current = getter(_IDEMPOTENCY_NAMESPACE, namespace_key)
            current_payload = current.get("payload") if isinstance(current, dict) and isinstance(current.get("payload"), dict) else current
            if isinstance(current_payload, dict) and float(current_payload.get("expires", 0)) > time.time():
                return False
            update: dict[str, Any] = {"namespace": _IDEMPOTENCY_NAMESPACE, "key": namespace_key, "payload": dict(record)}
            if isinstance(current, dict) and "payload" in current:
                expected_a = int(current.get("last_authoritative_seq", 0))
                expected_m = int(current.get("last_materialized_seq", 0))
                update.update(
                    expected_last_authoritative_seq=expected_a,
                    expected_last_materialized_seq=expected_m,
                    last_authoritative_seq=expected_a + 1,
                    last_materialized_seq=expected_m + 1,
                )
            else:
                update["expected_payload_hash"] = current
            try:
                if not bool(cas([update])):
                    return False
            except Exception:
                return False
    cache = getattr(guard, "_idempotency_cache", None) or {}
    if claim:
        cache[scope] = dict(record)
        setattr(guard, "_idempotency_cache", cache)
        return True

    # State transitions preserve projection versions and use CAS; unconditional
    # replace would permit a second worker/restart to clobber a newer outcome.
    getter = getattr(state, "get_named_projection", None)
    cas = getattr(state, "compare_and_swap_named_projections", None)
    replacer = getattr(state, "replace_named_projection", None)
    payload = dict(record)
    body = payload.pop("body", None)
    if isinstance(body, bytes):
        payload["body_b64"] = base64.b64encode(body).decode("ascii")
    if callable(getter) and callable(cas):
        projection_key = sha256_text(scope)
        attempts = max(1, int(os.getenv("MODELKEYGUARD_IDEMPOTENCY_CAS_RETRIES", "3")))
        for _attempt in range(attempts):
            current = getter(_IDEMPOTENCY_NAMESPACE, projection_key)
            existing = current.get("payload") if isinstance(current, dict) and isinstance(current.get("payload"), dict) else (current if isinstance(current, dict) else {})
            merged = dict(existing)
            merged.update(payload)
            update: dict[str, Any] = {"namespace": _IDEMPOTENCY_NAMESPACE, "key": projection_key, "payload": merged}
            if isinstance(current, dict) and "payload" in current:
                ea, em = int(current.get("last_authoritative_seq", 0)), int(current.get("last_materialized_seq", 0))
                update.update(expected_last_authoritative_seq=ea, expected_last_materialized_seq=em, last_authoritative_seq=ea + 1, last_materialized_seq=em + 1)
            else:
                update["expected_payload_hash"] = current if isinstance(current, dict) else None
            try:
                if bool(cas([update])):
                    cache[scope] = dict(merged)
                    if isinstance(body, bytes):
                        cache[scope]["body"] = body
                    setattr(guard, "_idempotency_cache", cache)
                    return True
            except Exception:
                break
        return False
    cache[scope] = dict(record)
    setattr(guard, "_idempotency_cache", cache)
    if callable(replacer):
        replacer(_IDEMPOTENCY_NAMESPACE, sha256_text(scope), payload)
    return True


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


def estimate_cost_and_tokens(
    payload: dict[str, Any],
    policy: dict[str, Any],
    *,
    key_id: str = "",
    provider: str = "",
    graph_state: GraphStateStore | None = None,
) -> tuple[float, int]:
    model = payload.get("model", "")
    price, _source = resolve_price_per_1k_tokens_usd(
        model=str(model),
        key_id=key_id,
        provider=provider,
        policy=policy,
        graph_state=graph_state,
    )
    messages = payload.get("messages", [])
    chars = len(json.dumps(messages))
    max_tokens = int(payload.get("max_tokens", payload.get("max_completion_tokens", 512)) or 512)
    estimated_tokens = max(1, chars // 4 + max_tokens)
    return round((estimated_tokens / 1000.0) * price, 6), estimated_tokens


def provider_usage(data: bytes, provider: str) -> tuple[int, int] | None:
    """Extract authoritative token usage when provider returned it."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) and provider == "gemini":
        usage = payload.get("usageMetadata") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            total = int(usage.get("totalTokenCount") or 0)
            return total, total
    if not isinstance(usage, dict):
        if provider == "ollama" and isinstance(payload, dict) and any(k in payload for k in ("prompt_eval_count", "eval_count")):
            prompt = int(payload.get("prompt_eval_count") or 0)
            completion = int(payload.get("eval_count") or 0)
            total = int(payload.get("eval_count_total") or (prompt + completion))
            return total, total
        return None
    if provider == "gemini":
        total = int(usage.get("totalTokenCount") or 0)
        return total, total
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return total, total


def settled_cost(
    response_body: bytes,
    *,
    provider: str,
    model: str,
    key_id: str,
    policy: dict[str, Any],
    graph_state: GraphStateStore | None,
    fallback_cost: float,
    fallback_tokens: int,
) -> tuple[float, int, bool]:
    usage = provider_usage(response_body, provider)
    if usage is None:
        return fallback_cost, fallback_tokens, False
    total, tokens = usage
    price, _ = resolve_price_per_1k_tokens_usd(
        model=model, key_id=key_id, provider=provider, policy=policy, graph_state=graph_state
    )
    return round((total / 1000.0) * price, 6), tokens, True


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


def _upstream_url_allowed(url: str) -> bool:
    """Reject unsafe provider targets before sending credentials.

    Production may explicitly allow private provider hosts; absent that list,
    loopback/private/link-local targets are denied. Local development remains
    compatible with the bundled Ollama endpoint.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        configured = {item.strip().lower() for item in os.getenv("MODELKEYGUARD_UPSTREAM_ALLOWED_HOSTS", "").split(",") if item.strip()}
        host_l = host.lower().rstrip(".")
        if configured and not any(host_l == item or host_l.endswith("." + item.lstrip("*.")) for item in configured):
            return False
        if configured:
            return True
        env = os.getenv("MODELKEYGUARD_ENV", "local").lower()
        if env not in {"prod", "production"}:
            return True
        try:
            ip = ipaddress.ip_address(host_l)
        except ValueError:
            try:
                resolved = {info[4][0] for info in socket.getaddrinfo(host_l, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
            except OSError:
                return False
            for address in resolved:
                try:
                    resolved_ip = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_reserved:
                    return False
            return True
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except (ValueError, TypeError):
        return False


def forward_provider(
    secret: str,
    raw: bytes,
    *,
    provider: str,
    url: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, str], bytes]:
    target_url = url or default_upstream_url(provider)
    if not _upstream_url_allowed(target_url):
        return 502, {"content-type": "application/json"}, json.dumps({"error": {"message": "provider_upstream_not_allowed"}}).encode("utf-8")
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


def _upstream_headers(secret: str, provider: str, content_type: str) -> dict[str, str]:
    headers = {"content-type": content_type}
    if provider == "azure_openai":
        headers["api-key"] = secret
    elif provider == "gemini":
        headers["x-goog-api-key"] = secret
    else:
        headers["authorization"] = f"Bearer {secret}"
    return headers


def _usage_from_stream_chunk(chunk: bytes, provider: str) -> dict[str, Any] | None:
    text = chunk.decode("utf-8", errors="ignore").strip()
    candidates = [text]
    if text.startswith("data:"):
        candidates.insert(0, text[5:].strip())
    for candidate in candidates:
        if not candidate or candidate == "[DONE]":
            continue
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage") or obj.get("usageMetadata")
        if isinstance(usage, dict):
            return usage
        response = obj.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            return response["usage"]
        if provider == "ollama" and any(key in obj for key in ("prompt_eval_count", "eval_count")):
            prompt = int(obj.get("prompt_eval_count") or 0)
            completion = int(obj.get("eval_count") or 0)
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    return None


async def _open_upstream_stream(stream: UpstreamStream) -> tuple[Any, Any]:
    timeout = float(os.getenv("MODELKEYGUARD_PROVIDER_TIMEOUT_SECONDS", "180"))
    if not _upstream_url_allowed(stream.target_url):
        raise ValueError("provider_upstream_not_allowed")
    client = httpx.AsyncClient(timeout=timeout)
    request = client.build_request(
        "POST", stream.target_url, content=stream.raw, headers=_upstream_headers(stream.secret, stream.provider, stream.content_type)
    )
    try:
        response = await client.send(request, stream=True)
    except Exception:
        await client.aclose()
        raise
    return client, response


async def _stream_upstream(
    stream: UpstreamStream,
    *,
    on_chunk: Any,
    on_complete: Any,
    on_uncertain: Any,
) -> Any:
    """Forward upstream bytes as they arrive; settle only after terminal EOF."""
    usage: dict[str, Any] | None = None
    usage_scan = bytearray()
    capture = bytearray()
    max_capture = int(os.getenv("MODELKEYGUARD_HISTORY_MAX_STREAM_BYTES", "1048576"))
    client = stream.opened_client
    response = stream.opened_response
    if client is None or response is None:
        client, response = await _open_upstream_stream(stream)
    try:
        upstream_rejected = response.status_code >= 400 and response.status_code < 500
        if response.status_code >= 500:
            await on_uncertain(response.status_code)
            error_body = await response.aread()
            if len(capture) < max_capture:
                capture.extend(error_body[: max_capture - len(capture)])
            return bytes(capture), None, response.status_code
        elif upstream_rejected:
            await on_complete(response.status_code, None, True)
            rejected_body = await response.aread()
            if len(capture) < max_capture:
                capture.extend(rejected_body[: max_capture - len(capture)])
            return bytes(capture), None, response.status_code
        async for chunk in response.aiter_raw():
            if not chunk:
                continue
            if len(capture) < max_capture:
                capture.extend(chunk[: max_capture - len(capture)])
            usage = _usage_from_stream_chunk(chunk, stream.provider) or usage
            usage_scan.extend(chunk)
            if len(usage_scan) > 131072:
                del usage_scan[:-65536]
            for line in bytes(usage_scan).splitlines():
                usage = _usage_from_stream_chunk(line, stream.provider) or usage
            await on_chunk(chunk)
        if response.status_code >= 500 or upstream_rejected:
            return bytes(capture), usage, response.status_code
        await on_complete(response.status_code, usage, False)
        return bytes(capture), usage, response.status_code
    except asyncio.CancelledError:
        # Client disconnect must not cancel upstream accounting.  Clear the
        # cancellation on this worker, drain to EOF (bounded by a dedicated
        # timeout), then settle or mark uncertain.  No chunks are enqueued
        # after disconnect, preventing an unbounded orphan queue.
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        drain_timeout = float(os.getenv("MODELKEYGUARD_PROVIDER_DRAIN_TIMEOUT_SECONDS", "180"))
        try:
            async with asyncio.timeout(drain_timeout):
                async for chunk in response.aiter_raw():
                    if not chunk:
                        continue
                    if len(capture) < max_capture:
                        capture.extend(chunk[: max_capture - len(capture)])
                    usage = _usage_from_stream_chunk(chunk, stream.provider) or usage
                    usage_scan.extend(chunk)
                    if len(usage_scan) > 131072:
                        del usage_scan[:-65536]
                    for line in bytes(usage_scan).splitlines():
                        usage = _usage_from_stream_chunk(line, stream.provider) or usage
            if response.status_code >= 500:
                await on_uncertain(response.status_code)
            else:
                await on_complete(response.status_code, usage, response.status_code < 400)
            return bytes(capture), usage, response.status_code
        except Exception:
            await on_uncertain(499)
            return bytes(capture), usage, 499
    except Exception:
        await on_uncertain(502)
        return bytes(capture), usage, 502
    finally:
        if client is not None:
            await client.aclose()


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
        "headers": {key: ("<redacted>" if key.lower() in {"authorization", "api-key", "x-goog-api-key", "proxy-authorization"} else value) for key, value in headers.items()},
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
    requested_key_id: str | None = None,
    idempotency_key: str | None = None,
    streaming: bool = False,
    dry_run: bool | None = None,
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

    if "model.invoke" not in set(principal_token.scopes):
        _set_meta(
            request_id=f"scope-{time.time_ns()}",
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            decision="BLOCKED",
            reason="model_invoke_scope_required",
            http_status=403,
            provider=provider,
        )
        return 403, {"error": {"message": "model_invoke_scope_required"}}, {"content-type": "application/json"}

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

    requested_key_id = (requested_key_id or "").strip() or _requested_modelkeyguard_key_id(payload)
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

    idem_key = str(idempotency_key or "").strip()
    idem_cache = getattr(guard, "_idempotency_cache", None) or {}
    setattr(guard, "_idempotency_cache", idem_cache)
    idem_scope = f"{principal_token.namespace}:{principal_token.principal_id}:{provider}:{key_id}:{idem_key}"
    idem_body = forward_body or raw_body or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if requested_key_id:
        idem_body = _strip_modelkeyguard_control_fields(payload)
    idem_hash = sha256_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        + "\x00"
        + hashlib.sha256(idem_body).hexdigest()
    )
    if idem_key:
        cached = _idempotency_read(guard, idem_scope)
        if cached and float(cached.get("expires", 0)) > time.time():
            if cached.get("payload_hash") != idem_hash:
                return 409, {"error": {"message": "idempotency_key_reused_with_different_payload"}}, {"content-type": "application/json"}
            if cached.get("state") == "uncertain":
                return 502, {"error": {"message": "idempotency_outcome_uncertain"}}, {"content-type": "application/json"}
            if cached.get("state") in {"reserved", "forwarding"}:
                return 409, {"error": {"message": "idempotency_request_in_progress"}}, {"content-type": "application/json"}
            if cached.get("state") == "settled" and cached.get("replayable") is False:
                return 409, {"error": {"message": "idempotency_stream_result_not_replayable"}}, {"content-type": "application/json"}
            cached_body = cached.get("body")
            if isinstance(cached_body, bytes):
                return int(cached.get("status", 200)), cached_body, dict(cached.get("headers") or {})
            return int(cached.get("status", 200)), cached_body, dict(cached.get("headers") or {})
        claimed = _idempotency_write(
            guard,
            idem_scope,
            {"payload_hash": idem_hash, "state": "forwarding", "expires": time.time() + float(os.getenv("MODELKEYGUARD_IDEMPOTENCY_RETENTION_SECONDS", "86400"))},
            claim=True,
        )
        if not claimed:
            return 409, {"error": {"message": "idempotency_request_in_progress"}}, {"content-type": "application/json"}
        idem_cache = getattr(guard, "_idempotency_cache", idem_cache)

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

    key_provider = str((guard.keys.get(key_id).provider if guard.keys.get(key_id) else provider) or provider)
    cost, estimated_tokens = estimate_cost_and_tokens(
        payload,
        policy,
        key_id=key_id,
        provider=key_provider or provider,
        graph_state=guard.graph_state,
    )
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

    if not guard.reserve_admission(
        GuardRequest(
            principal=Principal(principal_token.principal_id, principal_token.kind, principal_token.groups),
            key_id=key_id,
            model=str(model),
            namespace=principal_token.namespace,
            estimated_cost_usd=cost,
            estimated_tokens=estimated_tokens,
            request_id=decision.request_id,
            token_id=principal_token.token_id,
            on_behalf_of_user_id=principal_token.on_behalf_of_user_id,
        ),
        decision,
    ):
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "released"})
        _set_meta(http_status=429, decision="BLOCKED", reason="quota_reservation_conflict")
        return 429, {"error": {"message": "quota_reservation_conflict"}}, {"content-type": "application/json"}

    try:
        secret = resolve_secret(decision.secret_ref or "", guard)
    except KeyLifecycleError as e:
        guard.update_reservation(decision.request_id, "released")
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "released"})
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

    dry_run_mode = (os.getenv("MODELKEYGUARD_DRY_RUN", "1") == "1") if dry_run is None else bool(dry_run)
    if not secret:
        if dry_run_mode:
            guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens, usage_authoritative=False)
            dry_body = dry_run_response(str(model), principal_token.principal_id, key_id, event)
            if idem_key:
                _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "settled", "status": 200, "headers": {"content-type": "application/json"}, "body": dry_body})
            _set_meta(http_status=200)
            return 200, dry_body, {"content-type": "application/json"}
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(
                decision.request_id,
                "SECRET_RESOLUTION_DENIED",
                {"reason": "provider_secret_missing", "key_id": decision.key_id},
            )
        guard.update_reservation(decision.request_id, "released")
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "released"})
        _set_meta(
            http_status=403,
            decision="BLOCKED",
            reason="provider_secret_missing",
        )
        return 403, {"error": {"message": "provider_secret_missing", "key_id": decision.key_id}}, {"content-type": "application/json"}

    if dry_run_mode:
        guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=cost, actual_tokens=estimated_tokens, usage_authoritative=False)
        dry_body = dry_run_response(str(model), principal_token.principal_id, key_id, event)
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "settled", "status": 200, "headers": {"content-type": "application/json"}, "body": dry_body})
        _set_meta(http_status=200)
        return 200, dry_body, {"content-type": "application/json"}

    resolved_upstream_url = _merge_upstream_base(upstream_url, _key_upstream_override(policy, guard, key_id))
    provider_body = forward_body or raw_body or json.dumps(payload).encode("utf-8")
    if requested_key_id:
        provider_body = _strip_modelkeyguard_control_fields(payload)

    if streaming:
        # Admission and audit are committed before opening upstream.  The
        # response generator owns terminal settlement and remains responsible
        # for draining upstream after a client disconnect.
        guard.update_reservation(decision.request_id, "forwarding")
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(
                decision.request_id,
                "QUOTA_RESERVATION_FORWARDING",
                {"key_id": key_id, "provider": provider, "estimated_tokens": estimated_tokens},
            )
        stream = UpstreamStream(
            secret=secret,
            raw=provider_body,
            provider=provider,
            target_url=resolved_upstream_url or default_upstream_url(provider),
            content_type=forward_content_type,
            request_id=decision.request_id,
            key_id=key_id,
            model=str(model),
            decision=decision,
            estimated_cost=cost,
            estimated_tokens=estimated_tokens,
            policy=policy,
            graph_state=guard.graph_state,
            idempotency_scope=idem_scope if idem_key else None,
        )
        _set_meta(http_status=200, decision="FORWARDING")
        return 200, stream, {"content-type": "text/event-stream"}

    status, headers, body = forward_provider(
        secret,
        provider_body,
        provider=provider,
        url=resolved_upstream_url,
        content_type=forward_content_type,
    )
    if 200 <= status < 300:
        actual_cost, actual_tokens, usage_authoritative = settled_cost(
            body,
            provider=provider,
            model=str(model),
            key_id=key_id,
            policy=policy,
            graph_state=guard.graph_state,
            fallback_cost=cost,
            fallback_tokens=estimated_tokens,
        )
        settlement_ok = guard.record_usage(decision, estimated_cost_usd=cost, actual_cost_usd=actual_cost, actual_tokens=actual_tokens, usage_authoritative=usage_authoritative)
        _set_meta(http_status=status, decision="ALLOWED", usage_authoritative=usage_authoritative)
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "settled" if settlement_ok else "uncertain", "status": status, "headers": dict(headers), "body": body})
    elif 400 <= status < 500:
        # Provider rejected before inference: no usage debit.
        guard.update_reservation(decision.request_id, "released")
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(
                decision.request_id,
                "QUOTA_RESERVATION_RELEASED",
                {"reason": "provider_rejected", "http_status": status, "key_id": key_id},
            )
        _set_meta(http_status=status, decision="UPSTREAM_REJECTED")
        if idem_key:
            idem_cache.pop(idem_scope, None)
            state = getattr(guard, "graph_state", None)
            clearer = getattr(state, "clear_named_projection", None)
            if callable(clearer):
                clearer(_IDEMPOTENCY_NAMESPACE, sha256_text(idem_scope))
    else:
        # Timeout/5xx outcome may have reached provider. Preserve an explicit
        # uncertain state; never pretend estimated cost is actual usage.
        guard.update_reservation(decision.request_id, "uncertain")
        if guard.graph_state:
            guard.graph_state.append_access_conversation_event(
                decision.request_id,
                "QUOTA_RESERVATION_UNCERTAIN",
                {"reason": "upstream_outcome_unknown", "http_status": status, "key_id": key_id},
            )
        _set_meta(http_status=status, decision="UNCERTAIN", reason="upstream_outcome_unknown")
        if idem_key:
            _idempotency_write(guard, idem_scope, {**idem_cache.get(idem_scope, {}), "state": "uncertain"})
    return status, body, headers


def create_app(policy_path: str | Path = DEFAULT_POLICY):
    from fastapi import FastAPI, Request as FastAPIRequest
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    from .routers import (
        create_admin_history_router,
        create_admin_keys_router,
        create_admin_policy_router,
        create_admin_review_router,
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
        x_modelkeyguard_key_id: str | None = None,
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
            requested_key_id=x_modelkeyguard_key_id,
            idempotency_key=request.headers.get("idempotency-key"),
            streaming=adapter.should_stream(payload, route_model=route_model, deployment=deployment, operation=operation),
            dry_run=app.state.settings.dry_run,
        )

        content_type = headers.get("content-type", "application/json")
        if isinstance(data, bytes):
            _capture_history(metadata={**history_meta, "http_status": status}, response_raw=data)
            return Response(content=data, status_code=status, media_type=content_type)
        if status != 200:
            response = JSONResponse(status_code=status, content=data)
            _capture_history(metadata={**history_meta, "http_status": status}, response_raw=response.body)
            return response

        if adapter.should_stream(payload, route_model=route_model, deployment=deployment, operation=operation) and isinstance(data, (UpstreamStream, dict)):
            max_capture = int(os.getenv("MODELKEYGUARD_HISTORY_MAX_STREAM_BYTES", "1048576"))
            if isinstance(data, UpstreamStream):
                try:
                    data.opened_client, data.opened_response = await _open_upstream_stream(data)
                except Exception as exc:
                    app.state.guard.update_reservation(data.request_id, "uncertain")
                    if data.graph_state:
                        data.graph_state.append_access_conversation_event(
                            data.request_id,
                            "QUOTA_RESERVATION_UNCERTAIN",
                            {"reason": "upstream_open_failed", "error": exc.__class__.__name__, "provider": data.provider},
                        )
                    if data.idempotency_scope:
                        _idempotency_write(
                            app.state.guard,
                            data.idempotency_scope,
                            {"state": "uncertain", "status": 502, "replayable": False, "expires": time.time() + float(os.getenv("MODELKEYGUARD_IDEMPOTENCY_RETENTION_SECONDS", "86400"))},
                        )
                    return JSONResponse(status_code=502, content={"error": {"message": "provider_upstream_unreachable"}})
                if data.opened_response.status_code >= 400:
                    rejected_status = int(data.opened_response.status_code)
                    rejected_body = await data.opened_response.aread()
                    rejected_content_type = data.opened_response.headers.get("content-type", "application/json")
                    await data.opened_response.aclose()
                    await data.opened_client.aclose()
                    app.state.guard.update_reservation(data.request_id, "released" if rejected_status < 500 else "uncertain")
                    if data.idempotency_scope:
                        if rejected_status < 500:
                            cache = getattr(app.state.guard, "_idempotency_cache", {})
                            cache.pop(data.idempotency_scope, None)
                            state = getattr(app.state.guard, "graph_state", None)
                            clearer = getattr(state, "clear_named_projection", None)
                            if callable(clearer):
                                clearer(_IDEMPOTENCY_NAMESPACE, sha256_text(data.idempotency_scope))
                        else:
                            _idempotency_write(
                                app.state.guard,
                                data.idempotency_scope,
                                {"state": "uncertain", "status": rejected_status, "replayable": False, "expires": time.time() + float(os.getenv("MODELKEYGUARD_IDEMPOTENCY_RETENTION_SECONDS", "86400"))},
                            )
                    return Response(content=rejected_body, status_code=rejected_status, media_type=rejected_content_type)

            async def bounded_stream():
                # Bound pending output: slow clients must backpressure upstream;
                # after disconnect, producer drains without retaining chunks.
                queue_limit = max(1, int(os.getenv("MODELKEYGUARD_STREAM_QUEUE_CHUNKS", "32")))
                queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_limit)
                terminal: dict[str, Any] = {}
                disconnected = asyncio.Event()

                async def on_chunk(chunk: bytes) -> None:
                    if disconnected.is_set():
                        return
                    if not terminal.get("streaming_started") and isinstance(data, UpstreamStream):
                        terminal["streaming_started"] = True
                        app.state.guard.update_reservation(data.request_id, "streaming")
                        if data.graph_state:
                            data.graph_state.append_access_conversation_event(
                                data.request_id,
                                "QUOTA_RESERVATION_STREAMING",
                                {"provider": data.provider, "key_id": data.key_id},
                            )
                    while not disconnected.is_set():
                        try:
                            await asyncio.wait_for(queue.put(bytes(chunk)), timeout=0.5)
                            return
                        except asyncio.TimeoutError:
                            continue

                async def on_complete(http_status: int, usage: dict[str, Any] | None, released: bool) -> None:
                    terminal.update(http_status=http_status, usage=usage, released=released, settled=not released)
                    if isinstance(data, UpstreamStream):
                        # Keep successful reservation forwarding/streaming until
                        # quota CAS settlement below; otherwise EOF-before-CAS
                        # crash leaves a false settled state and lost debit.
                        data.graph_state and app.state.guard.update_reservation(data.request_id, "released" if released else "forwarding")

                async def on_uncertain(http_status: int) -> None:
                    terminal.update(http_status=http_status, uncertain=True)
                    if isinstance(data, UpstreamStream):
                        data.graph_state and app.state.guard.update_reservation(data.request_id, "uncertain")

                async def run_upstream() -> None:
                    try:
                        if isinstance(data, UpstreamStream):
                            body, usage, upstream_status = await _stream_upstream(
                                data, on_chunk=on_chunk, on_complete=on_complete, on_uncertain=on_uncertain
                            )
                        else:
                            body = b""
                            for synthetic_chunk in adapter.stream_chunks(data, model, payload, route_model=route_model, deployment=deployment, operation=operation):
                                body += bytes(synthetic_chunk)
                                await on_chunk(bytes(synthetic_chunk))
                            usage = data.get("usage") if isinstance(data, dict) else None
                            upstream_status = status
                            await on_complete(upstream_status, usage, False)
                        terminal.setdefault("body", body)
                        terminal.setdefault("usage", usage)
                        terminal.setdefault("http_status", upstream_status)
                    finally:
                        while True:
                            try:
                                queue.put_nowait(None)
                                break
                            except asyncio.QueueFull:
                                if not disconnected.is_set():
                                    await queue.put(None)
                                    break
                                try:
                                    queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    continue

                task = asyncio.create_task(run_upstream())
                captured: list[bytes] = []
                captured_bytes = 0
                try:
                    while True:
                        chunk = await queue.get()
                        if chunk is None:
                            break
                        if captured_bytes < max_capture:
                            room = max_capture - captured_bytes
                            captured.append(chunk[:room])
                            captured_bytes += min(len(chunk), room)
                        yield chunk
                    await task
                finally:
                    disconnected.set()
                    if not task.done():
                        # Do not abandon an upstream request on client disconnect.
                        while not task.done():
                            try:
                                await asyncio.shield(task)
                            except asyncio.CancelledError:
                                current = asyncio.current_task()
                                if current is not None and hasattr(current, "uncancel"):
                                    current.uncancel()
                        # Preserve settlement/history after the disconnected
                        # client has been detached; response output is simply
                        # discarded by ASGI.
                    body = bytes(b"".join(captured))
                    usage = terminal.get("usage")
                    usage_authoritative: bool | None = None
                    if terminal.get("settled") and not terminal.get("uncertain") and isinstance(data, UpstreamStream):
                        usage_body = json.dumps({"usage": usage}).encode("utf-8") if usage else body
                        actual_cost, actual_tokens, authoritative = settled_cost(
                            usage_body,
                            provider=data.provider,
                            model=data.model,
                            key_id=data.key_id,
                            policy=data.policy,
                            graph_state=data.graph_state,
                            fallback_cost=data.estimated_cost,
                            fallback_tokens=data.estimated_tokens,
                        )
                        usage_authoritative = authoritative
                        settlement_ok = data.decision and app.state.guard.record_usage(
                            data.decision,
                            estimated_cost_usd=data.estimated_cost,
                            actual_cost_usd=actual_cost,
                            actual_tokens=actual_tokens,
                            usage_authoritative=usage_authoritative,
                        )
                        if data.graph_state and settlement_ok:
                            app.state.guard.update_reservation(data.request_id, "settled", actual_cost, actual_tokens)
                        elif data.graph_state and not settlement_ok:
                            app.state.guard.update_reservation(data.request_id, "uncertain", actual_cost, actual_tokens)
                        if data.graph_state:
                            data.graph_state.append_access_conversation_event(
                                data.request_id,
                                "QUOTA_RESERVATION_SETTLED" if settlement_ok else "QUOTA_RESERVATION_UNCERTAIN",
                                {"usage_authoritative": authoritative, "actual_tokens": actual_tokens, "actual_cost_usd": actual_cost, "settlement_ok": bool(settlement_ok)},
                            )
                        if data.idempotency_scope:
                            _idempotency_write(
                                app.state.guard,
                                data.idempotency_scope,
                                {"state": "settled" if settlement_ok else "uncertain", "replayable": False, "status": terminal.get("http_status", 200), "headers": {"content-type": adapter.stream_media_type}, "expires": time.time() + float(os.getenv("MODELKEYGUARD_IDEMPOTENCY_RETENTION_SECONDS", "86400"))},
                            )
                    elif isinstance(data, UpstreamStream) and data.idempotency_scope:
                        _idempotency_write(
                            app.state.guard,
                            data.idempotency_scope,
                            {"state": "uncertain" if terminal.get("uncertain") else "released", "status": terminal.get("http_status", 502), "headers": {"content-type": adapter.stream_media_type}, "body": body, "expires": time.time() + float(os.getenv("MODELKEYGUARD_IDEMPOTENCY_RETENTION_SECONDS", "86400"))},
                        )
                    _capture_history(
                        metadata={**history_meta, "http_status": terminal.get("http_status", status), "usage_authoritative": usage_authoritative, "usage_estimated": usage_authoritative is False},
                        response_raw=body,
                        stream_chunks=captured,
                    )
            return StreamingResponse(
                bounded_stream(),
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
    app.include_router(create_admin_review_router())
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
