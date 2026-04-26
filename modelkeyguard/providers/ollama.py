from __future__ import annotations

import json
import time
from typing import Any, Iterator

from .base import ProviderAdapter, default_upstream_url


def openai_to_ollama_response(data: dict[str, Any], model: str, done: bool = True) -> dict[str, Any]:
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return {
        "model": model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": content},
        "done": done,
        "modelkeyguard": data.get("modelkeyguard", {}),
    }


class OllamaAdapter(ProviderAdapter):
    provider = "ollama"
    enforce_provider = True
    stream_media_type = "application/x-ndjson"

    def canonical_payload(self, payload: dict[str, Any], model: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": model,
            "messages": payload.get("messages", []),
            "max_tokens": int((payload.get("options") or {}).get("num_predict", 512) or 512),
        }

    def upstream_url(self, request: Any, model: str, **kwargs: Any) -> str | None:
        return default_upstream_url("ollama")

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> Iterator[bytes]:
        yield (json.dumps(openai_to_ollama_response(data, model, done=False)) + "\n").encode("utf-8")

    def success_payload(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return openai_to_ollama_response(data, model, done=True)
