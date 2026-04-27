from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any

from ..graph_state import GraphStateStore, iso_now
from ..settings import AppSettings

HISTORY_META_NS = "modelkeyguard.history.meta"
HISTORY_BLOB_NS = "modelkeyguard.history.blob"
HISTORY_INDEX_NS = "modelkeyguard.history.index"
HISTORY_CONFIG_NS = "modelkeyguard.history.config"
HISTORY_CONFIG_KEY = "runtime"
HISTORY_ACTIVE_WINDOW_KEY = "active_window"
HISTORY_NODE_KIND = "request_response_history"
HISTORY_EVENT_KIND = "REQUEST_RESPONSE_HISTORY_CAPTURED"


def default_history_config(settings: AppSettings) -> dict[str, Any]:
    return {
        "enabled": bool(settings.history_enabled),
        "retention_days": int(settings.history_retention_days),
        "max_active_records": int(settings.history_max_active_records),
        "max_active_bytes": int(settings.history_max_active_bytes),
        "updated_at": iso_now(),
        "source": "env",
    }


def get_history_config(graph_state: GraphStateStore, settings: AppSettings) -> dict[str, Any]:
    cfg = default_history_config(settings)
    runtime = _projection_get(graph_state, HISTORY_CONFIG_NS, HISTORY_CONFIG_KEY)
    if isinstance(runtime, dict):
        cfg.update(
            {
                "enabled": bool(runtime.get("enabled", cfg["enabled"])),
                "retention_days": max(1, int(runtime.get("retention_days", cfg["retention_days"]))),
                "max_active_records": max(1, int(runtime.get("max_active_records", cfg["max_active_records"]))),
                "max_active_bytes": max(1, int(runtime.get("max_active_bytes", cfg["max_active_bytes"]))),
                "updated_at": str(runtime.get("updated_at") or cfg["updated_at"]),
                "source": "runtime",
            }
        )
    return cfg


