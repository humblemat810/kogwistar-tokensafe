#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

from modelkeyguard.reviewer_agent import (
    DEFAULT_REVIEW_SYSTEM_PROMPT,
    ReviewStatusClient,
    run_langchain_reviewer,
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _reviewer_safe_token() -> str:
    return _env("REVIEWER_SAFE_TOKEN", "")


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
    args = parser.parse_args()

    status_client = ReviewStatusClient.from_env()
    status_client = ReviewStatusClient(
        base_url=args.base_url,
        bearer_token=status_client.bearer_token,
        admin_secret=status_client.admin_secret,
        keycloak=status_client.keycloak,
        timeout_seconds=status_client.timeout_seconds,
    )

    status = status_client.status()
    print("review_status:")
    print(json.dumps(status, indent=2, sort_keys=True))

    if not args.force and not bool(status.get("should_review")):
        print("reviewer_action: skipped (thresholds not met)")
        return 0

    token = _reviewer_safe_token()
    if not token:
        print("error: missing REVIEWER_SAFE_TOKEN for the Ollama-shaped review call", file=sys.stderr)
        print("hint: mint it with /admin/policy/tokens for principal agent:usage-reviewer and export REVIEWER_SAFE_TOKEN", file=sys.stderr)
        return 2

    result = run_langchain_reviewer(
        status=status,
        base_url=args.base_url,
        safe_token=token,
        model=args.model,
        system_prompt=args.system_prompt,
        key_id=_key_id(),
    )
    print("review_result:")
    print(json.dumps(result, indent=2, sort_keys=True))

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
