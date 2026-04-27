from __future__ import annotations

import json
import time
from typing import Any, Iterator

from .base import ProviderAdapter, default_upstream_url


def openai_stream_chunks(data: dict[str, Any]) -> list[bytes]:
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    model = str(data.get("model") or "")
    chunk = {
        "id": str(data.get("id") or "chatcmpl-modelkeyguard-stream"),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}],
    }
    return [f"data: {json.dumps(chunk)}\n\n".encode("utf-8"), b"data: [DONE]\n\n"]


def openai_responses_stream_chunks(data: dict[str, Any], model: str) -> list[bytes]:
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    response_id = str(data.get("id") or f"resp-modelkeyguard-stream-{int(time.time())}")
    delta = {"type": "response.output_text.delta", "response_id": response_id, "delta": content}
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "status": "completed",
        },
    }
    return [f"data: {json.dumps(delta)}\n\n".encode("utf-8"), f"data: {json.dumps(completed)}\n\n".encode("utf-8"), b"data: [DONE]\n\n"]


class OpenAIAdapter(ProviderAdapter):
    provider = "openai"
    enforce_provider = False
    stream_media_type = "text/event-stream"

    def model_name(self, payload: dict[str, Any], **kwargs: Any) -> str:
        return str(payload.get("model") or "")

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
        base = default_upstream_url("openai").rstrip("/")
        if operation == "responses":
            if base.endswith("/chat/completions"):
                return f"{base[:-len('/chat/completions')]}/responses"
            if base.endswith("/v1"):
                return f"{base}/responses"
            if base.endswith("/responses"):
                return base
            return f"{base}/responses"
        if base.endswith("/responses"):
            return f"{base[:-len('/responses')]}/chat/completions"
        return base

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> Iterator[bytes]:
        operation = str(kwargs.get("operation") or "")
        if operation == "responses":
            yield from iter(openai_responses_stream_chunks(data, model))
        else:
            yield from iter(openai_stream_chunks(data))

    def success_payload(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        operation = str(kwargs.get("operation") or "")
        if operation != "responses":
            return data
        if data.get("object") == "response":
            return data
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        out = {
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
        if "modelkeyguard" in data:
            out["modelkeyguard"] = data["modelkeyguard"]
        return out