def update_history_config(graph_state: GraphStateStore, settings: AppSettings, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = get_history_config(graph_state, settings)
    if "enabled" in payload:
        cfg["enabled"] = bool(payload.get("enabled"))
    if "retention_days" in payload:
        cfg["retention_days"] = max(1, int(payload.get("retention_days") or cfg["retention_days"]))
    if "max_active_records" in payload:
        cfg["max_active_records"] = max(1, int(payload.get("max_active_records") or cfg["max_active_records"]))
    if "max_active_bytes" in payload:
        cfg["max_active_bytes"] = max(1, int(payload.get("max_active_bytes") or cfg["max_active_bytes"]))
    cfg["updated_at"] = iso_now()
    cfg["source"] = "runtime"
    _projection_replace(graph_state, HISTORY_CONFIG_NS, HISTORY_CONFIG_KEY, cfg)
    refresh_active_window(graph_state, settings)
    return cfg


def capture_history_record(
    graph_state: GraphStateStore,
    settings: AppSettings,
    *,
    request_raw: bytes,
    response_raw: bytes,
    stream_chunks: list[bytes] | None,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    cfg = get_history_config(graph_state, settings)
    if not cfg.get("enabled", True):
        return None

    request_id = str(metadata.get("request_id") or f"req-{int(time.time() * 1000)}")
    ts = str(metadata.get("ts") or iso_now())
    request_text = _decode_body(request_raw)
    response_text = _decode_body(response_raw)
    chunk_text = [_decode_body(c) for c in (stream_chunks or [])]
    request_sha = hashlib.sha256(request_raw).hexdigest()
    response_sha = hashlib.sha256(response_raw).hexdigest()
    node_id = f"history:{request_id}:{int(time.time() * 1000)}"

    node_payload = {
        "request_id": request_id,
        "ts": ts,
        "metadata": dict(metadata),
        "request_body_text": request_text,
        "response_body_text": response_text,
        "stream_chunks": chunk_text,
        "request_sha256": request_sha,
        "response_sha256": response_sha,
    }
    graph_state.put_node(node_id, HISTORY_NODE_KIND, node_payload)
    graph_state.append_event(
        HISTORY_EVENT_KIND,
        node_id,
        {
            "request_id": request_id,
            "ts": ts,
            "provider": metadata.get("provider"),
            "route_family": metadata.get("route_family"),
            "route": metadata.get("route"),
            "model": metadata.get("model"),
            "decision": metadata.get("decision"),
            "http_status": metadata.get("http_status"),
            "request_sha256": request_sha,
            "response_sha256": response_sha,
            "request_bytes": len(request_raw),
            "response_bytes": len(response_raw),
            "stream_chunk_count": len(chunk_text),
        },
    )

    _link_to_access_events(graph_state, node_id, request_id)

    meta_payload = {
        "request_id": request_id,
        "node_id": node_id,
        "ts": ts,
        "provider": str(metadata.get("provider") or ""),
        "route_family": str(metadata.get("route_family") or ""),
        "route": str(metadata.get("route") or ""),
        "model": str(metadata.get("model") or ""),
        "principal_id": str(metadata.get("principal_id") or ""),
        "on_behalf_of_user_id": str(metadata.get("on_behalf_of_user_id") or ""),
        "key_id": str(metadata.get("key_id") or ""),
        "token_id": str(metadata.get("token_id") or ""),
        "subject_type": str(metadata.get("subject_type") or "principal"),
        "subject_id": str(metadata.get("subject_id") or metadata.get("principal_id") or ""),
        "decision": str(metadata.get("decision") or ""),
        "reason": str(metadata.get("reason") or ""),
        "http_status": int(metadata.get("http_status") or 0),
        "request_bytes": len(request_raw),
        "response_bytes": len(response_raw),
        "total_bytes": len(request_raw) + len(response_raw),
        "request_sha256": request_sha,
        "response_sha256": response_sha,
        "stream_chunk_count": len(chunk_text),
    }
    blob_payload = {
        "request_id": request_id,
        "node_id": node_id,
        "request_sha256": request_sha,
        "response_sha256": response_sha,
        "stream_chunk_count": len(chunk_text),
    }
    _projection_replace(graph_state, HISTORY_META_NS, request_id, meta_payload)
    _projection_replace(graph_state, HISTORY_BLOB_NS, request_id, blob_payload)
    refresh_active_window(graph_state, settings)
    return meta_payload


def refresh_active_window(graph_state: GraphStateStore, settings: AppSettings) -> dict[str, Any]:
    cfg = get_history_config(graph_state, settings)
    rows = [payload for _k, payload in _projection_list(graph_state, HISTORY_META_NS)]
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(cfg["retention_days"]))
    filtered = [r for r in rows if _parse_ts(r.get("ts")) >= cutoff]
    filtered.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("request_id") or "")), reverse=True)

    active: list[str] = []
    used_bytes = 0
    max_records = int(cfg["max_active_records"])
    max_bytes = int(cfg["max_active_bytes"])
    for row in filtered:
        if len(active) >= max_records:
            break
        rid = str(row.get("request_id") or "")
        if not rid:
            continue
        row_bytes = max(0, int(row.get("total_bytes") or 0))
        if used_bytes + row_bytes > max_bytes:
            continue
        active.append(rid)
        used_bytes += row_bytes

    window = {
        "request_ids": active,
        "total_bytes": used_bytes,
        "generated_at": iso_now(),
        "retention_days": int(cfg["retention_days"]),
        "max_active_records": max_records,
        "max_active_bytes": max_bytes,
    }
    _projection_replace(graph_state, HISTORY_INDEX_NS, HISTORY_ACTIVE_WINDOW_KEY, window)
    return window


def list_history(
    graph_state: GraphStateStore,
    settings: AppSettings,
    *,
    filters: dict[str, Any],
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    window = refresh_active_window(graph_state, settings)
    rows = _active_rows(graph_state, window)
    rows = [r for r in rows if _matches_filters(r, filters)]
    rows.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("request_id") or "")), reverse=True)

    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    total = len(rows)

    return {
        "filters": filters,
        "page": page,
        "page_size": page_size,
        "total": total,
        "data": rows[start:end],
        "config": get_history_config(graph_state, settings),
        "active_window": window,
    }


