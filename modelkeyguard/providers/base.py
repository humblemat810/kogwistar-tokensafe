from __future__ import annotations

import os
from typing import Any, Iterator


def default_upstream_url(provider: str) -> str:
    if provider == "azure_openai":
        return os.getenv("AZURE_OPENAI_UPSTREAM_URL", "https://example-resource.openai.azure.com")
    if provider == "gemini":
        return os.getenv("GEMINI_UPSTREAM_URL", "https://generativelanguage.googleapis.com")
    if provider == "ollama":
        return os.getenv("OLLAMA_UPSTREAM_URL", "http://127.0.0.1:11434/api/chat")
    return os.getenv("OPENAI_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions")


def as_bearer_header(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.lower().startswith("bearer "):
        return v
    return f"Bearer {v}"


class ProviderAdapter:
    provider = "openai"
    enforce_provider = False
    stream_media_type = "application/json"

    def auth_header(self, authorization: str | None, **kwargs: Any) -> str | None:
        return authorization

    def model_name(self, payload: dict[str, Any], **kwargs: Any) -> str:
        return str(payload.get("model") or "")

    def canonical_payload(self, payload: dict[str, Any], model: str, **kwargs: Any) -> dict[str, Any]:
        return dict(payload, model=model)

    def upstream_url(self, request: Any, model: str, **kwargs: Any) -> str | None:
        return None

    def forward_body(self, raw: bytes, payload: dict[str, Any], **kwargs: Any) -> bytes | None:
        return raw

    def forward_content_type(self, payload: dict[str, Any], **kwargs: Any) -> str:
        return "application/json"

    def should_stream(self, payload: dict[str, Any], **kwargs: Any) -> bool:
        return bool(payload.get("stream"))

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> Iterator[bytes]:
        raise NotImplementedError

    def success_payload(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return data
