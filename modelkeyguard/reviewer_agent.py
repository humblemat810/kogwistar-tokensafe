from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analytics import KeycloakServiceAccount
from .graph_state import GraphStateStore, iso_now, resolve_graph_app_key
from .services.history_ops import HISTORY_ACTIVE_WINDOW_KEY, HISTORY_INDEX_NS

REVIEW_CHECKPOINT_NAMESPACE = "modelkeyguard.review.checkpoint"
REVIEW_CHECKPOINT_KEY = "runtime"

DEFAULT_REVIEW_SYSTEM_PROMPT = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."
DEFAULT_DANGEROUS_KEYWORDS = (
    "secret",
    "token",
    "password",
    "credential",
    "bearer",
    "api key",
    "apikey",
    "private key",
    "exfiltrate",
    "dump",
    "reveal",
    "leak",
    "print",
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _parse_ts(value: Any) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ts_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ollama_chat_review(
    *,
    base_url: str,
    safe_token: str,
    model: str,
    system_prompt: str,
    prompt: str,
    key_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {safe_token}",
            "content-type": "application/json",
        },
        method="POST",
    )
    if key_id.strip():
        req.add_header("x-modelkeyguard-key-id", key_id.strip())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        hint = ""
        if exc.code == 401:
            hint = " Check REVIEWER_SAFE_TOKEN; the review status credential is separate from the model-call safe token."
        raise RuntimeError(f"review model call failed with HTTP {exc.code}: {body or exc.reason}.{hint}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("review response did not return a JSON object")
    return data


def _get_named_projection(graph_state: GraphStateStore, namespace: str, key: str) -> dict[str, Any] | None:
    getter = getattr(graph_state, "get_named_projection", None)
    if callable(getter):
        try:
            payload = getter(namespace, key)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    composite = f"{namespace}:{key}"
    payload = getattr(graph_state, "projections", {}).get(composite)
    return payload if isinstance(payload, dict) else None


def _replace_named_projection(graph_state: GraphStateStore, namespace: str, key: str, payload: dict[str, Any]) -> None:
    replacer = getattr(graph_state, "replace_named_projection", None)
    if callable(replacer):
        replacer(namespace, key, payload)
        return
    graph_state.put_projection(f"{namespace}:{key}", payload)


def _review_rules(policy: dict[str, Any]) -> dict[str, Any]:
    rules = policy.get("review_rules") if isinstance(policy.get("review_rules"), dict) else {}
    return rules if isinstance(rules, dict) else {}


def _review_threshold(rules: dict[str, Any], name: str, default: float) -> float:
    entry = rules.get(name)
    if isinstance(entry, dict):
        value = entry.get("threshold")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except Exception:
                return default
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, str):
        try:
            return float(entry)
        except Exception:
            return default
    return default


def _review_keywords(rules: dict[str, Any]) -> list[str]:
    entry = rules.get("dangerous_keywords")
    if isinstance(entry, dict):
        keywords = entry.get("keywords")
        if isinstance(keywords, list):
            return [str(k).strip().lower() for k in keywords if str(k).strip()]
    if isinstance(entry, list):
        return [str(k).strip().lower() for k in entry if str(k).strip()]
    return list(DEFAULT_DANGEROUS_KEYWORDS)


def _review_checkpoint(graph_state: GraphStateStore) -> dict[str, Any]:
    checkpoint = _get_named_projection(graph_state, REVIEW_CHECKPOINT_NAMESPACE, REVIEW_CHECKPOINT_KEY)
    if not checkpoint:
        return {
            "last_reviewed_ts": _ts_to_iso(datetime.fromtimestamp(0, tz=timezone.utc)),
            "last_reviewed_request_id": "",
            "reviewed_at": "",
            "reviewed_by": "",
            "projection_schema_version": 1,
        }
    payload = dict(checkpoint)
    payload.setdefault("last_reviewed_ts", _ts_to_iso(datetime.fromtimestamp(0, tz=timezone.utc)))
    payload.setdefault("last_reviewed_request_id", "")
    payload.setdefault("reviewed_at", "")
    payload.setdefault("reviewed_by", "")
    payload.setdefault("projection_schema_version", 1)
    return payload


def _active_request_ids(graph_state: GraphStateStore) -> set[str] | None:
    active_window = _get_named_projection(graph_state, HISTORY_INDEX_NS, HISTORY_ACTIVE_WINDOW_KEY)
    if isinstance(active_window, dict):
        ids = active_window.get("request_ids")
        if isinstance(ids, list) and ids:
            return {str(x) for x in ids if str(x)}
    return None


