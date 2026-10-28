"""Unit tests for BaseAdapter default messages() / stream_messages() impls."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from serving.adapters.base import BaseAdapter, ModelConfig


class _FakeOpenAIAdapter(BaseAdapter):
    """Captures translated OpenAI requests; returns canned responses."""

    last_messages: list[dict[str, Any]] | None = None
    last_params: dict[str, Any] | None = None

    async def chat_completion(self, messages, **params):
        self.last_messages = messages
        self.last_params = params
        return {
            "id": "chatcmpl-fake",
            "model": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def stream_chat_completion(self, messages, **params) -> AsyncGenerator[str, None]:
        self.last_messages = messages
        self.last_params = params
        # Three OpenAI SSE chunks.
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'


def _cfg() -> ModelConfig:
    return ModelConfig(id="fake", name="Fake", provider="fake", base_url="http://x")


def test_native_format_default():
    a = _FakeOpenAIAdapter(_cfg())
    assert a.native_format == "openai"


@pytest.mark.asyncio
async def test_default_messages_translates_through_chat_completion():
    a = _FakeOpenAIAdapter(_cfg())
    body = {"model": "fake", "max_tokens": 10, "messages": [{"role": "user", "content": "Hello"}]}
    out = await a.messages(body, request_id="req_1")
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "Hi"
    # Verify translation reached the inner chat_completion.
    assert a.last_messages == [{"role": "user", "content": "Hello"}]
    assert a.last_params["max_tokens"] == 10


async def _stream_without_cache_usage(self, messages, **params):
    self.last_messages = messages
    self.last_params = params
    yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
    yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'


@pytest.mark.asyncio
async def test_default_stream_messages_translates_sse():
    a = _FakeOpenAIAdapter(_cfg())
    body = {
        "model": "fake",
        "max_tokens": 10,
        "stream": True,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    out = b""
    async for chunk in a.stream_messages(body, request_id="req_1"):
        out += chunk
    assert b"event: message_start" in out
    assert b"event: message_stop" in out
    assert b'"text": "Hi"' in out


@pytest.mark.asyncio
async def test_default_stream_messages_keeps_absent_cache_usage_unknown(monkeypatch):
    """Translator streams must not turn missing cache fields into measured zeros."""
    sink: dict[str, int] = {}
    a = _FakeOpenAIAdapter(_cfg())
    monkeypatch.setattr(
        _FakeOpenAIAdapter,
        "stream_chat_completion",
        _stream_without_cache_usage,
        raising=True,
    )
    body = {
        "model": "fake",
        "max_tokens": 10,
        "stream": True,
        "messages": [{"role": "user", "content": "Hi"}],
    }

    async for _chunk in a.stream_messages(body, request_id="req-cache", usage_sink=sink):
        pass

    assert sink["input_tokens"] == 1
    assert sink["output_tokens"] == 1
    assert "cache_read_input_tokens" not in sink
    assert "cache_creation_input_tokens" not in sink


class _ScriptedOpenAIAdapter(_FakeOpenAIAdapter):
    """Yields a fixed OpenAI SSE script, then errors or blocks forever."""

    def __init__(self, config, chunks, *, error=None, block_after=False):
        super().__init__(config)
        self.chunks = chunks
        self.error = error
        self.block_after = block_after
        self.upstream_closed = False

    async def stream_chat_completion(self, messages, **params) -> AsyncGenerator[str, None]:
        self.last_messages = messages
        self.last_params = params
        try:
            for chunk in self.chunks:
                yield chunk
            if self.error is not None:
                raise self.error
            if self.block_after:
                await asyncio.Event().wait()
        finally:
            self.upstream_closed = True


def _stream_body() -> dict[str, Any]:
    return {
        "model": "fake",
        "max_tokens": 10,
        "stream": True,
        "messages": [{"role": "user", "content": "Hi"}],
    }


def _openai_tool_chunk(
    arguments: str, *, index: int = 0, call_id: str = "call_1", name: str = "lookup"
) -> str:
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "model": "fake",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _openai_text_chunk(content: str) -> str:
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "model": "fake",
        "choices": [{"index": 0, "delta": {"content": content}}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _sse_events(raw: bytes):
    events = []
    event_type = None
    for line in raw.decode().splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: ") :]
        elif line.startswith("data: "):
            events.append((event_type, json.loads(line[len("data: ") :])))
    return events


@pytest.mark.asyncio
async def test_stream_messages_upstream_error_flushes_received_content_without_terminal():
    """An error is not a successful end: keep buffered content, emit no stop."""
    a = _ScriptedOpenAIAdapter(
        _cfg(),
        [_openai_tool_chunk('{"a":'), _openai_text_chunk("visible-before-error")],
        error=RuntimeError("upstream failed"),
    )
    out = b""
    with pytest.raises(RuntimeError, match="upstream failed"):
        async for chunk in a.stream_messages(_stream_body(), request_id="req_err"):
            out += chunk

    # The text was already received and was waiting behind an incomplete tool.
    assert b'"text": "visible-before-error"' in out
    # Treating the failure as a normal finish would fabricate a successful
    # Anthropic terminal event; the router must see the exception instead.
    assert b"event: message_stop" not in out
    assert b"event: message_delta" not in out


@pytest.mark.asyncio
async def test_stream_messages_upstream_error_preserves_pending_parallel_call():
    """The abort path must not drop a complete call still pending behind another."""
    a = _ScriptedOpenAIAdapter(
        _cfg(),
        [
            _openai_tool_chunk('{"x":', index=0, call_id="call_0", name="first"),
            _openai_tool_chunk('{"x":1}', index=1, call_id="call_1", name="second"),
        ],
        error=RuntimeError("upstream failed"),
    )
    out = b""
    with pytest.raises(RuntimeError, match="upstream failed"):
        async for chunk in a.stream_messages(_stream_body(), request_id="req_pending_err"):
            out += chunk

    events = _sse_events(out)
    starts = [
        (data["content_block"].get("id"), data["content_block"].get("name"))
        for name, data in events
        if name == "content_block_start" and data["content_block"]["type"] == "tool_use"
    ]
    assert starts == [("call_0", "first"), ("call_1", "second")]

    args: dict[int, str] = {}
    for name, data in events:
        if name != "content_block_delta" or data["delta"].get("type") != "input_json_delta":
            continue
        args[data["index"]] = args.get(data["index"], "") + data["delta"]["partial_json"]
    assert list(args.values()) == ['{"x":', '{"x":1}']
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [None, "1", True], ids=["null", "str", "bool"])
async def test_stream_messages_invalid_tool_index_raises_after_closing_blocks(index):
    """A non-integer index takes the abort path: closed blocks, no terminal."""
    a = _ScriptedOpenAIAdapter(
        _cfg(),
        [
            _openai_tool_chunk('{"a":', call_id="call_0"),
            _openai_text_chunk("queued"),
            _openai_tool_chunk('{"b":1}', index=index, call_id="call_bad"),
            _openai_text_chunk("never"),
        ],
    )
    out = b""
    with pytest.raises(ValueError, match="tool call index must be an integer"):
        async for chunk in a.stream_messages(_stream_body(), request_id="req_bad_index"):
            out += chunk

    events = _sse_events(out)
    starts = [
        (data["content_block"]["type"], data["content_block"].get("id"))
        for name, data in events
        if name == "content_block_start"
    ]
    assert starts == [("tool_use", "call_0"), ("text", None)]
    stops = [data["index"] for name, data in events if name == "content_block_stop"]
    assert sorted(stops) == [0, 1]
    assert b"call_bad" not in out
    assert b"never" not in out
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)
    # The owned upstream generator is closed on the error path too.
    assert a.upstream_closed


@pytest.mark.asyncio
async def test_stream_messages_cancellation_does_not_emit_terminal_or_flush_buffers():
    """A cancelled client is not an upstream finish; do not dress it as one."""
    a = _ScriptedOpenAIAdapter(
        _cfg(),
        [_openai_tool_chunk('{"a":'), _openai_text_chunk("held-on-cancel")],
        block_after=True,
    )
    out = b""
    agen = a.stream_messages(_stream_body(), request_id="req_cancel")
    try:
        for _ in range(20):
            chunk = await agen.__anext__()
            out += chunk
            if b": keepalive" in chunk:
                break
        else:  # pragma: no cover - a hang would mean the activity frame vanished
            pytest.fail("translator never signalled buffered upstream activity")
    finally:
        await agen.aclose()

    assert b"event: message_stop" not in out
    assert b"event: message_delta" not in out
    assert b"held-on-cancel" not in out
    # Closing the outer generator must finalize its owned upstream rather than
    # leaving the provider stream for the event loop's finalizer.
    assert a.upstream_closed


@pytest.mark.asyncio
async def test_stream_messages_aclose_closes_owned_upstream_immediately():
    """An early consumer close must run the owned upstream generator's finally.

    This mirrors the independent probe: ``stream_chat_completion`` is a plain
    function returning a retained async generator, and the outer generator is
    closed after one event.
    """

    class _TrackedAdapter(_FakeOpenAIAdapter):
        def __init__(self, config):
            super().__init__(config)
            self.closed = False
            self.upstream = None

        def stream_chat_completion(self, messages, **params):
            async def _produce():
                try:
                    yield _openai_text_chunk("hello")
                    yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                finally:
                    self.closed = True

            self.upstream = _produce()  # Retain so GC cannot mask a missing close.
            return self.upstream

    adapter = _TrackedAdapter(_cfg())
    stream = adapter.stream_messages(_stream_body(), request_id="req_close")
    try:
        await anext(stream)
        await stream.aclose()
        assert adapter.closed
    finally:
        assert adapter.upstream is not None
        await adapter.upstream.aclose()
