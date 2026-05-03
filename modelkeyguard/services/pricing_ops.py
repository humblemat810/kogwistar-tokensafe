from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..graph_state import GraphStateStore

PRICING_PROJECTION_NAMESPACE = "modelkeyguard.pricing.policy"
PRICING_PROJECTION_KEY = "runtime"
PRICING_EVENT_UPSERT = "PRICING_POLICY_UPSERT"
PRICING_EVENT_REVOKE = "PRICING_POLICY_REVOKED"
DEFAULT_PRICE_PER_1K_USD = 0.002


def normalize_pricing_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if value not in {"key", "provider_model", "model"}:
        raise ValueError("pricing_scope_must_be_key_provider_model_or_model")
    return value


def normalize_pricing_subject(scope: str, subject: str) -> str:
    normalized_scope = normalize_pricing_scope(scope)
    value = str(subject or "").strip()
    if not value:
        raise ValueError("pricing_subject_required")
    if normalized_scope == "provider_model":
        if ":" not in value:
            raise ValueError("pricing_subject_for_provider_model_must_be_provider_colon_model")
        provider, model = value.split(":", 1)
        provider = provider.strip().lower()
        model = model.strip()
        if not provider or not model:
            raise ValueError("pricing_subject_for_provider_model_must_be_provider_colon_model")
        return f"{provider}:{model}"
    return value


def pricing_subject_for_provider_model(provider: str, model: str) -> str:
    return f"{str(provider or '').strip().lower()}:{str(model or '').strip()}"


def _projection_get(graph_state: GraphStateStore, namespace: str, key: str) -> dict[str, Any] | None:
    getter = getattr(graph_state, "get_named_projection", None)
    if callable(getter):
        try:
            rec = getter(namespace, key)
            payload = dict((rec or {}).get("payload") or {})
            if payload:
                return payload
        except Exception:
            pass
    raw = getattr(graph_state, "projections", {}).get(f"{namespace}:{key}")
    if isinstance(raw, dict):
        return dict(raw)
    return None


def _projection_replace(graph_state: GraphStateStore, namespace: str, key: str, payload: dict[str, Any]) -> None:
    replacer = getattr(graph_state, "replace_named_projection", None)
    if callable(replacer):
        try:
            replacer(
                namespace,
                key,
                payload,
                projection_schema_version=int(payload.get("projection_schema_version") or 1),
                materialization_status="active",
            )
            return
        except TypeError:
            replacer(namespace, key, payload)
            return
    graph_state.put_projection(f"{namespace}:{key}", payload)


def _seed_projection_from_policy(model_price_table: dict[str, Any]) -> dict[str, Any]:
    model_map: dict[str, float] = {}
    for model, price in (model_price_table or {}).items():
        try:
            model_map[str(model)] = float(price)
        except Exception:
            continue
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "active": {"key": {}, "provider_model": {}, "model": model_map},
        "latest_by_subject": {},
        "revisions": [],
        "updated_at_ms": now_ms,
        "projection_schema_version": 1,
    }