def _history_rows(graph_state: GraphStateStore) -> list[dict[str, Any]]:
    active_ids = _active_request_ids(graph_state)
    rows: list[dict[str, Any]] = []
    for node in graph_state.nodes.values():
        if node.kind != "request_response_history":
            continue
        request_id = str(node.payload.get("request_id") or "")
        if active_ids is not None and request_id not in active_ids:
            continue
        rows.append(
            {
                "request_id": request_id,
                "ts": str(node.payload.get("ts") or ""),
                "node_id": node.id,
                "request_body_text": str(node.payload.get("request_body_text") or ""),
                "response_body_text": str(node.payload.get("response_body_text") or ""),
                "metadata": dict(node.payload.get("metadata") or {}),
            }
        )
    rows.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("request_id") or "")))
    return rows


def _usage_rows(graph_state: GraphStateStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in graph_state.events:
        if str(event.get("kind") or "") != "MODEL_USAGE_RESULT":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        rows.append(
            {
                "ts": str(event.get("ts") or ""),
                "request_id": str(payload.get("request_id") or event.get("subject") or ""),
                "principal_id": str(payload.get("principal_id") or ""),
                "on_behalf_of_user_id": str(payload.get("on_behalf_of_user_id") or ""),
                "token_id": str(payload.get("token_id") or ""),
                "estimated_cost_usd": float(payload.get("estimated_cost_usd") or 0.0),
                "actual_cost_usd": float(payload.get("actual_cost_usd") or payload.get("estimated_cost_usd") or 0.0),
                "actual_tokens": int(payload.get("actual_tokens") or payload.get("estimated_tokens") or 0),
            }
        )
    rows.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("request_id") or "")))
    return rows


