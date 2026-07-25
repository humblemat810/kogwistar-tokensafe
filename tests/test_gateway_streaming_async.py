from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from modelkeyguard.core import AccessDecision, ModelKeyGuard, Principal, Request
from modelkeyguard.gateway import UpstreamStream, _idempotency_read, _idempotency_write, _open_upstream_stream, _stream_upstream, _usage_from_stream_chunk
from modelkeyguard.graph_state import GraphStateStore
from modelkeyguard.reconciliation import expire_stale_reservations, http_provider_reconciler, reconcile_with_provider, register_provider_reconciler, uncertain_requests


class _SplitStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"usage":{"prompt_tokens":2,'
        yield b'"completion_tokens":3,"total_tokens":5}}\n\n'


class _DrainStream(httpx.AsyncByteStream):
    def __init__(self, release: asyncio.Event):
        self.release = release

    async def __aiter__(self):
        yield b"data: first\n\n"
        await self.release.wait()
        yield b"data: terminal\n\n"


def test_async_stream_forwards_chunks_and_parses_split_usage(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=_SplitStream(), headers={"content-type": "text/event-stream"}))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream(
        secret="secret",
        raw=b"{}",
        provider="openai",
        target_url="https://upstream.test/v1/chat/completions",
        content_type="application/json",
        request_id="req-1",
        key_id="key-1",
        model="gpt-test",
        decision=None,
        estimated_cost=1.0,
        estimated_tokens=100,
        policy={},
        graph_state=None,
    )
    seen: list[bytes] = []
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        body, usage, status = await _stream_upstream(
            stream,
            on_chunk=lambda chunk: _append(seen, chunk),
            on_complete=lambda code, value, released: _complete(terminal, code, value, released),
            on_uncertain=lambda code: _uncertain(terminal, code),
        )
        return body, usage, status

    body, usage, status = asyncio.run(run())
    assert status == 200
    assert seen == [b'data: {"usage":{"prompt_tokens":2,', b'"completion_tokens":3,"total_tokens":5}}\n\n']
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert body == b"".join(seen)
    assert terminal["released"] is False


