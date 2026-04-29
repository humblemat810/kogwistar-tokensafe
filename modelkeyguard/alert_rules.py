from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from collections import defaultdict, Counter
import json
import time

from .graph_state import GraphStateStore

AlertCondition = Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], bool]
AlertAction = Callable[[dict[str, Any], GraphStateStore], None]
ReviewCallback = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Alert:
    rule_id: str
    severity: str
    subject_type: str
    subject_id: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertRule:
    """Installable alert rule.

    A rule is a condition callback plus zero or more action callbacks. The default
    action persists ALERT_RAISED as graph state. Callers can inject webhook,
    PagerDuty, email, Slack, or Kogwistar approval callbacks later without
    changing the detection engine.
    """

    rule_id: str
    description: str
    severity: str
    condition: AlertCondition
    build_alert: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], Alert]
    actions: tuple[AlertAction, ...] = ()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


SENSITIVE_FIELD_HINTS = ("secret", "api_key", "apikey", "authorization", "access_token", "refresh_token", "provider_key", "bearer", "password", "ciphertext")

def _looks_sensitive_key(key: str) -> bool:
    return any(hint in key.lower() for hint in SENSITIVE_FIELD_HINTS)

def _looks_sensitive_value(value: str) -> bool:
    low = value.lower()
    return value.startswith("sk-") or value.startswith("kgw_sk_") or low.startswith("bearer ") or "api_key=" in low

