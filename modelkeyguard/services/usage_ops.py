from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any


def load_usage_events(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def derive_prompt_heuristics(payload: dict[str, Any], usage_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    usage_profile = usage_profile or {}
    text = "\n".join(_extract_message_texts(payload)).lower()

    observed_intents: set[str] = set()
    if any(tok in text for tok in ("summarize", "summary", "bullet", "tl;dr", "tldr")):
        observed_intents.add("summarization")
    if "?" in text or any(tok in text for tok in ("what ", "why ", "how ", "explain", "answer")):
        observed_intents.add("qna")
    if any(tok in text for tok in ("code", "python", "javascript", "typescript", "refactor", "compile", "function", "class", "bug")):
        observed_intents.add("coding")

    exfiltration_signals = [
        token
        for token in ("api key", "apikey", "token", "secret", "password", "credential", "bearer", "sk-", "private key")
        if token in text
    ]
    token_exfiltration_attempt = bool(exfiltration_signals) and any(tok in text for tok in ("extract", "reveal", "show", "dump", "print", "leak", "exfiltrate"))

    allowed = {str(x).strip().lower() for x in usage_profile.get("allowed_intents", []) if str(x).strip()}
    disallowed = sorted(intent for intent in observed_intents if allowed and intent not in allowed)
    intent_drift = bool(disallowed)

    return {
        "observed_intents": sorted(observed_intents),
        "allowed_intents": sorted(allowed),
        "disallowed_intents": disallowed,
        "intent_drift": intent_drift,
        "token_exfiltration_attempt": token_exfiltration_attempt,
        "token_exfiltration_signals": exfiltration_signals,
    }


def build_usage_monitor_dataset(
    events: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    time_range: str = "24h",
    bucket: str = "hour",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=_range_seconds(time_range))
    bucket_seconds = _bucket_seconds(bucket)

    filtered = [
        e
        for e in events
        if _parse_ts(e.get("ts")) >= since and _matches_subject(e, subject_type, subject_id)
    ]
    filtered.sort(key=lambda x: _parse_ts(x.get("ts")))

    by_bucket: dict[str, dict[str, Any]] = {}
    model_counts = Counter()
    deny_reasons = Counter()
    totals = {"requests": 0, "allowed": 0, "denied": 0, "tokens": 0, "usd": 0.0}

    for e in filtered:
        ts = _parse_ts(e.get("ts"))
        bkey = _bucket_key(ts, bucket_seconds)
        row = by_bucket.setdefault(
            bkey,
            {"bucket": bkey, "requests": 0, "allowed": 0, "denied": 0, "tokens": 0, "usd": 0.0, "anomaly_score": 0.0, "top_model": None},
        )
        denied = _is_denied(e)
        row["requests"] += 1
        row["allowed"] += 0 if denied else 1
        row["denied"] += 1 if denied else 0
        row["tokens"] += int(e.get("estimated_tokens") or 0)
        row["usd"] += float(e.get("estimated_cost_usd") or 0.0)
        row["anomaly_score"] += _event_anomaly_score(e)

        totals["requests"] += 1
        totals["allowed"] += 0 if denied else 1
        totals["denied"] += 1 if denied else 0
        totals["tokens"] += int(e.get("estimated_tokens") or 0)
        totals["usd"] += float(e.get("estimated_cost_usd") or 0.0)

        model = str(e.get("model") or "")
        if model:
            model_counts[model] += 1
        if denied:
            deny_reasons[str(e.get("reason") or "unknown")] += 1

    # Model-shift anomaly bonus between adjacent buckets.
    ordered = [by_bucket[k] for k in sorted(by_bucket)]
    prev_model: str | None = None
    for row in ordered:
        bucket_events = [e for e in filtered if _bucket_key(_parse_ts(e.get("ts")), bucket_seconds) == row["bucket"]]
        top = Counter(str(e.get("model") or "") for e in bucket_events if e.get("model")).most_common(1)
        row["top_model"] = top[0][0] if top else None
        if prev_model and row["top_model"] and row["top_model"] != prev_model:
            row["anomaly_score"] += 2.0
        prev_model = row["top_model"] or prev_model
        row["usd"] = round(float(row["usd"]), 6)

    return {
        "filters": {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "time_range": time_range,
            "bucket": bucket,
        },
        "overview": {
            "requests": totals["requests"],
            "allowed": totals["allowed"],
            "denied": totals["denied"],
            "tokens": totals["tokens"],
            "usd": round(float(totals["usd"]), 6),
        },
        "charts": {
            "time_series": ordered,
            "top_models": [{"model": m, "count": c} for m, c in model_counts.most_common(10)],
            "top_deny_reasons": [{"reason": r, "count": c} for r, c in deny_reasons.most_common(10)],
        },
        "drilldown": {
            "events": [_compact_event(e) for e in filtered[-200:]],
        },
        "usage_profiles": policy.get("usage_profiles", {}),
    }


def _extract_message_texts(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for message in payload.get("messages", []) if isinstance(payload.get("messages"), list) else []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    out.append(str(item["text"]))
    return out


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


def _range_seconds(time_range: str) -> int:
    return {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(time_range, 86400)


def _bucket_seconds(bucket: str) -> int:
    return {"10s": 10, "5m": 300, "hour": 3600, "day": 86400}.get(bucket, 3600)


def _bucket_key(ts: datetime, bucket_seconds: int) -> str:
    epoch = int(ts.timestamp())
    floor_epoch = (epoch // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floor_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _matches_subject(event: dict[str, Any], subject_type: str | None, subject_id: str | None) -> bool:
    if not subject_type:
        return True
    field = {
        "principal": "principal_id",
        "user": "on_behalf_of_user_id",
        "key": "key_id",
        "token": "token_id",
    }.get(subject_type)
    if not field:
        return True
    if subject_id is None:
        return bool(event.get(field))
    return str(event.get(field) or "") == subject_id


def _is_denied(event: dict[str, Any]) -> bool:
    decision = str(event.get("decision", "")).upper()
    reason = str(event.get("reason", "")).lower()
    return decision in {"BLOCKED", "DENIED"} or "denied" in reason or reason.endswith("exceeded")


def _event_anomaly_score(event: dict[str, Any]) -> float:
    score = 0.0
    if _is_denied(event):
        score += 2.0
    heur = event.get("prompt_heuristics") if isinstance(event.get("prompt_heuristics"), dict) else {}
    if heur.get("token_exfiltration_attempt"):
        score += 5.0
    if heur.get("intent_drift"):
        score += 4.0
    return score


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": event.get("ts"),
        "request_id": event.get("request_id"),
        "principal_id": event.get("principal_id"),
        "on_behalf_of_user_id": event.get("on_behalf_of_user_id"),
        "application_id": event.get("application_id"),
        "key_id": event.get("key_id"),
        "token_id": event.get("token_id"),
        "model": event.get("model"),
        "decision": event.get("decision"),
        "reason": event.get("reason"),
        "estimated_tokens": event.get("estimated_tokens"),
        "estimated_cost_usd": event.get("estimated_cost_usd"),
        "prompt_heuristics": event.get("prompt_heuristics", {}),
    }
