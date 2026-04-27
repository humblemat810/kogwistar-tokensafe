from __future__ import annotations

import time
from typing import Any

from .base import ProviderAdapter, default_upstream_url
from .openai import openai_stream_chunks


class AzureOpenAIAdapter(ProviderAdapter):
    provider = "azure_openai"
    enforce_provider = True
    stream_media_type = "text/event-stream"

    def model_name(self, payload: dict[str, Any], **kwargs: Any) -> str:
        return str(payload.get("model") or kwargs.get("deployment") or "")

    def canonical_payload(self, payload: dict[str, Any], model: str, **kwargs: Any) -> dict[str, Any]:
        canonical = dict(payload, model=model)
        operation = str(kwargs.get("operation") or "")
        if operation == "responses" and "messages" not in canonical:
            converted: list[dict[str, Any]] = []
            for item in payload.get("input", []) if isinstance(payload.get("input"), list) else []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                role = str(item.get("role", "")).strip() or "user"
                content = item.get("content")
                if isinstance(content, list):
                    parts: list[str] = []
                    for part in content:
                        if isinstance(part, dict):
                            if "text" in part:
                                parts.append(str(part.get("text")))
                            elif "content" in part:
                                parts.append(str(part.get("content")))
                        elif isinstance(part, str):
                            parts.append(part)
                    text = "".join(parts)
                else:
                    text = str(content or "")
                converted.append({"role": role, "content": text})
            canonical["messages"] = converted
        return canonical

    def upstream_url(self, request: Any, model: str, **kwargs: Any) -> str | None:
        operation = str(kwargs.get("operation") or "chat_completions")
        if operation == "responses":
            path = "/openai/responses"
        else:
            deployment = str(kwargs.get("deployment") or model)
            path = f"/openai/deployments/{deployment}/chat/completions"
        qs = request.url.query
        base = default_upstream_url("azure_openai").rstrip("/")
        url = f"{base}{path}"
        if qs:
            url = f"{url}?{qs}"
        return url

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any):
        yield from iter(openai_stream_chunks(data))

    def success_payload(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        operation = str(kwargs.get("operation") or "")
        if operation != "responses":
            return data
        if data.get("object") == "response":
            return data
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        return {
            "id": str(data.get("id") or f"resp-modelkeyguard-{int(time.time())}"),
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        }
