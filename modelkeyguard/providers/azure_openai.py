from __future__ import annotations

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
        return dict(payload, model=model)

    def upstream_url(self, request: Any, model: str, **kwargs: Any) -> str | None:
        deployment = str(kwargs.get("deployment") or model)
        qs = request.url.query
        base = default_upstream_url("azure_openai").rstrip("/")
        url = f"{base}/openai/deployments/{deployment}/chat/completions"
        if qs:
            url = f"{url}?{qs}"
        return url

    def stream_chunks(self, data: dict[str, Any], model: str, payload: dict[str, Any], **kwargs: Any):
        yield from iter(openai_stream_chunks(data))