def get_history_detail(graph_state: GraphStateStore, settings: AppSettings, request_id: str) -> dict[str, Any] | None:
    window = refresh_active_window(graph_state, settings)
    active = {str(x) for x in (window.get("request_ids") or [])}
    if request_id not in active:
        return None
    meta = _projection_get(graph_state, HISTORY_META_NS, request_id)
    if not isinstance(meta, dict):
        return None
    node_id = str(meta.get("node_id") or "")
    node = graph_state.nodes.get(node_id)
    if not node or node.kind != HISTORY_NODE_KIND:
        return None
    payload = node.payload
    return {
        "request_id": request_id,
        "metadata": meta,
        "request_body_text": str(payload.get("request_body_text") or ""),
        "response_body_text": str(payload.get("response_body_text") or ""),
        "stream_chunks": [str(x) for x in (payload.get("stream_chunks") or [])],
        "request_sha256": str(payload.get("request_sha256") or ""),
        "response_sha256": str(payload.get("response_sha256") or ""),
    }


def _active_rows(graph_state: GraphStateStore, window: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [str(x) for x in (window.get("request_ids") or [])]
    rows = []
    for rid in ids:
        payload = _projection_get(graph_state, HISTORY_META_NS, rid)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    time_range = str(filters.get("time_range") or "24h")
    if time_range and time_range != "all":
        now = datetime.now(timezone.utc)
        seconds = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(time_range, 86400)
        if _parse_ts(row.get("ts")) < (now - timedelta(seconds=seconds)):
            return False

    request_id = str(filters.get("request_id") or "")
    if request_id and str(row.get("request_id") or "") != request_id:
        return False

    for field in ("provider", "model", "decision", "route_family"):
        want = str(filters.get(field) or "")
        if want and str(row.get(field) or "") != want:
            return False

    for filter_field, row_field in (
        ("principal_id", "principal_id"),
        ("user_id", "on_behalf_of_user_id"),
        ("key_id", "key_id"),
        ("token_id", "token_id"),
    ):
        want = str(filters.get(filter_field) or "")
        if want and str(row.get(row_field) or "") != want:
            return False

    http_status = filters.get("http_status")
    if http_status not in (None, "") and int(row.get("http_status") or 0) != int(http_status):
        return False

    subject_type = str(filters.get("subject_type") or "")
    subject_id = str(filters.get("subject_id") or "")
    if subject_type and subject_id:
        field_map = {
            "principal": "principal_id",
            "user": "on_behalf_of_user_id",
            "key": "key_id",
            "token": "token_id",
        }
        field = field_map.get(subject_type)
        if field and str(row.get(field) or "") != subject_id:
            return False
    return True


def _decode_body(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("utf-8", errors="replace")


def _parse_ts(value: Any) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _link_to_access_events(graph_state: GraphStateStore, history_node_id: str, request_id: str) -> None:
    for node in graph_state.nodes.values():
        if node.kind != "access_conversation_event":
            continue
        if str(node.payload.get("request_id") or "") != request_id:
            continue
        edge_id = f"edge:{history_node_id}:FOR_REQUEST:{node.id}"
        if edge_id in graph_state.edges:
            continue
        graph_state.put_edge(edge_id, "FOR_REQUEST", history_node_id, node.id, {"request_id": request_id})


def _projection_get(graph_state: GraphStateStore, namespace: str, key: str) -> dict[str, Any] | None:
    if hasattr(graph_state, "get_named_projection"):
        rec = getattr(graph_state, "get_named_projection")(namespace, key)
        if isinstance(rec, dict):
            payload = rec.get("payload")
            return dict(payload) if isinstance(payload, dict) else None
        return None
    payload = graph_state.projections.get(f"{namespace}:{key}")
    return dict(payload) if isinstance(payload, dict) else None


def _projection_replace(graph_state: GraphStateStore, namespace: str, key: str, payload: dict[str, Any]) -> None:
    if hasattr(graph_state, "replace_named_projection"):
        getattr(graph_state, "replace_named_projection")(namespace, key, payload)
        return
    graph_state.put_projection(f"{namespace}:{key}", payload)


def _projection_list(graph_state: GraphStateStore, namespace: str) -> list[tuple[str, dict[str, Any]]]:
    if hasattr(graph_state, "list_named_projections"):
        rows = getattr(graph_state, "list_named_projections")(namespace)
        out: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            payload = row.get("payload")
            if key and isinstance(payload, dict):
                out.append((key, payload))
        return out
    prefix = f"{namespace}:"
    out: list[tuple[str, dict[str, Any]]] = []
    for key, payload in graph_state.projections.items():
        if not key.startswith(prefix):
            continue
        k = key[len(prefix) :]
        if isinstance(payload, dict):
            out.append((k, payload))
    return out