@pytest.mark.parametrize(
    ("provider", "chunk", "expected"),
    [
        ("openai", b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n', {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
        ("azure_openai", b'data: {"usage":{"prompt_tokens":2,"completion_tokens":4,"total_tokens":6}}\n\n', {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}),
        ("gemini", b'{"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":5,"totalTokenCount":8}}\n', {"promptTokenCount": 3, "candidatesTokenCount": 5, "totalTokenCount": 8}),
        ("ollama", b'{"done":true,"prompt_eval_count":7,"eval_count":9}\n', {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}),
    ],
)
def test_provider_terminal_usage_shapes(provider, chunk, expected):
    assert _usage_from_stream_chunk(chunk, provider) == expected


@pytest.mark.parametrize(
    ("provider", "chunk"),
    [
        ("openai", b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'),
        ("azure_openai", b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'),
        ("gemini", b'{"candidates":[{"content":{"parts":[{"text":"c"}]}}]}\n'),
        ("ollama", b'{"message":{"content":"d"},"done":false}\n'),
    ],
)
def test_each_provider_forwards_async_chunks_before_eof(monkeypatch, provider, chunk):
    class OneChunk(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk
            await asyncio.sleep(0)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=OneChunk()))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream("secret", b"{}", provider, "https://upstream.test", "application/json", f"req-{provider}", "key", "model", None, 1.0, 10, {}, None)
    seen: list[bytes] = []
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        return await _stream_upstream(
            stream,
            on_chunk=lambda value: _append(seen, value),
            on_complete=lambda code, usage, released: _complete(terminal, code, usage, released),
            on_uncertain=lambda code: _uncertain(terminal, code),
        )

    _body, _usage, status = asyncio.run(run())
    assert status == 200
    assert seen == [chunk]
    assert terminal["released"] is False


def test_async_stream_provider_4xx_releases_without_settlement(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(429, content=b'{"error":"rate"}', headers={"content-type": "application/json"}))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream("secret", b"{}", "openai", "https://upstream.test", "application/json", "req-4xx", "key", "m", None, 1.0, 10, {}, None)
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        return await _stream_upstream(
            stream,
            on_chunk=lambda chunk: _append([], chunk),
            on_complete=lambda code, usage, released: _complete(terminal, code, usage, released),
            on_uncertain=lambda code: _uncertain(terminal, code),
        )

    _body, usage, status = asyncio.run(run())
    assert status == 429
    assert usage is None
    assert terminal == {"code": 429, "usage": None, "released": True}


def test_async_stream_provider_5xx_marks_uncertain(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(503, content=b"temporary"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream("secret", b"{}", "openai", "https://upstream.test", "application/json", "req-5xx", "key", "m", None, 1.0, 10, {}, None)
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        return await _stream_upstream(stream, on_chunk=lambda chunk: _append([], chunk), on_complete=lambda *args: _complete(terminal, *args), on_uncertain=lambda code: _uncertain(terminal, code))

    _body, _usage, status = asyncio.run(run())
    assert status == 503
    assert terminal["uncertain"] == 503


@pytest.mark.parametrize("provider", ["openai", "azure_openai", "gemini", "ollama"])
def test_each_provider_error_status_has_explicit_terminal_state(monkeypatch, provider):
    transport = httpx.MockTransport(lambda request: httpx.Response(429, content=b'{"error":"rate"}'))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream("secret", b"{}", provider, "https://upstream.test", "application/json", f"req-4xx-{provider}", "key", "m", None, 1.0, 10, {}, None)
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        return await _stream_upstream(
            stream,
            on_chunk=lambda chunk: _append([], chunk),
            on_complete=lambda code, usage, released: _complete(terminal, code, usage, released),
            on_uncertain=lambda code: _uncertain(terminal, code),
        )

    _body, usage, status = asyncio.run(run())
    assert status == 429
    assert usage is None
    assert terminal["released"] is True


def test_idempotency_claim_is_durable_and_single_winner(tmp_path):
    path = tmp_path / "graph.jsonl"
    first = ModelKeyGuard(graph_state=GraphStateStore(path, app_key="test-key"))
    record = {"payload_hash": "hash", "state": "forwarding", "expires": 9999999999}
    assert _idempotency_write(first, "tenant:principal:openai:key:req", record, claim=True)
    second = ModelKeyGuard(graph_state=GraphStateStore(path, app_key="test-key"))
    assert not _idempotency_write(second, "tenant:principal:openai:key:req", record, claim=True)
    assert _idempotency_read(second, "tenant:principal:openai:key:req")["state"] == "forwarding"


def test_disconnect_drains_upstream_before_settlement(monkeypatch):
    release = asyncio.Event()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=_DrainStream(release)))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    stream = UpstreamStream("secret", b"{}", "openai", "https://upstream.test", "application/json", "req-drain", "key", "m", None, 1.0, 10, {}, None)
    seen: list[bytes] = []
    terminal: dict[str, object] = {}

    async def run():
        client, response = await _open_upstream_stream(stream)
        stream.opened_client, stream.opened_response = client, response
        return await _stream_upstream(
            stream,
            on_chunk=lambda chunk: _append(seen, chunk),
            on_complete=lambda code, usage, released: _complete(terminal, code, usage, released),
            on_uncertain=lambda code: _uncertain(terminal, code),
        )

    async def scenario():
        producer = asyncio.create_task(run())
        while not seen:
            await asyncio.sleep(0)
        async def consume():
            return await asyncio.shield(producer)

        consumer = asyncio.create_task(consume())
        consumer.cancel()
        release.set()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        result = await producer
        return result

    _body, _usage, status = asyncio.run(scenario())
    assert status == 200
    assert terminal["released"] is False
    assert seen == [b"data: first\n\n", b"data: terminal\n\n"]


def test_reservation_admission_allows_only_one_request_under_quota(tmp_path):
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    guard = ModelKeyGuard(graph_state=graph)
    guard._quota_policies = lambda lane, subject: [{"period": "hour", "max_requests": 1, "max_tokens": 100, "max_usd": 10.0}]
    request = Request(
        principal=Principal("agent:test", "agent"),
        key_id="key:test",
        model="gpt-test",
        namespace="tenant:test",
        estimated_cost_usd=1.0,
        estimated_tokens=10,
        request_id="req-quota",
        token_id="tok-1",
    )
    decision = AccessDecision(True, False, 200, "allow", "ok", "key:test", "agent:test", "tenant:test", "req-quota", "tok-1", None, {}, "env://TEST")
    def attempt(index: int) -> bool:
        req = Request(**{**request.__dict__, "request_id": f"req-quota-{index}"})
        dec = AccessDecision(**{**decision.__dict__, "request_id": req.request_id})
        return guard.reserve_admission(req, dec)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sorted(outcomes) == [False, True]


def test_settlement_cas_is_idempotent_after_crash_before_ledger(tmp_path):
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    guard = ModelKeyGuard(graph_state=graph)
    guard._quota_policies = lambda lane, subject: [{"period": "hour", "max_requests": 10, "max_tokens": 1000, "max_usd": 100.0}]
    request = Request(
        principal=Principal("agent:settle", "agent"), key_id="key:settle", model="m", namespace="tenant:test",
        estimated_cost_usd=1.0, estimated_tokens=10, request_id="req-settle", token_id="tok-settle",
    )
    decision = AccessDecision(True, False, 200, "allow", "ok", "key:settle", "agent:settle", "tenant:test", "req-settle", "tok-settle", None, {}, "env://TEST")
    assert guard.reserve_admission(request, decision)
    original_ledger = graph.append_usage_ledger_event
    graph.append_usage_ledger_event = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash"))
    guard.record_usage(decision, 1.0, 0.25, 7)
    graph.append_usage_ledger_event = original_ledger

    marker = graph.get_named_projection("modelkeyguard_settlement", "req-settle")
    assert marker["state"] == "settled"
    assert graph.get_quota_used("principal", "agent:settle", "hour")["requests"] == 1
    guard.record_usage(decision, 1.0, 0.25, 7)
    assert graph.get_quota_used("principal", "agent:settle", "hour")["requests"] == 1
    assert any(node.kind == "usage_ledger_event" and node.payload.get("request_id") == "req-settle" for node in graph.nodes.values())


def test_reconciliation_marks_stale_forwarding_uncertain_without_retry(tmp_path):
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    graph.replace_named_projection(
        "modelkeyguard_reservation",
        "active",
        {"reservations": {"req-crash": {"state": "forwarding", "expires_at": 1, "lanes": []}}},
    )
    assert expire_stale_reservations(graph, now=2) == {"uncertain": 1, "released": 0}
    rows = uncertain_requests(graph)
    assert rows[0]["request_id"] == "req-crash"
    assert rows[0]["state"] == "uncertain"


def test_provider_reconciler_settles_uncertain_without_resend(tmp_path):
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    graph.replace_named_projection(
        "modelkeyguard_reservation",
        "active",
        {"reservations": {"req-provider": {"state": "uncertain", "provider": "openai", "expires_at": 9999999999, "estimated_cost_usd": 1.0, "estimated_tokens": 10, "lanes": [{"lane": "principal", "subject_id": "agent:test", "period": "hour"}]}}},
    )
    register_provider_reconciler("openai", lambda record: {"outcome": "settled", "actual_tokens": 7, "actual_cost_usd": 0.01})
    result = reconcile_with_provider(graph, "req-provider", provider="openai")
    assert result["outcome"] == "settled"
    assert uncertain_requests(graph) == []
    assert graph.get_quota_used("principal", "agent:test", "hour")["tokens"] == 7


def test_provider_reconciler_cannot_request_resend(tmp_path):
    graph = GraphStateStore(tmp_path / "graph.jsonl", app_key="test-key")
    graph.replace_named_projection(
        "modelkeyguard_reservation",
        "active",
        {"reservations": {"req-no-retry": {"state": "uncertain", "expires_at": 9999999999, "lanes": []}}},
    )
    register_provider_reconciler("ollama", lambda record: {"retry": True})
    with pytest.raises(ValueError, match="automatic_retry_forbidden"):
        reconcile_with_provider(graph, "req-no-retry", provider="ollama")


def test_http_provider_reconciler_is_read_only_and_maps_usage(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"outcome":"settled","actual_tokens":12,"actual_cost_usd":0.02}'

    seen = {}

    def fake_urlopen(request, timeout):
        seen["method"] = request.method
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("modelkeyguard.reconciliation.urllib.request.urlopen", fake_urlopen)
    resolver = http_provider_reconciler("https://reconcile.test/{request_id}")
    assert resolver({"request_id": "req/a"}) == {"outcome": "settled", "actual_cost_usd": 0.02, "actual_tokens": 12}
    assert seen["method"] == "GET"
    assert seen["url"].endswith("req%2Fa")


async def _append(target: list[bytes], value: bytes) -> None:
    target.append(value)


async def _complete(target: dict[str, object], code: int, usage: object, released: bool) -> None:
    target.update(code=code, usage=usage, released=released)


async def _uncertain(target: dict[str, object], code: int) -> None:
    target.update(uncertain=code)
