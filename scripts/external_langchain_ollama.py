#!/usr/bin/env python3
"""LangChain Ollama smoke client against the ModelKeyGuard gateway.

This uses ChatOllama against the gateway's Ollama-shaped /api/chat route and
authenticates with a safe token. Use it when you want to verify that the
gateway can route an Ollama-flavored request while still enforcing ACLs and
quotas.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

DEFAULT_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


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


def _build_llm():
    from langchain_ollama import ChatOllama

    base_url = os.getenv("KGW_BASE_URL", os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8789")).rstrip("/")
    model = os.getenv("KGW_OLLAMA_MODEL", os.getenv("OPENAI_MODEL", "gemma4:e2b"))
    headers = {}
    token = os.getenv("KGW_TOKEN", os.getenv("SAFE_TOKEN", os.getenv("OPENAI_API_KEY", ""))).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return ChatOllama(model=model, base_url=base_url, client_kwargs={"headers": headers}, temperature=0)
    except TypeError:
        return ChatOllama(model=model, base_url=base_url, headers=headers, temperature=0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct LangChain Ollama smoke client")
    parser.add_argument(
        "--message",
        default="Say one short line confirming this request reached Ollama directly.",
        help="user message to send",
    )
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    system = os.getenv("KGW_SYSTEM_PROMPT", DEFAULT_SYSTEM)
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [SystemMessage(content=system), HumanMessage(content=args.message)]
    llm = _build_llm()

    print(f"base_url={os.getenv('KGW_BASE_URL', os.getenv('OPENAI_BASE_URL', 'http://127.0.0.1:8789')).rstrip('/')}")
    print(f"model={os.getenv('KGW_OLLAMA_MODEL', os.getenv('OPENAI_MODEL', 'gemma4:e2b'))}")

    try:
        if args.stream:
            print("stream=true")
            for chunk in llm.stream(messages):
                text = _message_text(chunk)
                if text:
                    print(text, end="|", flush=True)
            print()
            return 0

        print("stream=false")
        result = llm.invoke(messages)
        print("response_text:")
        print(_message_text(result))
        metadata = getattr(result, "response_metadata", None)
        if isinstance(metadata, dict) and metadata:
            usage = metadata.get("token_usage") or metadata.get("usage") or metadata.get("usage_metadata")
            if usage:
                print("response_usage:")
                print(json.dumps(usage, sort_keys=True))
            finish_reason = metadata.get("finish_reason")
            if finish_reason:
                print(f"finish_reason: {finish_reason}")
        return 0
    except ModuleNotFoundError as e:
        print(f"error: missing dependency: {e}", file=sys.stderr)
        print(
            "Install smoke dependencies in a separate venv, then retry:\n"
            "  bash scripts/setup_langchain_smoke_env.sh\n"
            "  source .venv-langchain-smoke/bin/activate",
            file=sys.stderr,
        )
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
