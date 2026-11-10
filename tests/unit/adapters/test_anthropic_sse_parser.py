"""The direct-Anthropic adapter frames its SSE with the shared, capped parser.

Both of its stream loops appended every read to one byte buffer and searched all
of it for the blank line that ends a frame: quadratic in a frame's length, and
unbounded for an upstream that never ends one. They now use ``SSEParser``, as
the shared HTTP client does: linear, failing a frame past its limit, and
framing CRLF streams too.

The time bounds are generous -- the parser needs milliseconds -- and the old
loops overshoot them several times over.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from serving.adapters import anthropic as anthropic_mod
from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.http import AsyncHTTPClient, UpstreamPayloadTooLarge
from serving.servers.sse import SSEParser

pytestmark = pytest.mark.unit

READ = 1024


def _cfg() -> ModelConfig:
    return ModelConfig(
        id="claude-x",
        name="Claude X",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        provider_model_id="claude-x",
        api_keys=["sk-ant-test"],
    )


def _event(kind: str, payload: dict, newline: bytes = b"\n") -> bytes:
    body = f"event: {kind}{newline.decode()}data: {json.dumps(payload)}{newline.decode()}"
    return body.encode() + newline


def _body(text: str, newline: bytes = b"\n") -> bytes:
    return b"".join(
        [
            _event(
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 11}}},
                newline,
            ),
            _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
                newline,
            ),
            _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 9},
                },
                newline,
            ),
        ]
    )


def _serve(monkeypatch, body: bytes, read: int = READ) -> None:
    class _FakeContent:
        async def iter_any(self):
            for start in range(0, len(body), read):
                yield body[start : start + read]

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, *_a, **_k):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)


async def _translated(adapter: AnthropicAdapter) -> list[str]:
    return [
        chunk if isinstance(chunk, str) else chunk.decode()
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]


async def _passthrough(adapter: AnthropicAdapter, usage: dict) -> bytes:
    chunks = [
        chunk
        async for chunk in adapter.stream_messages(
            {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]},
            request_id="r",
            usage_sink=usage,
        )
    ]
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_a_large_event_in_small_reads_is_translated_in_linear_time(monkeypatch):
    """8 MiB in 1 KiB reads: the old loop rescanned and recopied its buffer per read."""
    text = "x" * (8 * 1024 * 1024)
    _serve(monkeypatch, _body(text))

    start = time.perf_counter()
    chunks = await _translated(AnthropicAdapter(_cfg()))
    elapsed = time.perf_counter() - start

    assert elapsed < 2.5, f"an 8 MiB event took {elapsed:.2f}s"
    content = json.loads(chunks[0].removeprefix("data: "))["choices"][0]["delta"]["content"]
    assert content == text
    final = json.loads(chunks[-2].removeprefix("data: "))
    assert final["usage"]["completion_tokens"] == 9


@pytest.mark.asyncio
async def test_a_large_event_in_small_reads_is_passed_through_in_linear_time(monkeypatch):
    body = _body("y" * (8 * 1024 * 1024))
    _serve(monkeypatch, body)
    usage: dict = {}

    start = time.perf_counter()
    relayed = await _passthrough(AnthropicAdapter(_cfg()), usage)
    elapsed = time.perf_counter() - start

    assert elapsed < 2.5, f"an 8 MiB event took {elapsed:.2f}s"
    assert relayed == body  # the bytes go through untouched
    assert (usage["input_tokens"], usage["output_tokens"]) == (11, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["translated", "passthrough"])
async def test_a_frame_that_never_ends_fails_the_stream(monkeypatch, path):
    """Past the parser's limit the stream fails, as on the shared client, instead of growing."""
    monkeypatch.setattr(anthropic_mod, "SSEParser", lambda: SSEParser(max_frame_chars=64 * 1024))
    _serve(monkeypatch, b"data: " + b"z" * (256 * 1024))
    adapter = AnthropicAdapter(_cfg())

    with pytest.raises(UpstreamPayloadTooLarge):
        if path == "translated":
            await _translated(adapter)
        else:
            await _passthrough(adapter, {})


@pytest.mark.asyncio
async def test_a_crlf_stream_is_relayed_as_it_arrives(monkeypatch):
    """CRLF frames never matched the old ``\\n\\n`` split: nothing came out until the body ended."""
    head = _body("hello", newline=b"\r\n").split(b"event: message_delta")[0]
    tail = (
        b"event: message_delta" + _body("hello", newline=b"\r\n").split(b"event: message_delta")[1]
    )
    relayed = asyncio.Event()

    class _FakeContent:
        async def iter_any(self):
            yield head
            # The client must have the text before the rest of the body is sent.
            await asyncio.wait_for(relayed.wait(), 2)
            yield tail

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, *_a, **_k):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    chunks = []
    async for chunk in AnthropicAdapter(_cfg()).stream_chat_completion(
        [{"role": "user", "content": "hi"}]
    ):
        chunks.append(chunk)
        if "hello" in chunk:
            relayed.set()

    assert json.loads(chunks[0].removeprefix("data: "))["choices"][0]["delta"]["content"] == (
        "hello"
    )
    final = json.loads(chunks[-2].removeprefix("data: "))
    assert final["usage"]["completion_tokens"] == 9
