#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from pydantic import BaseModel, Field


class GuardedStructuredOutput(BaseModel):
    summary: str = Field(description="One short summary sentence.")
    risk_level: str = Field(description="One of: low, medium, high.")
    action: str = Field(description="Recommended next action.")


def _build_messages(system_prompt: str, user_prompt: str):
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Real Azure LangChain structured-output smoke via ModelKeyGuard")
    p.add_argument("--base-url", default=os.getenv("KGW_BASE_URL", "http://127.0.0.1:8789"))
    p.add_argument("--safe-token", default=os.getenv("KGW_SAFE_TOKEN", ""))
    p.add_argument("--deployment", default=os.getenv("KGW_AZURE_DEPLOYMENT", ""))
    p.add_argument("--api-version", default=os.getenv("KGW_AZURE_API_VERSION", "2024-10-21"))
    p.add_argument(
        "--system-prompt",
        default=os.getenv("KGW_SYSTEM_PROMPT", "You are a strict enterprise assistant that returns structured JSON content."),
    )
    p.add_argument(
        "--user-prompt",
        default=os.getenv(
            "KGW_USER_PROMPT",
            "Summarize policy drift risk for a deployment in one sentence and provide a risk level and action.",
        ),
    )
    return p.parse_args()


def run(args: argparse.Namespace) -> int:
    if not args.safe_token:
        print("error: --safe-token (or KGW_SAFE_TOKEN) is required", file=sys.stderr)
        return 2
    if not args.deployment:
        print("error: --deployment (or KGW_AZURE_DEPLOYMENT) is required", file=sys.stderr)
        return 2

    try:
        from langchain_openai import AzureChatOpenAI
    except ModuleNotFoundError as e:
        print(f"error: missing dependency: {e}", file=sys.stderr)
        print(
            "Install separate smoke deps first:\n"
            "  bash scripts/setup_langchain_smoke_env.sh\n"
            "  source .venv-langchain-smoke/bin/activate",
            file=sys.stderr,
        )
        return 2

    llm = AzureChatOpenAI(
        azure_endpoint=args.base_url.rstrip("/"),
        azure_deployment=args.deployment,
        api_version=args.api_version,
        api_key=args.safe_token,
        temperature=0,
    )
    model = llm.with_structured_output(GuardedStructuredOutput)
    result: Any = model.invoke(_build_messages(args.system_prompt, args.user_prompt))
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
