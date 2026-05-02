#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from modelkeyguard.governance_runtime import (
    evaluate_scanner_plugins,
    load_usage_scanner_checkpoint,
    recent_history_text,
    save_usage_scanner_checkpoint,
    scanner_should_run,
    try_load_policy,
    try_open_graph_state,
)
from modelkeyguard.reviewer_agent import ReviewStatusClient
from modelkeyguard.usage_agent import UsageAnalysisAgent

# This CLI is intentionally thin. If you want to write a real analysis agent,
# start in `modelkeyguard/usage_agent.py` and keep this file as the wrapper.


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch usage analytics for a user, principal, or key using the reusable scaffold."
    )
    parser.add_argument("--base-url", default=os.getenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"))
    parser.add_argument("--time-range", default=os.getenv("MODELKEYGUARD_ANALYTICS_TIME_RANGE", "24h"))
    parser.add_argument("--bucket", default=os.getenv("MODELKEYGUARD_ANALYTICS_BUCKET", "hour"))
    parser.add_argument("--bearer-token", default=os.getenv("MODELKEYGUARD_BEARER_TOKEN", "").strip() or None)
    parser.add_argument("--keycloak-url", default=os.getenv("KEYCLOAK_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--realm", default=os.getenv("KEYCLOAK_REALM", "modelguard"))
    parser.add_argument("--client-id", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_ID", "modelguard-usage-agent"))
    parser.add_argument("--client-secret", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", ""))
    parser.add_argument("--user", action="append", default=[], help="Subject ID in the user lane, e.g. user:alice")
    parser.add_argument("--principal", action="append", default=[], help="Subject ID in the principal lane, e.g. agent:doc-ingestor")
    parser.add_argument("--key", action="append", default=[], help="Subject ID in the key lane, e.g. key:openai:prod")
    parser.add_argument("--runtime-mode", choices=["sync", "async"], default=os.getenv("MODELKEYGUARD_RUNTIME_MODE", "sync"))
    parser.add_argument("--loop", action="store_true", help="poll review triggers and run analysis periodically")
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("MODELKEYGUARD_SCANNER_INTERVAL_SECONDS", "30")))
    parser.add_argument("--max-iterations", type=int, default=0, help="0 means unbounded when --loop is set")
    parser.add_argument("--force", action="store_true", help="run analysis even when scanner triggers are not met")
    args = parser.parse_args()

    agent = _build_agent(args)
    agent.runtime_mode = args.runtime_mode
    if args.user:
        agent.user_subjects = list(args.user)
    if args.principal:
        agent.principal_subjects = list(args.principal)
    if args.key:
        agent.key_subjects = list(args.key)
    if not args.loop:
        print(agent.render())
        return 0
    return _run_loop(agent, args)


def _run_loop(agent: UsageAnalysisAgent, args: argparse.Namespace) -> int:
    status_client = ReviewStatusClient.from_env()
    status_client = ReviewStatusClient(
        base_url=args.base_url,
        bearer_token=status_client.bearer_token,
        admin_secret=status_client.admin_secret,
        keycloak=status_client.keycloak,
        timeout_seconds=status_client.timeout_seconds,
    )
    policy = try_load_policy()
    graph_state = try_open_graph_state()
    iterations = 0

    while True:
        status: dict[str, object] = {}
        try:
            status = status_client.status()
        except Exception as exc:
            print(f"warning: scanner status query failed: {exc}", file=sys.stderr)
            status = {"should_review": False, "window": {}}

        checkpoint = load_usage_scanner_checkpoint(graph_state)
        plugins = evaluate_scanner_plugins(
            policy=policy,
            workflow_name="usage_analysis",
            status=status if isinstance(status, dict) else {},
            recent_text=recent_history_text(graph_state),
        )
        should_run, reason = scanner_should_run(
            status=status if isinstance(status, dict) else {},
            force=bool(args.force),
            plugin_results=plugins,
            usage_checkpoint=checkpoint,
        )

        if should_run:
            report = agent.run()
            payload = {
                "scanner_action": "ran",
                "runtime_mode": agent.runtime_mode,
                "trigger_reason": reason,
                "plugins": plugins,
                "report": report,
            }
            latest_request_id = str(((status.get("window") if isinstance(status, dict) else {}) or {}).get("latest_request_id") or "")
            payload["checkpoint"] = save_usage_scanner_checkpoint(graph_state, latest_request_id=latest_request_id)
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "scanner_action": "skipped",
                        "runtime_mode": agent.runtime_mode,
                        "trigger_reason": reason,
                        "plugins": plugins,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )

        iterations += 1
        if args.max_iterations > 0 and iterations >= args.max_iterations:
            break
        time.sleep(max(0.1, float(args.interval_seconds)))
    return 0


def _build_agent(args: argparse.Namespace) -> UsageAnalysisAgent:
    if args.bearer_token:
        os.environ["MODELKEYGUARD_BEARER_TOKEN"] = args.bearer_token
    os.environ["MODELKEYGUARD_GATEWAY_PUBLIC_URL"] = args.base_url
    os.environ["MODELKEYGUARD_ANALYTICS_TIME_RANGE"] = args.time_range
    os.environ["MODELKEYGUARD_ANALYTICS_BUCKET"] = args.bucket
    os.environ["KEYCLOAK_URL"] = args.keycloak_url
    os.environ["KEYCLOAK_REALM"] = args.realm
    os.environ["MODELKEYGUARD_OIDC_USAGE_CLIENT_ID"] = args.client_id
    os.environ["MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET"] = args.client_secret
    os.environ["MODELKEYGUARD_RUNTIME_MODE"] = args.runtime_mode
    agent = UsageAnalysisAgent.from_env()
    if any((args.user, args.principal, args.key)):
        agent.user_subjects = list(args.user)
        agent.principal_subjects = list(args.principal)
        agent.key_subjects = list(args.key)
    return agent


if __name__ == "__main__":
    raise SystemExit(main())