def _dangerous_keyword_hits(rows: list[dict[str, Any]], keywords: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for keyword in keywords:
        matched = [
            row
            for row in rows
            if keyword in f"{row.get('request_body_text', '')}\n{row.get('response_body_text', '')}".lower()
        ]
        if matched:
            hits.append(
                {
                    "keyword": keyword,
                    "count": len(matched),
                    "request_ids": [str(row.get("request_id") or "") for row in matched[:10]],
                }
            )
    return hits


def _history_preview(rows: list[dict[str, Any]], keyword_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: dict[str, set[str]] = {}
    for hit in keyword_hits:
        keyword = str(hit.get("keyword") or "")
        for request_id in hit.get("request_ids") or []:
            matched.setdefault(str(request_id), set()).add(keyword)
    preview: list[dict[str, Any]] = []
    for row in rows[-10:]:
        request_id = str(row.get("request_id") or "")
        preview.append(
            {
                "request_id": request_id,
                "ts": row.get("ts"),
                "matched_keywords": sorted(matched.get(request_id, set())),
            }
        )
    return preview


def compute_review_status(graph_state: GraphStateStore, policy: dict[str, Any]) -> dict[str, Any]:
    rules = _review_rules(policy)
    checkpoint = _review_checkpoint(graph_state)
    checkpoint_ts = _parse_ts(checkpoint.get("last_reviewed_ts"))
    history_rows = _history_rows(graph_state)
    usage_rows = _usage_rows(graph_state)
    history_after = [row for row in history_rows if _parse_ts(row.get("ts")) > checkpoint_ts]
    usage_after = [row for row in usage_rows if _parse_ts(row.get("ts")) > checkpoint_ts]

    keywords = _review_keywords(rules)
    keyword_hits = _dangerous_keyword_hits(history_after, keywords)
    conversation_count = len({str(row.get("request_id") or "") for row in history_after if str(row.get("request_id") or "")})
    usd_used = round(sum(float(row.get("actual_cost_usd") or 0.0) for row in usage_after), 8)
    token_used = int(sum(int(row.get("actual_tokens") or 0) for row in usage_after))
    latest_ts = max(
        [checkpoint_ts]
        + [_parse_ts(row.get("ts")) for row in history_after]
        + [_parse_ts(row.get("ts")) for row in usage_after]
    )
    latest_history = history_after[-1] if history_after else None
    latest_usage = usage_after[-1] if usage_after else None
    latest_request_id = ""
    if latest_history:
        latest_request_id = str(latest_history.get("request_id") or "")
    elif latest_usage:
        latest_request_id = str(latest_usage.get("request_id") or "")

    token_threshold = _review_threshold(rules, "token_used_since_last_review", 50000)
    dollar_threshold = _review_threshold(rules, "dollar_used_since_last_review", 1.0)
    conversation_threshold = _review_threshold(rules, "conversation_count_since_last_review", 10)
    dangerous_threshold = _review_threshold(rules, "dangerous_keyword_hits", 1)
    keyword_count = sum(int(hit.get("count") or 0) for hit in keyword_hits)

    triggers = [
        {
            "trigger_id": "token_used_since_last_review",
            "unit": "tokens",
            "threshold": token_threshold,
            "current": token_used,
            "triggered": token_used >= token_threshold,
        },
        {
            "trigger_id": "dollar_used_since_last_review",
            "unit": "usd",
            "threshold": dollar_threshold,
            "current": usd_used,
            "triggered": usd_used >= dollar_threshold,
        },
        {
            "trigger_id": "conversation_count_since_last_review",
            "unit": "conversations",
            "threshold": conversation_threshold,
            "current": conversation_count,
            "triggered": conversation_count >= conversation_threshold,
        },
        {
            "trigger_id": "dangerous_keyword_hits",
            "unit": "hits",
            "threshold": dangerous_threshold,
            "current": keyword_count,
            "triggered": bool(keyword_hits) and keyword_count >= dangerous_threshold,
            "keywords": keywords,
            "hits": keyword_hits,
        },
    ]

    should_review = any(bool(item.get("triggered")) for item in triggers)
    return {
        "generated_at": iso_now(),
        "reviewer": "modelkeyguard.reviewer_agent",
        "checkpoint": checkpoint,
        "window": {
            "history_records": len(history_after),
            "usage_records": len(usage_after),
            "latest_ts": _ts_to_iso(latest_ts),
            "latest_request_id": latest_request_id,
        },
        "summary": {
            "conversation_count_since_last_review": conversation_count,
            "dollar_used_since_last_review": usd_used,
            "token_used_since_last_review": token_used,
            "dangerous_keyword_hits": keyword_count,
        },
        "triggers": triggers,
        "should_review": should_review,
        "dangerous_keyword_hits": keyword_hits,
        "review_rules": rules,
        "history_preview": _history_preview(history_after, keyword_hits),
        "usage_preview": usage_after[-10:],
    }


def advance_review_checkpoint(
    graph_state: GraphStateStore,
    policy: dict[str, Any],
    *,
    reviewed_by: str = "reviewer-agent",
    review_summary: str = "",
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = status or compute_review_status(graph_state, policy)
    checkpoint = status.get("checkpoint") if isinstance(status.get("checkpoint"), dict) else {}
    latest_ts = str(status.get("window", {}).get("latest_ts") or checkpoint.get("last_reviewed_ts") or iso_now())
    latest_request_id = str(status.get("window", {}).get("latest_request_id") or checkpoint.get("last_reviewed_request_id") or "")
    payload = {
        "last_reviewed_ts": latest_ts,
        "last_reviewed_request_id": latest_request_id,
        "reviewed_at": iso_now(),
        "reviewed_by": reviewed_by,
        "review_summary": review_summary,
        "projection_schema_version": 1,
    }
    _replace_named_projection(graph_state, REVIEW_CHECKPOINT_NAMESPACE, REVIEW_CHECKPOINT_KEY, payload)
    return payload


def build_review_prompt(status: dict[str, Any]) -> str:
    return (
        "Review the following governance status and produce a short operator note.\n"
        "Do not include secrets, tokens, or raw request bodies.\n\n"
        f"{json.dumps(status, indent=2, sort_keys=True)}"
    )


def _message_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(str(item["content"]))
        return "".join(parts)
    return str(content)


def run_langchain_reviewer(
    *,
    status: dict[str, Any],
    base_url: str,
    safe_token: str,
    model: str = "gemma4:e2b",
    system_prompt: str = DEFAULT_REVIEW_SYSTEM_PROMPT,
    key_id: str = "",
) -> dict[str, Any]:
    prompt = build_review_prompt(status)
    if key_id.strip():
        result = _ollama_chat_review(
            base_url=base_url,
            safe_token=safe_token,
            model=model,
            system_prompt=system_prompt,
            prompt=prompt,
            key_id=key_id,
        )
        message = result.get("message") if isinstance(result, dict) else {}
        text = ""
        if isinstance(message, dict):
            text = str(message.get("content") or "")
        if not text:
            text = str(result.get("response") or "")
        return {
            "model": model,
            "base_url": base_url.rstrip("/"),
            "key_id": key_id.strip(),
            "text": text,
        }

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    headers = {"Authorization": f"Bearer {safe_token}"}
    try:
        llm = ChatOllama(model=model, base_url=base_url.rstrip("/"), client_kwargs={"headers": headers}, temperature=0)
    except TypeError:
        llm = ChatOllama(model=model, base_url=base_url.rstrip("/"), headers=headers, temperature=0)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
    result = llm.invoke(messages)
    return {
        "model": model,
        "base_url": base_url.rstrip("/"),
        "key_id": key_id.strip(),
        "text": _message_text(result),
    }


@dataclass(frozen=True)
class ReviewStatusClient:
    base_url: str
    bearer_token: str | None = None
    admin_secret: str | None = None
    keycloak: KeycloakServiceAccount | None = None
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls) -> "ReviewStatusClient":
        bearer_token = _env("MODELKEYGUARD_BEARER_TOKEN")
        keycloak = None
        if not bearer_token:
            client_secret = _env("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET")
            if client_secret:
                keycloak = KeycloakServiceAccount.from_env()
        admin_secret = _env("MODELKEYGUARD_ADMIN_API_SECRET")
        return cls(
            base_url=_env("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"),
            bearer_token=bearer_token or None,
            admin_secret=admin_secret or None,
            keycloak=keycloak,
            timeout_seconds=int(_env("MODELKEYGUARD_REVIEW_TIMEOUT_SECONDS", "10")),
        )

    def status(self) -> dict[str, Any]:
        return self._request_json("GET", "/admin/review/status.json")

    def checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/admin/review/checkpoint", payload)

    def _headers(self) -> dict[str, str]:
        if self.bearer_token:
            return {"Authorization": f"Bearer {self.bearer_token}"}
        if self.keycloak:
            return {"Authorization": f"Bearer {self.keycloak.mint_access_token()}"}
        if self.admin_secret:
            return {"x-modelkeyguard-admin-secret": self.admin_secret}
        raise RuntimeError("missing bearer token, admin secret, or Keycloak service account")

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        candidates = self._header_candidates()
        last_error: urllib.error.HTTPError | None = None
        for headers in candidates:
            req_headers = dict(headers)
            if data is not None:
                req_headers["content-type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    body = resp.read().decode("utf-8")
                parsed = json.loads(body or "{}")
                if not isinstance(parsed, dict):
                    raise RuntimeError("review endpoint did not return a JSON object")
                return parsed
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and len(candidates) > 1:
                    last_error = exc
                    continue
                raise
        if last_error is not None:
            try:
                body = last_error.read().decode("utf-8")
            except Exception:
                body = ""
            raise urllib.error.HTTPError(last_error.url, last_error.code, body or last_error.msg, last_error.hdrs, last_error.fp)
        raise RuntimeError("missing bearer token, admin secret, or Keycloak service account")

    def _header_candidates(self) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        if self.bearer_token:
            candidates.append({"Authorization": f"Bearer {self.bearer_token}"})
        if self.admin_secret:
            candidates.append({"x-modelkeyguard-admin-secret": self.admin_secret})
        if self.keycloak:
            candidates.append({"Authorization": f"Bearer {self.keycloak.mint_access_token()}"})
        return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modelkeyguard review-status",
        description="Query the reviewer checkpoint and trigger status",
    )
    parser.add_argument("--local", action="store_true", help="compute review status from the local graph store instead of calling the gateway")
    parser.add_argument("--policy", default=_env("MODELKEYGUARD_POLICY_PATH", "config/gateway_policy.json"))
    parser.add_argument("--base-url", default=_env("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"))
    parser.add_argument("--bearer-token", default=_env("MODELKEYGUARD_BEARER_TOKEN"))
    parser.add_argument("--admin-secret", default=_env("MODELKEYGUARD_ADMIN_API_SECRET"))
    parser.add_argument("--keycloak-url", default=_env("KEYCLOAK_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--realm", default=_env("KEYCLOAK_REALM", "modelguard"))
    parser.add_argument("--client-id", default=_env("MODELKEYGUARD_OIDC_USAGE_CLIENT_ID", "modelguard-usage-agent"))
    parser.add_argument("--client-secret", default=_env("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET"))
    args = parser.parse_args(argv)

    if args.local:
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        graph_path = Path(_env("MODELKEYGUARD_REVIEW_GRAPH_PATH", _env("MODELKEYGUARD_GRAPH_PATH", "out/modelkeyguard_graph.jsonl")))
        graph_state = GraphStateStore(graph_path, app_key=resolve_graph_app_key())
        print(json.dumps(compute_review_status(graph_state, policy), indent=2, sort_keys=True))
        return 0

    client = ReviewStatusClient(
        base_url=args.base_url,
        bearer_token=args.bearer_token or None,
        admin_secret=args.admin_secret or None,
        keycloak=KeycloakServiceAccount(
            keycloak_url=args.keycloak_url,
            realm=args.realm,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
        if not args.bearer_token and not args.admin_secret and args.client_secret
        else None,
    )
    try:
        status = client.status()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        print(body or f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
