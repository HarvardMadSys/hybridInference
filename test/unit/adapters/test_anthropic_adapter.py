"""Unit tests for AnthropicAdapter (kind: anthropic).

Covers OpenAI-format northbound dispatch to api.anthropic.com upstream.
Anthropic-format passthrough (messages/stream_messages) lands in Task 8.
"""

from __future__ import annotations

from typing import Any

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig


def _cfg(**overrides) -> ModelConfig:
    base = {
        "id": "claude-opus-4.7",
        "name": "Claude Opus 4.7",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test",
        "provider_model_id": "claude-opus-4-7",
        "max_output_length": 1024,
        "supports_tools": True,
        "supported_params": [
            "temperature",
            "max_tokens",
            "top_p",
            "stream",
            "tools",
            "tool_choice",
            "stop",
        ],
    }
    base.update(overrides)
    return ModelConfig(**base)


@pytest.mark.asyncio
async def test_chat_completion_translates_to_anthropic_upstream(monkeypatch):
    upstream_resp = {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hello there"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    captured: dict[str, Any] = {}

    async def fake_json_post_with_retry(
        self, url, json=None, headers=None, timeout=None, retries=2
    ):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_json_post_with_retry)

    adapter = AnthropicAdapter(_cfg())
    out = await adapter.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=128,
        temperature=0.5,
    )

    # Output is OpenAI-format ChatCompletion shape.
    assert out["choices"][0]["message"]["content"] == "Hello there"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 3

    # Verify upstream request shape.
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["json"]["model"] == "claude-opus-4-7"
    assert captured["json"]["max_tokens"] == 128
    assert captured["json"]["messages"][0]["role"] == "user"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"]


@pytest.mark.asyncio
async def test_stream_chat_completion_translates_anthropic_sse_to_openai_chunks(monkeypatch):
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_x","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n'
        b"event: content_block_stop\n"
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            # Single chunk delivery is enough for the test; the parser handles partial frames.
            yield upstream_sse

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    adapter = AnthropicAdapter(_cfg())
    chunks = []
    async for c in adapter.stream_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        stream=True,
    ):
        chunks.append(c if isinstance(c, str) else c.decode("utf-8"))
    joined = "".join(chunks)

    # OpenAI-format stream output: at least one delta containing "Hi", plus [DONE].
    assert '"content"' in joined and '"Hi"' in joined
    assert "[DONE]" in joined


@pytest.mark.asyncio
async def test_messages_identity_passthrough(monkeypatch):
    body_in = {
        "model": "claude-opus-4.7",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Hi"}],
        "system": "Be helpful.",
    }
    upstream_resp = {
        "id": "msg_passthrough",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 2},
    }
    captured: dict[str, Any] = {}

    async def fake_json_post_with_retry(
        self, url, json=None, headers=None, timeout=None, retries=2
    ):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_json_post_with_retry)

    adapter = AnthropicAdapter(_cfg())
    out = await adapter.messages(body_in, request_id="req_pass")

    # Identity: upstream response returned verbatim.
    assert out == upstream_resp

    # Body forwarded verbatim except model rewritten to provider_model_id.
    assert captured["json"]["model"] == "claude-opus-4-7"
    assert captured["json"]["messages"] == [{"role": "user", "content": "Hi"}]
    assert captured["json"]["system"] == "Be helpful."
    assert captured["json"]["max_tokens"] == 200
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"]


@pytest.mark.asyncio
async def test_stream_messages_identity_passthrough_records_usage(monkeypatch):
    upstream_sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_y","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    class _FakeContent:
        async def iter_any(self):
            yield upstream_sse

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    captured: dict[str, Any] = {}

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure_session)

    body = {
        "model": "claude-opus-4.7",
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    adapter = AnthropicAdapter(_cfg())
    out = b""
    async for c in adapter.stream_messages(body, request_id="req_s"):
        out += c

    # Bytes streamed verbatim from upstream.
    assert out == upstream_sse
    # Adapter captures usage post-stream for DB logging.
    assert adapter.last_stream_usage == {
        "input_tokens": 7,
        "output_tokens": 4,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    # Forwarded body: model rewritten + stream=True.
    assert captured["json"]["model"] == "claude-opus-4-7"
    assert captured["json"]["stream"] is True


def test_registry_returns_anthropic_adapter_for_kind_anthropic():
    """Smoke test: kind: anthropic dispatches to AnthropicAdapter."""
    from serving.servers.registry import _make_adapter

    cfg = {
        "id": "claude-opus-4.7",
        "name": "Claude Opus 4.7",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test",
        "provider_model_id": "claude-opus-4-7",
    }
    adapter = _make_adapter("anthropic", cfg)
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.native_format == "anthropic"
