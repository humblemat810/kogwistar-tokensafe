from __future__ import annotations

"""Durable uncertain-request reconciliation queue.

This worker is deliberately report-only: uncertain requests are never retried
automatically.  Operators/provider-specific handlers may later call
``mark_reconciled`` with an authoritative outcome.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .graph_state import GraphStateStore

RESERVATION_NAMESPACE = "modelkeyguard_reservation"
RESERVATION_KEY = "active"
ProviderReconciler = Callable[[dict[str, Any]], dict[str, Any]]
_PROVIDER_RECONCILIERS: dict[str, ProviderReconciler] = {}


def register_provider_reconciler(provider: str, resolver: ProviderReconciler) -> None:
    """Register a read-only provider status/usage resolver.

    Resolver must query provider state; it must never submit a second model
    request.  Registration is process-local, while outcomes are persisted in
    the graph projection by :func:`reconcile_with_provider`.
    """
    name = str(provider or "").strip().lower()
    if not name:
        raise ValueError("provider_required")
    _PROVIDER_RECONCILIERS[name] = resolver


def http_provider_reconciler(url_template: str, *, token: str | None = None, timeout_seconds: float = 10.0) -> ProviderReconciler:
    """Build a read-only HTTP provider reconciliation resolver.

    ``url_template`` must contain ``{request_id}``; endpoint response must be
    JSON with ``outcome`` (settled/released/uncertain) and optional usage.
    No POST, retry, or resend operation is performed by this client.
    """
    if "{request_id}" not in url_template:
        raise ValueError("reconciliation_url_requires_request_id_placeholder")

    def resolve(record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(record.get("request_id") or "")
        url = url_template.replace("{request_id}", urllib.parse.quote(request_id, safe=""))
        headers = {"accept": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise RuntimeError(f"provider_reconciliation_unavailable:{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("provider_reconciliation_response_not_object")
        if payload.get("retry") or payload.get("resend"):
            raise ValueError("automatic_retry_forbidden")
        return {
            "outcome": payload.get("outcome") or payload.get("state"),
            "actual_cost_usd": payload.get("actual_cost_usd", payload.get("cost_usd")),
            "actual_tokens": payload.get("actual_tokens", payload.get("tokens")),
        }

    return resolve


def register_env_provider_reconcilers() -> None:
    """Register configured read-only endpoints for all supported providers."""
    for provider in ("openai", "azure_openai", "gemini", "ollama"):
        env_name = f"MODELKEYGUARD_{provider.upper()}_RECONCILE_URL"
        template = os.getenv(env_name, "").strip()
        if not template:
            continue
        token = os.getenv(f"MODELKEYGUARD_{provider.upper()}_RECONCILE_TOKEN") or None
        register_provider_reconciler(provider, http_provider_reconciler(template, token=token))


def _replace_reservation(graph_state: GraphStateStore, payload: dict[str, Any], row: dict[str, Any] | None) -> bool:
    cas = getattr(graph_state, "compare_and_swap_named_projections", None)
    if callable(cas):
        update: dict[str, Any] = {"namespace": RESERVATION_NAMESPACE, "key": RESERVATION_KEY, "payload": payload}
        if isinstance(row, dict) and "payload" in row:
            expected_a = int(row.get("last_authoritative_seq", 0))
            expected_m = int(row.get("last_materialized_seq", 0))
            update.update(
                expected_last_authoritative_seq=expected_a,
                expected_last_materialized_seq=expected_m,
                last_authoritative_seq=expected_a + 1,
                last_materialized_seq=expected_m + 1,
            )
        else:
            current_payload = row if isinstance(row, dict) else None
            update["expected_payload_hash"] = current_payload
        attempts = max(1, int(os.getenv("MODELKEYGUARD_RESERVATION_CAS_RETRIES", "3")))
        for _attempt in range(attempts):
            try:
                if bool(cas([update])):
                    return True
            except Exception:
                return False
            if _attempt + 1 < attempts:
                row = graph_state.get_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY)
                if isinstance(row, dict) and "payload" in row:
                    update["expected_last_authoritative_seq"] = int(row.get("last_authoritative_seq", 0))
                    update["expected_last_materialized_seq"] = int(row.get("last_materialized_seq", 0))
                    update["last_authoritative_seq"] = update["expected_last_authoritative_seq"] + 1
                    update["last_materialized_seq"] = update["expected_last_materialized_seq"] + 1
        return False
    graph_state.replace_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY, payload)
    return True


def uncertain_requests(graph_state: GraphStateStore) -> list[dict[str, Any]]:
    row = graph_state.get_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY)
    payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
    reservations = payload.get("reservations", {}) if isinstance(payload, dict) else {}
    return [
        {"request_id": str(request_id), **dict(value)}
        for request_id, value in reservations.items()
        if isinstance(value, dict) and value.get("state") == "uncertain" and not value.get("reconciled_at")
    ]


def expire_stale_reservations(graph_state: GraphStateStore, *, now: float | None = None) -> dict[str, int]:
    """Classify abandoned forwarding as uncertain; never retry it."""
    now = float(now or time.time())
    row = graph_state.get_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY)
    payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
    if not isinstance(payload, dict):
        return {"uncertain": 0, "released": 0}
    reservations = dict(payload.get("reservations") or {})
    changed = {"uncertain": 0, "released": 0}
    for request_id, raw in list(reservations.items()):
        value = dict(raw) if isinstance(raw, dict) else {}
        if float(value.get("expires_at", now + 1)) >= now:
            continue
        state = str(value.get("state") or "")
        if state in {"forwarding", "streaming"}:
            value.update(state="uncertain", uncertain_reason="stale_upstream_after_worker_loss", updated_at=now)
            changed["uncertain"] += 1
        elif state == "reserved":
            value.update(state="released", release_reason="reservation_ttl_expired", updated_at=now)
            changed["released"] += 1
        else:
            continue
        reservations[request_id] = value
    if any(changed.values()):
        _replace_reservation(graph_state, {**payload, "reservations": reservations, "updated_at": now}, row)
    return changed


def mark_reconciled(graph_state: GraphStateStore, request_id: str, *, outcome: str, actual_cost_usd: float | None = None, actual_tokens: int | None = None) -> bool:
    row = graph_state.get_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY)
    payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
    if not isinstance(payload, dict):
        return False
    reservations = dict(payload.get("reservations") or {})
    value = dict(reservations.get(request_id) or {})
    if outcome not in {"settled", "released", "uncertain"}:
        raise ValueError("invalid_reconciliation_outcome")
    if not value or value.get("state") != "uncertain":
        return False
    if outcome == "settled":
        # Reuse the gateway's multi-projection settlement CAS.  Directly
        # add_quota_usage here could debit one lane, crash, then leave the
        # reservation uncertain (or debit again on retry).
        from .core import AccessDecision, ModelKeyGuard

        settled_cost = float(actual_cost_usd if actual_cost_usd is not None else value.get("estimated_cost_usd", 0.0))
        settled_tokens = int(actual_tokens if actual_tokens is not None else value.get("estimated_tokens", 0))
        decision = AccessDecision(
            allowed=True,
            requires_approval=False,
            http_status=200,
            reason="reconciled",
            acl_reason="reconciliation",
            key_id=str(value.get("key_id") or ""),
            principal_id=str(value.get("principal_id") or ""),
            namespace=str(value.get("namespace") or ""),
            request_id=str(request_id),
            token_id=str(value.get("token_id") or ""),
            on_behalf_of_user_id=value.get("on_behalf_of_user_id"),
            remaining={},
            secret_ref=None,
        )
        if not ModelKeyGuard(graph_state=graph_state).settle_reservation(
            decision,
            float(value.get("estimated_cost_usd", settled_cost)),
            settled_cost,
            settled_tokens,
            usage_authoritative=actual_cost_usd is not None and actual_tokens is not None,
        ):
            return False
        # Settlement CAS may have advanced the reservation projection; reload
        # before adding reconciliation metadata, preserving its new version.
        row = graph_state.get_named_projection(RESERVATION_NAMESPACE, RESERVATION_KEY)
        latest_payload = row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else row
        latest_reservations = dict(latest_payload.get("reservations") or {}) if isinstance(latest_payload, dict) else {}
        value = dict(latest_reservations.get(request_id) or value)
        value.update({"state": "settled", "reconciled_at": time.time(), "reconciliation_source": "operator"})
        value["usage_estimated"] = actual_cost_usd is None or actual_tokens is None
        value["actual_cost_usd"] = settled_cost
        value["actual_tokens"] = settled_tokens
        latest_reservations[request_id] = value
        return _replace_reservation(graph_state, {**(latest_payload if isinstance(latest_payload, dict) else {}), "reservations": latest_reservations, "updated_at": time.time()}, row)

    value.update({"state": outcome, "reconciled_at": time.time(), "reconciliation_source": "operator"})
    if actual_cost_usd is not None:
        value["actual_cost_usd"] = float(actual_cost_usd)
    if actual_tokens is not None:
        value["actual_tokens"] = int(actual_tokens)
    reservations[request_id] = value
    return _replace_reservation(graph_state, {**payload, "reservations": reservations, "updated_at": time.time()}, row)


def reconcile_with_provider(graph_state: GraphStateStore, request_id: str, *, provider: str) -> dict[str, Any]:
    """Resolve one uncertain request through a registered read-only adapter.

    Adapter response must contain ``outcome`` and may contain actual usage.
    Any ``retry``/``resend`` signal is rejected, enforcing no automatic retry.
    """
    rows = {row["request_id"]: row for row in uncertain_requests(graph_state)}
    record = rows.get(str(request_id))
    if not record:
        raise KeyError(f"uncertain_request_not_found:{request_id}")
    resolver = _PROVIDER_RECONCILIERS.get(str(provider or "").strip().lower())
    if resolver is None:
        raise LookupError(f"provider_reconciler_not_registered:{provider}")
    result = resolver(dict(record))
    if not isinstance(result, dict) or result.get("retry") or result.get("resend"):
        raise ValueError("automatic_retry_forbidden")
    outcome = str(result.get("outcome") or "").strip().lower()
    if outcome not in {"settled", "released", "uncertain"}:
        raise ValueError("provider_reconciler_missing_valid_outcome")
    ok = mark_reconciled(
        graph_state,
        str(request_id),
        outcome=outcome,
        actual_cost_usd=result.get("actual_cost_usd"),
        actual_tokens=result.get("actual_tokens"),
    )
    if ok:
        graph_state.append_access_conversation_event(
            str(request_id),
            "QUOTA_RESERVATION_RECONCILED",
            {"provider": str(provider), "outcome": outcome, "source": "provider_reconciler"},
        )
    return {"request_id": str(request_id), "provider": str(provider), "outcome": outcome, "updated": ok}


def reconcile_once(graph_path: str | Path, app_key: str | None = None, out_path: str | Path | None = None) -> dict[str, Any]:
    graph = GraphStateStore(graph_path, app_key=app_key)
    register_env_provider_reconcilers()
    transitions = expire_stale_reservations(graph)
    rows = uncertain_requests(graph)
    result = {"ts": time.time(), "uncertain_count": len(rows), "requests": rows, "transitions": transitions, "automatic_retry": False}
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List uncertain ModelKeyGuard requests; never retries automatically.")
    parser.add_argument("--graph", default=os.getenv("MODELKEYGUARD_GRAPH_PATH", "out/modelkeyguard_graph.jsonl"))
    parser.add_argument("--out", default=os.getenv("MODELKEYGUARD_RECONCILIATION_OUT", "out/reconciliation.jsonl"))
    args = parser.parse_args(argv)
    print(json.dumps(reconcile_once(args.graph, out_path=args.out), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
