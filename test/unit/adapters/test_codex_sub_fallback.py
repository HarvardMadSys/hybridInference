"""Tests for CodexSubscriptionAdapter fallback reasoning_effort handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.codex_sub import CodexSubscriptionAdapter


def _make_adapter() -> CodexSubscriptionAdapter:
    """Create a minimally configured adapter for testing fallback paths."""
    cfg = ModelConfig(
        id="gpt-5.1-codex",
        name="GPT-5.1 Codex",
        provider="codex_sub",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    adapter = CodexSubscriptionAdapter(cfg)
    adapter._initialized = True
    adapter._fallback_api_key = "sk-test-fallback"
    return adapter


_MOCK_CHAT_RESPONSE = {
    "id": "chatcmpl-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


class TestFallbackReasoningEffort:
    """Verify reasoning_effort is correctly forwarded in fallback paths."""

    @pytest.mark.asyncio
    async def test_fallback_chat_with_reasoning_excludes_temperature(self):
        adapter = _make_adapter()
        adapter.http = AsyncMock()
        adapter.http.json_post = AsyncMock(return_value=_MOCK_CHAT_RESPONSE)

        await adapter._fallback_chat(
            [{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            temperature=0.8,
        )

        payload = adapter.http.json_post.call_args.kwargs["json"]
        assert payload["reasoning_effort"] == "medium"
        assert "temperature" not in payload

    @pytest.mark.asyncio
    async def test_fallback_chat_without_reasoning_passes_temperature(self):
        adapter = _make_adapter()
        adapter.http = AsyncMock()
        adapter.http.json_post = AsyncMock(return_value=_MOCK_CHAT_RESPONSE)

        await adapter._fallback_chat(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
        )

        payload = adapter.http.json_post.call_args.kwargs["json"]
        assert payload["temperature"] == 0.5
        assert "reasoning_effort" not in payload

    @pytest.mark.asyncio
    async def test_fallback_chat_reasoning_none_passes_temperature(self):
        adapter = _make_adapter()
        adapter.http = AsyncMock()
        adapter.http.json_post = AsyncMock(return_value=_MOCK_CHAT_RESPONSE)

        await adapter._fallback_chat(
            [{"role": "user", "content": "hi"}],
            reasoning_effort="none",
            temperature=0.7,
        )

        payload = adapter.http.json_post.call_args.kwargs["json"]
        assert payload["temperature"] == 0.7
        assert "reasoning_effort" not in payload

    @pytest.mark.asyncio
    async def test_fallback_stream_with_reasoning_excludes_temperature(self):
        """Verify payload construction in _fallback_stream."""
        adapter = _make_adapter()
        captured_payload = {}

        async def fake_stream_post(*args, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            # Yield a [DONE] sentinel to end the stream loop cleanly
            yield "data: [DONE]"

        adapter.http = AsyncMock()
        adapter.http.stream_post = fake_stream_post

        # Consume the generator
        async for _ in adapter._fallback_stream(
            [{"role": "user", "content": "hi"}],
            reasoning_effort="high",
            temperature=1.0,
        ):
            pass

        assert captured_payload["reasoning_effort"] == "high"
        assert "temperature" not in captured_payload

    @pytest.mark.asyncio
    async def test_fallback_stream_without_reasoning_passes_temperature(self):
        adapter = _make_adapter()
        captured_payload = {}

        async def fake_stream_post(*args, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            yield "data: [DONE]"

        adapter.http = AsyncMock()
        adapter.http.stream_post = fake_stream_post

        async for _ in adapter._fallback_stream(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
        ):
            pass

        assert captured_payload["temperature"] == 0.5
        assert "reasoning_effort" not in captured_payload

    @pytest.mark.asyncio
    async def test_fallback_stream_reasoning_none_passes_temperature(self):
        adapter = _make_adapter()
        captured_payload = {}

        async def fake_stream_post(*args, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            yield "data: [DONE]"

        adapter.http = AsyncMock()
        adapter.http.stream_post = fake_stream_post

        async for _ in adapter._fallback_stream(
            [{"role": "user", "content": "hi"}],
            reasoning_effort="none",
            temperature=0.7,
        ):
            pass

        assert captured_payload["temperature"] == 0.7
        assert "reasoning_effort" not in captured_payload
