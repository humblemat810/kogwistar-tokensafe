from __future__ import annotations

import json
import time
from typing import Any, Iterator

from .base import ProviderAdapter


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


class OpenAIAdapter(ProviderAdapter):
    provider = "openai"
    enforce_provider = False
    stream_media_type = "text/event-stream"

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> Iterator[bytes]:
        yield from iter(openai_stream_chunks(data))
