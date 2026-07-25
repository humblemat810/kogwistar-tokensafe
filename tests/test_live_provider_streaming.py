from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from modelkeyguard.gateway import UpstreamStream, _open_upstream_stream, _stream_upstream


_PROVIDERS = ("openai", "azure_openai", "gemini", "ollama")


def _live_config(provider: str) -> tuple[str, str, str] | None:
    prefix = f"MODELKEYGUARD_LIVE_{provider.upper()}_"
    url = os.getenv(prefix + "URL", "").strip()
    secret = os.getenv(prefix + "KEY", "")
    model = os.getenv(prefix + "MODEL", "")
    if not url or not model:
        return None
    return url, secret, model


def _payload(provider: str, model: str) -> bytes:
    if provider == "gemini":
        return ("{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"Reply with one word: live\"}]}],"
                "\"generationConfig\":{\"maxOutputTokens\":8}}" ).encode()
    if provider == "ollama":
        return json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with one word: live"}], "stream": True}).encode()
    return json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with one word: live"}], "stream": True, "stream_options": {"include_usage": True}}).encode()


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_live_provider_streams_before_eof(provider):
    config = _live_config(provider)
    if config is None:
        pytest.skip(f"configure MODELKEYGUARD_LIVE_{provider.upper()}_URL and _MODEL")
    url, secret, model = config
    stream = UpstreamStream(
        secret=secret,
        raw=_payload(provider, model),
        provider=provider,
        target_url=url,
        content_type="application/json",
        request_id=f"live-test-{provider}-{time.time_ns()}",
        key_id="live-test",
        model=model,
        decision=None,
        estimated_cost=0.0,
        estimated_tokens=0,
        policy={},
        graph_state=None,
    )
    chunks: list[bytes] = []
    first_chunk_at: list[float] = []
    terminal: dict[str, object] = {}

    async def on_chunk(chunk: bytes) -> None:
        if not first_chunk_at:
            first_chunk_at.append(time.monotonic())
        chunks.append(chunk)

    async def on_complete(status: int, usage: dict | None, released: bool) -> None:
        terminal.update(status=status, usage=usage, released=released)

    async def on_uncertain(status: int) -> None:
        terminal.update(status=status, uncertain=True)

    async def run() -> tuple[bytes, dict | None, int, float]:
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        body, usage, status = await _stream_upstream(
            stream, on_chunk=on_chunk, on_complete=on_complete, on_uncertain=on_uncertain
        )
        return body, usage, status, time.monotonic()

    body, usage, status, finished = asyncio.run(run())
    assert 200 <= status < 300, body[:500]
    assert chunks and body.startswith(chunks[0])
    assert first_chunk_at and first_chunk_at[0] < finished
    assert terminal.get("released") is False
    if os.getenv("MODELKEYGUARD_LIVE_REQUIRE_USAGE", "0").lower() in {"1", "true", "yes"}:
        assert usage is not None, "provider emitted no terminal usage"
