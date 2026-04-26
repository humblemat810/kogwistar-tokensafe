from __future__ import annotations

import json
from typing import Any, Iterator

from .base import ProviderAdapter, as_bearer_header, default_upstream_url


def gemini_to_openai_payload(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, str]] = []

    system_instruction = payload.get("system_instruction")
    if system_instruction is None:
        # Some SDKs/libraries serialize this in camelCase.
        system_instruction = payload.get("systemInstruction")
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts", [])
        text = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text"))
        if text:
            messages.append({"role": "system", "content": text})

    for item in payload.get("contents", []):
        if not isinstance(item, dict):
            continue
        raw_role = str(item.get("role"))
        if raw_role == "model":
            role = "assistant"
        elif raw_role == "system":
            role = "system"
        else:
            role = "user"
        parts = item.get("parts", [])
        text = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text"))
        messages.append({"role": role, "content": text})

    generation = payload.get("generationConfig") if isinstance(payload.get("generationConfig"), dict) else {}
    max_tokens = generation.get("maxOutputTokens")
    return {
        "model": model,
        "messages": messages,
        "max_tokens": int(max_tokens) if max_tokens is not None else 512,
    }


def openai_to_gemini_response(data: dict[str, Any], model: str) -> dict[str, Any]:
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": content}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": model,
        "usageMetadata": data.get("usage", {}),
        "modelkeyguard": data.get("modelkeyguard", {}),
    }


class GeminiAdapter(ProviderAdapter):
    provider = "gemini"
    enforce_provider = True

    def __init__(self, stream: bool):
        self.stream = stream
        self.stream_media_type = "application/json"

    def auth_header(self, authorization: str | None, **kwargs: Any) -> str | None:
        x_goog_api_key = kwargs.get("x_goog_api_key")
        return as_bearer_header(str(x_goog_api_key)) if x_goog_api_key else authorization

    def model_name(self, payload: dict[str, Any], **kwargs: Any) -> str:
        return str(kwargs.get("route_model") or payload.get("model") or "")

    def canonical_payload(self, payload: dict[str, Any], model: str, **kwargs: Any) -> dict[str, Any]:
        return gemini_to_openai_payload(model, payload)

    def upstream_url(self, request: Any, model: str, **kwargs: Any) -> str | None:
        suffix = "streamGenerateContent" if self.stream else "generateContent"
        base = default_upstream_url("gemini").rstrip("/")
        return f"{base}/v1beta/models/{model}:{suffix}"

    def should_stream(self, payload: dict[str, Any], **kwargs: Any) -> bool:
        return self.stream

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> Iterator[bytes]:
        gemini = openai_to_gemini_response(data, model)
        yield (json.dumps(gemini) + "\n").encode("utf-8")

    def success_payload(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return openai_to_gemini_response(data, model)