def redact_sensitive(value: Any) -> Any:
    """Return a copy safe for graph alerts and LLM review prompts."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if _looks_sensitive_key(str(k)) else redact_sensitive(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(v) for v in value)
    if isinstance(value, str) and _looks_sensitive_value(value):
        return "<redacted>"
    return value


def default_persist_action(alert: dict[str, Any], graph_state: GraphStateStore) -> None:
    safe_subject = str(alert.get("subject_id", "unknown")).replace(":", "_")
    node_id = f"alert:{alert.get('rule_id')}:{safe_subject}:{int(time.time() * 1000)}"
    graph_state.put_node(node_id, "alert", alert)
    graph_state.append_event("ALERT_RAISED", node_id, alert)


class AlertEngine:
    def __init__(self, graph_state: GraphStateStore, rules: Iterable[AlertRule] | None = None, default_actions: Iterable[AlertAction] | None = None) -> None:
        self.graph_state = graph_state
        # Preserve explicit empty rule sets while keeping built-in defaults when
        # caller passes None.
        self.rules = list(rules) if rules is not None else default_rules()
        # Preserve explicit empty actions (evaluate-only mode) while keeping
        # default persistence behavior when caller passes None.
        self.default_actions = tuple(default_actions) if default_actions is not None else (default_persist_action,)

    def evaluate(self, events: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for rule in self.rules:
            for context in build_subject_contexts(events, policy):
                if rule.condition(context, events, policy):
                    alert = rule.build_alert(context, events, policy)
                    record = {
                        "event_type": "ALERT_RAISED",
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "rule_id": alert.rule_id,
                        "severity": alert.severity,
                        "subject_type": alert.subject_type,
                        "subject_id": alert.subject_id,
                        "reason": alert.reason,
                        "payload": alert.payload,
                    }
                    record = redact_sensitive(record)
                    alerts.append(record)
                    for action in self.default_actions + rule.actions:
                        action(record, self.graph_state)
        return alerts


def build_subject_contexts(events: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for subject_type, field_name in (("principal", "principal_id"), ("user", "on_behalf_of_user_id"), ("application", "application_id"), ("key", "key_id")):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in events:
            sid = e.get(field_name)
            if sid:
                grouped[str(sid)].append(e)
        for sid, rows in grouped.items():
            contexts.append({"subject_type": subject_type, "subject_id": sid, "events": rows})
    # Some deployments call application principal_id, so add app contexts by principal prefix too.
    grouped_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        pid = str(e.get("principal_id", ""))
        if pid.startswith("app:") or pid.startswith("service:"):
            grouped_app[pid].append(e)
    for sid, rows in grouped_app.items():
        contexts.append({"subject_type": "application", "subject_id": sid, "events": rows})
    return contexts


def _count(rows: list[dict[str, Any]], **matches: str) -> int:
    total = 0
    for r in rows:
        if all(str(r.get(k)) == v for k, v in matches.items()):
            total += 1
    return total


def _denied_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if str(r.get("decision", "")).upper() in {"BLOCKED", "DENIED"} or "denied" in str(r.get("reason", "")).lower()]


def default_rules() -> list[AlertRule]:
    def high_denial_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
        rows = ctx["events"]
        threshold = int(policy.get("alert_rules", {}).get("high_denial_count", 3))
        return len(_denied_rows(rows)) >= threshold

    def high_denial_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        denied = _denied_rows(ctx["events"])
        reasons = Counter(str(r.get("reason")) for r in denied).most_common(5)
        return Alert("high_denial_rate", "medium", ctx["subject_type"], ctx["subject_id"], "many denied/blocked model access attempts", {"denied_count": len(denied), "top_reasons": reasons})

    def prompt_mismatch_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> bool:
        return _count(ctx["events"], reason="system_prompt_signature_mismatch") > 0

    def prompt_mismatch_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = [r for r in ctx["events"] if r.get("reason") == "system_prompt_signature_mismatch"]
        return Alert("system_prompt_signature_mismatch", "high", ctx["subject_type"], ctx["subject_id"], "system prompt hash differs from assigned usage profile", {"count": len(rows), "hashes": sorted({r.get("system_prompt_hash") for r in rows if r.get("system_prompt_hash")})})

    def quota_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> bool:
        reasons = {"principal_capacity_exceeded", "user_quota_exceeded", "key_quota_exceeded", "token_quota_exceeded"}
        return any(r.get("reason") in reasons for r in ctx["events"])

    def quota_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = [r for r in ctx["events"] if r.get("reason") in {"principal_capacity_exceeded", "user_quota_exceeded", "key_quota_exceeded", "token_quota_exceeded"}]
        reasons = Counter(str(r.get("reason")) for r in rows).most_common(5)
        return Alert("quota_exhausted", "medium", ctx["subject_type"], ctx["subject_id"], "quota/capacity exhausted", {"count": len(rows), "top_reasons": reasons})

    def unexpected_model_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
        if ctx["subject_type"] not in {"principal", "application"}:
            return False
        profile = policy.get("usage_profiles", {}).get(ctx["subject_id"], {})
        expected = set(profile.get("models", []))
        if not expected:
            return False
        return any(r.get("model") and r.get("model") not in expected for r in ctx["events"])

    def unexpected_model_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], policy: dict[str, Any]) -> Alert:
        expected = set(policy.get("usage_profiles", {}).get(ctx["subject_id"], {}).get("models", []))
        bad = sorted({r.get("model") for r in ctx["events"] if r.get("model") and r.get("model") not in expected})
        return Alert("usage_profile_model_violation", "high", ctx["subject_type"], ctx["subject_id"], "model usage violates assigned usage profile", {"unexpected_models": bad, "expected_models": sorted(expected)})

    def token_exfiltration_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> bool:
        return any(bool((r.get("prompt_heuristics") or {}).get("token_exfiltration_attempt")) for r in ctx["events"])

    def token_exfiltration_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = [r for r in ctx["events"] if bool((r.get("prompt_heuristics") or {}).get("token_exfiltration_attempt"))]
        signals = Counter(
            s
            for r in rows
            for s in ((r.get("prompt_heuristics") or {}).get("token_exfiltration_signals") or [])
            if isinstance(s, str)
        ).most_common(10)
        return Alert(
            "token_exfiltration_attempt",
            "high",
            ctx["subject_type"],
            ctx["subject_id"],
            "potential token/secret extraction attempt observed",
            {"count": len(rows), "signals": signals},
        )

    def intent_drift_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> bool:
        return any(bool((r.get("prompt_heuristics") or {}).get("intent_drift")) for r in ctx["events"])

    def intent_drift_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = [r for r in ctx["events"] if bool((r.get("prompt_heuristics") or {}).get("intent_drift"))]
        disallowed = Counter(
            i
            for r in rows
            for i in ((r.get("prompt_heuristics") or {}).get("disallowed_intents") or [])
            if isinstance(i, str)
        ).most_common(10)
        return Alert(
            "intent_drift",
            "high",
            ctx["subject_type"],
            ctx["subject_id"],
            "usage intent drift from configured profile detected",
            {"count": len(rows), "disallowed_intents": disallowed},
        )

    def sudden_model_shift_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
        rows = [r for r in ctx["events"] if r.get("model")]
        threshold = int(policy.get("alert_rules", {}).get("model_shift_min_events", 8))
        if len(rows) < threshold:
            return False
        mid = len(rows) // 2
        first = Counter(str(r.get("model")) for r in rows[:mid]).most_common(1)
        second = Counter(str(r.get("model")) for r in rows[mid:]).most_common(1)
        return bool(first and second and first[0][0] != second[0][0])

    def sudden_model_shift_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = [r for r in ctx["events"] if r.get("model")]
        mid = len(rows) // 2
        first = Counter(str(r.get("model")) for r in rows[:mid]).most_common(1)
        second = Counter(str(r.get("model")) for r in rows[mid:]).most_common(1)
        return Alert(
            "sudden_model_shift",
            "medium",
            ctx["subject_type"],
            ctx["subject_id"],
            "subject model distribution shifted abruptly",
            {"from_model": first[0][0] if first else None, "to_model": second[0][0] if second else None, "events": len(rows)},
        )

    def deny_spike_condition(ctx: dict[str, Any], _events: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
        rows = ctx["events"][-10:]
        min_events = int(policy.get("alert_rules", {}).get("deny_spike_min_events", 5))
        if len(rows) < min_events:
            return False
        ratio = len(_denied_rows(rows)) / max(1, len(rows))
        threshold = float(policy.get("alert_rules", {}).get("deny_spike_ratio", 0.6))
        return ratio >= threshold

    def deny_spike_alert(ctx: dict[str, Any], _events: list[dict[str, Any]], _policy: dict[str, Any]) -> Alert:
        rows = ctx["events"][-10:]
        denied = _denied_rows(rows)
        ratio = round(len(denied) / max(1, len(rows)), 4)
        reasons = Counter(str(r.get("reason")) for r in denied).most_common(5)
        return Alert(
            "deny_spike",
            "medium",
            ctx["subject_type"],
            ctx["subject_id"],
            "recent deny ratio spiked",
            {"window": len(rows), "denied": len(denied), "ratio": ratio, "top_reasons": reasons},
        )

    return [
        AlertRule("high_denial_rate", "Raise when denied/blocked requests exceed threshold", "medium", high_denial_condition, high_denial_alert),
        AlertRule("deny_spike", "Raise when recent deny ratio spikes", "medium", deny_spike_condition, deny_spike_alert),
        AlertRule("system_prompt_signature_mismatch", "Raise when system prompt hash mismatches profile", "high", prompt_mismatch_condition, prompt_mismatch_alert),
        AlertRule("quota_exhausted", "Raise on principal/user/key/token quota exhaustion", "medium", quota_condition, quota_alert),
        AlertRule("usage_profile_model_violation", "Raise on unexpected model for profile", "high", unexpected_model_condition, unexpected_model_alert),
        AlertRule("token_exfiltration_attempt", "Raise on token/secret extraction attempt patterns", "high", token_exfiltration_condition, token_exfiltration_alert),
        AlertRule("intent_drift", "Raise when observed intent drifts from configured profile intent", "high", intent_drift_condition, intent_drift_alert),
        AlertRule("sudden_model_shift", "Raise when model usage shifts abruptly", "medium", sudden_model_shift_condition, sudden_model_shift_alert),
    ]


class LLMUsageReviewer:
    """Per-subject LLM review coordinator.

    In production, pass review_callback to call an LLM, policy engine, webhook, or
    approval system. The default callback is deterministic and safe for tests: it
    summarizes hard-rule evidence without making network calls.
    """

    def __init__(self, graph_state: GraphStateStore, review_callback: ReviewCallback | None = None) -> None:
        self.graph_state = graph_state
        self.review_callback = review_callback or self.default_review_callback

    def review(self, events: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for ctx in build_subject_contexts(events, policy):
            if ctx["subject_type"] not in {"principal", "user", "application"}:
                continue
            prompt = self._build_review_payload(ctx, policy)
            result = self.review_callback(prompt)
            review = {
                "event_type": "LLM_USAGE_REVIEWED",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "subject_type": ctx["subject_type"],
                "subject_id": ctx["subject_id"],
                "result": result,
            }
            reviews.append(review)
            node_id = f"review:{ctx['subject_type']}:{str(ctx['subject_id']).replace(':','_')}:{int(time.time() * 1000)}"
            self.graph_state.put_node(node_id, "llm_usage_review", review)
            self.graph_state.append_event("LLM_USAGE_REVIEWED", node_id, review)
        return reviews

    def _build_review_payload(self, ctx: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        rows = ctx["events"]
        key_usage_contracts = self._build_key_usage_contracts(rows, policy)
        return {
            "task": "Decide whether model-key usage matches the pre-assigned usage profile. Return risk, reasons, and recommended action.",
            "subject_type": ctx["subject_type"],
            "subject_id": ctx["subject_id"],
            "usage_profile": policy.get("usage_profiles", {}).get(ctx["subject_id"], {}),
            "key_usage_contracts": key_usage_contracts,
            "event_count": len(rows),
            "sample": redact_sensitive(rows[:20]),
            "aggregates": {
                "decisions": Counter(str(r.get("decision")) for r in rows).most_common(),
                "reasons": Counter(str(r.get("reason")) for r in rows).most_common(10),
                "models": Counter(str(r.get("model")) for r in rows if r.get("model")).most_common(10),
                "intents": Counter(
                    i
                    for r in rows
                    for i in ((r.get("prompt_heuristics") or {}).get("observed_intents") or [])
                    if isinstance(i, str)
                ).most_common(10),
            },
        }

    def _build_key_usage_contracts(self, rows: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
        by_policy = {
            str(item.get("id", "")): item
            for item in policy.get("model_keys", [])
            if isinstance(item, dict) and item.get("id")
        }
        key_ids = sorted({str(r.get("key_id")) for r in rows if r.get("key_id")})
        contracts: dict[str, dict[str, Any]] = {}
        for key_id in key_ids:
            graph_payload: dict[str, Any] | None = None
            node = self.graph_state.nodes.get(key_id) if self.graph_state else None
            if node and node.kind == "model_key":
                graph_payload = node.payload
            policy_payload = by_policy.get(key_id, {})
            models = graph_payload.get("models") if graph_payload else policy_payload.get("models", [])
            contracts[key_id] = {
                "provider": str((graph_payload or policy_payload).get("provider", "")),
                "display_name": str((graph_payload or policy_payload).get("display_name", key_id)),
                "models": [str(m) for m in models] if isinstance(models, list) else [],
                "intended_use": str((graph_payload or policy_payload).get("intended_use", "")),
            }
        return contracts

    @staticmethod
    def default_review_callback(payload: dict[str, Any]) -> dict[str, Any]:
        reasons = dict(payload.get("aggregates", {}).get("reasons", []))
        intents = dict(payload.get("aggregates", {}).get("intents", []))
        risk = "low"
        rec = "continue_monitoring"
        findings: list[str] = []
        labels: list[str] = []
        if reasons.get("system_prompt_signature_mismatch", 0):
            risk = "high"; rec = "investigate_and_consider_revocation"; findings.append("system prompt signature mismatch observed"); labels.append("prompt-mismatch")
        if reasons.get("token_exfiltration_attempt", 0):
            risk = "high"; rec = "investigate_and_consider_revocation"; findings.append("token extraction signals observed"); labels.append("token-exfil")
        if reasons.get("intent_drift", 0) or intents.get("coding", 0):
            if risk != "high":
                risk = "medium"; rec = "review_profile_and_intent_scope"
            findings.append("intent drift observed"); labels.append("intent-drift")
        if reasons.get("principal_capacity_exceeded", 0) or reasons.get("user_quota_exceeded", 0) or reasons.get("token_quota_exceeded", 0):
            if risk != "high":
                risk = "medium"; rec = "review_quota_or_possible_token_leak"
            findings.append("quota or capacity exceeded")
            labels.append("quota")
        return {"risk": risk, "recommended_action": rec, "findings": findings, "labels": sorted(set(labels)), "reviewer": "deterministic-local-callback"}
