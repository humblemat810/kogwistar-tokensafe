#!/usr/bin/env python3
"""Run the governance usage reviewer against the ModelKeyGuard gateway.

This CLI has two independent auth paths:

1) Review-status path (`/admin/review/status.json`):
   - Uses `MODELKEYGUARD_BEARER_TOKEN`, or
   - `MODELKEYGUARD_ADMIN_API_SECRET`, or
   - Keycloak service-account minting via `MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET`.

2) Reviewer model-call path (`/api/chat`):
   - Uses `REVIEWER_SAFE_TOKEN` (preferred), then token fallbacks.

Output shape is stable for automation:
- `review_status`
- `review_result`
- optional `reviewer_loop_health` and `checkpoint`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from modelkeyguard.governance_runtime import (
    evaluate_scanner_plugins,
    load_scanner_loop_health,
    load_scanner_runtime_config,
    recent_history_text,
    run_usage_reviewer_runtime,
    save_scanner_loop_health,
    scanner_state_transition,
    scanner_should_run,
    try_load_policy,
    try_open_graph_state,
)
from modelkeyguard.reviewer_agent import (
    DEFAULT_REVIEW_SYSTEM_PROMPT,
    ReviewStatusClient,
    run_langchain_reviewer,  # kept as explicit dependency anchor for tutorial/tests
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _reviewer_safe_token() -> str:
    return _env("REVIEWER_SAFE_TOKEN", "")


def _review_model_token_candidates() -> list[tuple[str, str]]:
    def _candidate(name: str) -> tuple[str, str]:
        return name, _env(name, "")

    ordered: list[tuple[str, str]] = [
        _candidate("REVIEWER_SAFE_TOKEN"),
        _candidate("KGW_TOKEN"),
        _candidate("SAFE_TOKEN"),
        _candidate("OPENAI_API_KEY"),
        _candidate("MODELKEYGUARD_BEARER_TOKEN"),
    ]
    seen: set[str] = set()
    resolved: list[tuple[str, str]] = []
    for source, token in ordered:
        if not token or token in seen:
            continue
        resolved.append((source, token))
        seen.add(token)
    return resolved


def _key_id() -> str:
    return _env("MODELKEYGUARD_KEY_ID", "")


def main() -> int:
    parser = argparse.ArgumentParser(description="LangChain-based usage reviewer against the ModelKeyGuard gateway")
    parser.add_argument("--base-url", default=_env("KGW_BASE_URL", _env("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789")))
    parser.add_argument("--model", default=_env("KGW_OLLAMA_MODEL", "gemma4:e2b"))
    parser.add_argument("--force", action="store_true", help="run the reviewer even if the trigger thresholds have not fired yet")
    parser.add_argument("--advance-checkpoint", action="store_true", help="advance the named review checkpoint after a successful run")
    parser.add_argument("--reviewed-by", default=_env("MODELKEYGUARD_REVIEWED_BY", "reviewer-agent"))
    parser.add_argument("--system-prompt", default=DEFAULT_REVIEW_SYSTEM_PROMPT)
    parser.add_argument("--runtime-mode", choices=["sync", "async"], default=_env("MODELKEYGUARD_RUNTIME_MODE", "sync"))
    parser.add_argument("--loop", action="store_true", help="poll review status and run reviewer periodically")
    parser.add_argument("--interval-seconds", type=float, default=float(_env("MODELKEYGUARD_SCANNER_INTERVAL_SECONDS", "30")))
    parser.add_argument("--max-iterations", type=int, default=0, help="0 means unbounded when --loop is set")
    parser.add_argument("--scanner-backoff-initial-seconds", type=float, default=None)
    parser.add_argument("--scanner-backoff-max-seconds", type=float, default=None)
    parser.add_argument("--scanner-breaker-enabled", action="store_true", help="enable circuit breaker for terminal scanner errors")
    parser.add_argument("--scanner-breaker-max-failures", type=int, default=None)
    parser.add_argument("--scanner-error-family-policy-json", default=None, help='JSON map, e.g. {"quota_limit":"terminal"}')
    args = parser.parse_args()
    _apply_scanner_runtime_overrides(args)

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
    runtime_cfg = load_scanner_runtime_config(policy=policy, workflow_name="usage_reviewer")
    health = load_scanner_loop_health(graph_state, workflow_name="usage_reviewer")
    iterations = 0
    while True:
        status = status_client.status()
        print("review_status:")
        print(json.dumps(status, indent=2, sort_keys=True))

        plugins = evaluate_scanner_plugins(
            policy=policy,
            workflow_name="usage_reviewer",
            status=status,
            recent_text=recent_history_text(graph_state),
        )
        should_run, _reason = scanner_should_run(
            status=status,
            force=bool(args.force),
            plugin_results=plugins,
            usage_checkpoint=None,
        )
        if not should_run:
            health = save_scanner_loop_health(
                graph_state,
                workflow_name="usage_reviewer",
                state=str(health.get("state") or "run"),
                last_error_family=str(health.get("last_error_family") or ""),
                last_error_message=str(health.get("last_error_message") or ""),
                consecutive_failures=int(health.get("consecutive_failures") or 0),
                next_retry_at=str(health.get("next_retry_at") or ""),
                breaker_tripped=bool(health.get("breaker_tripped") or False),
                last_success_ts=str(health.get("last_success_ts") or ""),
            )
            print(
                json.dumps(
                    {
                        "reviewer_action": "skipped",
                        "reason": "thresholds_not_met",
                        "loop_health": health,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            try:
                result = _run_reviewer_once(args, status=status)
                transition = scanner_state_transition(
                    workflow_name="usage_reviewer",
                    config=runtime_cfg,
                    health=health,
                    run_error=None,
                )
                health = save_scanner_loop_health(
                    graph_state,
                    workflow_name="usage_reviewer",
                    state=transition["state"],
                    last_error_family=transition["last_error_family"],
                    last_error_message=transition["last_error_message"],
                    consecutive_failures=int(transition["consecutive_failures"]),
                    next_retry_at=transition["next_retry_at"],
                    breaker_tripped=bool(transition["breaker_tripped"]),
                    last_success_ts=str(transition["last_success_ts"]),
                )
                print("review_result:")
                print(json.dumps(result, indent=2, sort_keys=True))
                if not str(result.get("text") or "").strip():
                    print(
                        "warning: reviewer model call succeeded but returned empty text; "
                        "check model/key routing or tighten the review prompt.",
                        file=sys.stderr,
                    )
                print("reviewer_loop_health:")
                print(json.dumps(health, indent=2, sort_keys=True))

                if args.advance_checkpoint:
                    try:
                        checkpoint = status_client.checkpoint(
                            {
                                "reviewed_by": args.reviewed_by,
                                "review_summary": result.get("text", ""),
                                "status": status,
                            }
                        )
                        print("checkpoint:")
                        print(json.dumps(checkpoint, indent=2, sort_keys=True))
                    except Exception as exc:
                        print(f"warning: review checkpoint not advanced: {exc}", file=sys.stderr)
            except Exception as exc:
                transition = scanner_state_transition(
                    workflow_name="usage_reviewer",
                    config=runtime_cfg,
                    health=health,
                    run_error=exc,
                )
                health = save_scanner_loop_health(
                    graph_state,
                    workflow_name="usage_reviewer",
                    state=transition["state"],
                    last_error_family=transition["last_error_family"],
                    last_error_message=transition["last_error_message"],
                    consecutive_failures=int(transition["consecutive_failures"]),
                    next_retry_at=transition["next_retry_at"],
                    breaker_tripped=bool(transition["breaker_tripped"]),
                    last_success_ts=str(transition["last_success_ts"]),
                )
                print(
                    json.dumps(
                        {
                            "reviewer_action": "error",
                            "error": str(exc),
                            "loop_health": health,
                            "error_family_policy": transition["error_family_policy"],
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                if bool(transition["terminal_stop"]):
                    return 2
                if not args.loop:
                    return 1
                time.sleep(float(transition["retry_after_seconds"]))
                iterations += 1
                if args.max_iterations > 0 and iterations >= args.max_iterations:
                    break
                continue

        iterations += 1
        if not args.loop:
            break
        if args.max_iterations > 0 and iterations >= args.max_iterations:
            break
        time.sleep(max(0.1, float(args.interval_seconds)))

    return 0


def _run_reviewer_once(args: argparse.Namespace, *, status: dict[str, object]) -> dict[str, object]:
    candidates = _review_model_token_candidates()
    if not candidates:
        print("error: missing REVIEWER_SAFE_TOKEN for the Ollama-shaped review call", file=sys.stderr)
        print(
            "hint: export REVIEWER_SAFE_TOKEN (recommended), or KGW_TOKEN/SAFE_TOKEN/OPENAI_API_KEY/MODELKEYGUARD_BEARER_TOKEN",
            file=sys.stderr,
        )
        raise SystemExit(2)

    result: dict[str, object] | None = None
    last_error: Exception | None = None
    key_id = _key_id()
    for idx, (token_source, token) in enumerate(candidates):
        if idx > 0:
            print(f"warning: retrying reviewer model call with token from {token_source}", file=sys.stderr)
        try:
            result = run_usage_reviewer_runtime(
                status=status,
                base_url=args.base_url,
                safe_token=token,
                model=args.model,
                system_prompt=args.system_prompt,
                key_id=key_id,
                runtime_mode=args.runtime_mode,
            )
            if not isinstance(result, dict) or not result:
                raise RuntimeError("usage_reviewer runtime contract violation: empty review_result payload")
            break
        except RuntimeError as exc:
            last_error = exc
            exc_text = str(exc)
            if "model_key_ambiguous" in exc_text and not key_id.strip():
                raise RuntimeError(
                    "review model call failed: model_key_ambiguous. "
                    "Set MODELKEYGUARD_KEY_ID to a specific provider key "
                    "(for example key:fwd-ollama:gemma4-e2b:3) for this reviewer run."
                ) from exc
            if "HTTP 401" not in str(exc):
                raise
            continue
    if result is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("reviewer model call failed before producing a result")
    return result


def _apply_scanner_runtime_overrides(args: argparse.Namespace) -> None:
    if args.scanner_backoff_initial_seconds is not None:
        os.environ["MODELKEYGUARD_SCANNER_BACKOFF_INITIAL_SECONDS"] = str(args.scanner_backoff_initial_seconds)
    if args.scanner_backoff_max_seconds is not None:
        os.environ["MODELKEYGUARD_SCANNER_BACKOFF_MAX_SECONDS"] = str(args.scanner_backoff_max_seconds)
    if args.scanner_breaker_enabled:
        os.environ["MODELKEYGUARD_SCANNER_BREAKER_ENABLED"] = "1"
    if args.scanner_breaker_max_failures is not None:
        os.environ["MODELKEYGUARD_SCANNER_BREAKER_MAX_FAILURES"] = str(args.scanner_breaker_max_failures)
    if args.scanner_error_family_policy_json:
        os.environ["MODELKEYGUARD_SCANNER_ERROR_FAMILY_POLICY_JSON"] = str(args.scanner_error_family_policy_json)


if __name__ == "__main__":
    raise SystemExit(main())
