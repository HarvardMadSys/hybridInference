"""Unit tests for BaseAdapter default messages() / stream_messages() impls."""

from __future__ import annotations

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