def rebuild_pricing_projection(
    graph_state: GraphStateStore,
    *,
    model_price_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = _seed_projection_from_policy(model_price_table or {})
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    revisions: list[dict[str, Any]] = []

    for event in getattr(graph_state, "events", []):
        kind = str(event.get("kind", ""))
        if kind not in {PRICING_EVENT_UPSERT, PRICING_EVENT_REVOKE}:
            continue
        payload = dict(event.get("payload") or {})
        scope = str(payload.get("scope", "")).strip().lower()
        subject = str(payload.get("subject", "")).strip()
        if scope not in {"key", "provider_model", "model"} or not subject:
            continue
        revision_ms = int(payload.get("revision_ms") or 0)
        event_id = str(event.get("id", ""))
        row = {
            "id": event_id,
            "scope": scope,
            "subject": subject,
            "price_per_1k_tokens_usd": payload.get("price_per_1k_tokens_usd"),
            "revoked": bool(payload.get("revoked")),
            "revision_ms": revision_ms,
            "reason": str(payload.get("reason", "")),
            "actor": str(payload.get("actor", "")),
            "ts": str(event.get("ts", "")),
        }
        revisions.append(row)
        key = (scope, subject)
        current = latest.get(key)
        current_key = (int(current.get("revision_ms") or 0), str(current.get("id", ""))) if current else (-1, "")
        row_key = (revision_ms, event_id)
        if row_key >= current_key:
            latest[key] = row

    revisions.sort(key=lambda r: (int(r.get("revision_ms") or 0), str(r.get("id", ""))))
    projection["revisions"] = revisions
    projection["latest_by_subject"] = {f"{scope}:{subject}": row for (scope, subject), row in latest.items()}
    projection["active"]["key"] = {}
    projection["active"]["provider_model"] = {}
    # model map already seeded from policy table as fallback baseline.
    for (scope, subject), row in latest.items():
        if bool(row.get("revoked")):
            if scope == "model":
                projection["active"]["model"].pop(subject, None)
            continue
        try:
            price = float(row.get("price_per_1k_tokens_usd"))
        except Exception:
            continue
        projection["active"].setdefault(scope, {})[subject] = price
    projection["updated_at_ms"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    _projection_replace(graph_state, PRICING_PROJECTION_NAMESPACE, PRICING_PROJECTION_KEY, projection)
    return projection


def load_pricing_projection(
    graph_state: GraphStateStore,
    *,
    model_price_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    loaded = _projection_get(graph_state, PRICING_PROJECTION_NAMESPACE, PRICING_PROJECTION_KEY)
    if isinstance(loaded, dict) and isinstance(loaded.get("active"), dict):
        return loaded
    return rebuild_pricing_projection(graph_state, model_price_table=model_price_table)


def append_pricing_revision(
    graph_state: GraphStateStore,
    *,
    scope: str,
    subject: str,
    price_per_1k_tokens_usd: float | None = None,
    revoked: bool = False,
    reason: str = "",
    actor: str = "",
    model_price_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_scope = normalize_pricing_scope(scope)
    normalized_subject = normalize_pricing_subject(normalized_scope, subject)
    if not revoked and price_per_1k_tokens_usd is None:
        raise ValueError("pricing_price_required")
    if price_per_1k_tokens_usd is not None and float(price_per_1k_tokens_usd) < 0:
        raise ValueError("pricing_price_must_be_non_negative")
    revision_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload: dict[str, Any] = {
        "scope": normalized_scope,
        "subject": normalized_subject,
        "revision_ms": revision_ms,
        "revoked": bool(revoked),
        "reason": str(reason or ""),
        "actor": str(actor or ""),
    }
    if price_per_1k_tokens_usd is not None:
        payload["price_per_1k_tokens_usd"] = float(price_per_1k_tokens_usd)
    event = graph_state.append_event(
        PRICING_EVENT_REVOKE if revoked else PRICING_EVENT_UPSERT,
        f"pricing:{normalized_scope}:{normalized_subject}",
        payload,
    )
    projection = rebuild_pricing_projection(graph_state, model_price_table=model_price_table)
    latest = dict((projection.get("latest_by_subject") or {}).get(f"{normalized_scope}:{normalized_subject}") or {})
    return {
        "event_id": str(event.get("id", "")),
        "scope": normalized_scope,
        "subject": normalized_subject,
        "latest": latest,
        "projection_updated_at_ms": int(projection.get("updated_at_ms") or 0),
    }


def resolve_price_per_1k_tokens_usd(
    *,
    model: str,
    key_id: str = "",
    provider: str = "",
    policy: dict[str, Any] | None = None,
    graph_state: GraphStateStore | None = None,
) -> tuple[float, str]:
    fallback_default = DEFAULT_PRICE_PER_1K_USD
    policy = policy or {}
    model_table = dict(policy.get("model_price_per_1k_tokens_usd") or {})
    try:
        if model in model_table:
            _ = float(model_table[model])
    except Exception:
        model_table.pop(model, None)
    projection: dict[str, Any] | None = None
    if graph_state is not None:
        projection = load_pricing_projection(graph_state, model_price_table=model_table)

    active = dict((projection or {}).get("active") or {})
    key_map = dict(active.get("key") or {})
    pm_map = dict(active.get("provider_model") or {})
    model_map = dict(active.get("model") or model_table)

    key_id = str(key_id or "").strip()
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip()
    if key_id and key_id in key_map:
        return float(key_map[key_id]), f"key:{key_id}"
    pm_subject = pricing_subject_for_provider_model(provider, model)
    if provider and model and pm_subject in pm_map:
        return float(pm_map[pm_subject]), f"provider_model:{pm_subject}"
    if model and model in model_map:
        return float(model_map[model]), f"model:{model}"
    return float(policy.get("default_price_per_1k_tokens_usd", fallback_default) or fallback_default), "default"
