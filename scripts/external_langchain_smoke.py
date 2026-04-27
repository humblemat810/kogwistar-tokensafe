#!/usr/bin/env python3
"""External-environment smoke client for ModelKeyGuard provider adapters.

Run this from a separate environment that has LangChain provider packages installed.
"""
from __future__ import annotations

import argparse
import json
import inspect
import os
import sys
from typing import Any

DEFAULT_SYSTEM = "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."


def _base_url() -> str:
    return os.getenv("KGW_BASE_URL", "http://127.0.0.1:8789").rstrip("/")


def _safe_token() -> str:
    return os.getenv("KGW_TOKEN", "kgw_demo_doc_ingestor")


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


def _build_openai_native():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.getenv("KGW_OPENAI_MODEL", "gpt-4o-mini"),
        base_url=f"{_base_url()}/v1",
        api_key=_safe_token(),
        temperature=0,
    )


def _build_azure_native():
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        model=os.getenv("KGW_AZURE_DEPLOYMENT", "azure-mini"),
        azure_deployment=os.getenv("KGW_AZURE_DEPLOYMENT", "azure-mini"),
        api_version=os.getenv("KGW_AZURE_API_VERSION", "2024-10-21"),
        azure_endpoint=os.getenv("KGW_AZURE_ENDPOINT", _base_url()),
        api_key=_safe_token(),
        temperature=0,
    )


def _build_ollama_native():
    from langchain_ollama import ChatOllama

    base = os.getenv("KGW_OLLAMA_BASE_URL", _base_url())
    model = os.getenv("KGW_OLLAMA_MODEL", "llama3.1")
    headers = {"Authorization": f"Bearer {_safe_token()}"}
    try:
        return ChatOllama(model=model, base_url=base, client_kwargs={"headers": headers}, temperature=0)
    except TypeError:
        return ChatOllama(model=model, base_url=base, headers=headers, temperature=0)


def _build_gemini_native():
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = os.getenv("KGW_GEMINI_MODEL", "gemini-2.0-flash")
    endpoint = os.getenv("KGW_GEMINI_ENDPOINT", _base_url())
    # ChatGoogleGenerativeAI endpoint override support is version-dependent.
    kwargs: dict[str, Any] = {
        "model": model,
        "google_api_key": _safe_token(),
        "temperature": 0,
    }
    sig = inspect.signature(ChatGoogleGenerativeAI)
    if "transport" in sig.parameters:
        kwargs["transport"] = "rest"
    if "client_options" in sig.parameters:
        kwargs["client_options"] = {"api_endpoint": endpoint}
    try:
        return ChatGoogleGenerativeAI(**kwargs)
    except TypeError as e:
        raise RuntimeError(
            "This langchain_google_genai version does not support custom endpoint override. "
            "Use --mode universal for Gemini fallback via ChatOpenAI."
        ) from e


def _build_universal(provider: str):
    from langchain_openai import ChatOpenAI

    model_by_provider = {
        "openai": os.getenv("KGW_OPENAI_MODEL", "gpt-4o-mini"),
        "azure_openai": os.getenv("KGW_AZURE_DEPLOYMENT", "azure-mini"),
        "ollama": os.getenv("KGW_OLLAMA_MODEL", "llama3.1"),
        "gemini": os.getenv("KGW_GEMINI_MODEL", "gemini-2.0-flash"),
    }
    return ChatOpenAI(
        model=model_by_provider[provider],
        base_url=f"{_base_url()}/v1",
        api_key=_safe_token(),
        temperature=0,
    )


def _build_llm(provider: str, mode: str):
    if mode == "universal":
        return _build_universal(provider)
    if provider == "openai":
        return _build_openai_native()
    if provider == "azure_openai":
        return _build_azure_native()
    if provider == "ollama":
        return _build_ollama_native()
    if provider == "gemini":
        return _build_gemini_native()
    raise RuntimeError(f"unsupported provider={provider}")


def _build_messages(system: str, user_text: str):
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=system), HumanMessage(content=user_text)]


def run(provider: str, mode: str, stream: bool, user_text: str) -> int:
    system = os.getenv("KGW_SYSTEM_PROMPT", DEFAULT_SYSTEM)
    messages = _build_messages(system, user_text)
    llm = _build_llm(provider, mode)

    if stream:
        print("stream=true")
        for chunk in llm.stream(messages):
            text = _message_text(chunk)
            if text:
                print(text, end="", flush=True)
        print()
        return 0

    print("stream=false")
    result = llm.invoke(messages)
    text = _message_text(result)
    print("response_text:")
    print(text)
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LangChain smoke client for ModelKeyGuard provider adapters")
    p.add_argument("--provider", choices=["openai", "azure_openai", "ollama", "gemini"], required=True)
    p.add_argument("--mode", choices=["native", "universal"], default="native")
    p.add_argument("--stream", action="store_true")
    p.add_argument("--message", default="Say one short line confirming the request passed through ModelKeyGuard.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return run(args.provider, args.mode, args.stream, args.message)
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
