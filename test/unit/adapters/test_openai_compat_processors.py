"""Tests for OpenAICompatAdapter processor selection and stream handling."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.processors import (
    DefaultProcessor,
    GLMProcessor,
    QwenCoderProcessor,
    ThinkBlockProcessor,
    get_processor,
)


def _make_chunk(*, delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "glm-4.7-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}"


def _make_adapter(*, processor: str | None) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="glm-4.7-flash",
        name="GLM-4.7-Flash",
        provider="openai_compat",
        base_url="http://mock.local/v1",
        provider_model_id="glm-4.7-flash",
        processor=processor,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_streaming_default_processor_preserves_content_after_reasoning_only_chunks():
    """A route-level default override should avoid GLM XML buffering on vLLM streams."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"role": "assistant", "content": "", "reasoning_content": None})
        yield _make_chunk(delta={"reasoning_content": "Let me think step by step..."})
        yield _make_chunk(delta={"content": "hi"})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="default")
    adapter.http.stream_post = fake_stream_post

    chunks = [chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])]

    assert chunks[-1] == "data: [DONE]\n\n"

    payloads = [json.loads(chunk[6:]) for chunk in chunks[:-1]]
    content = "".join(
        payload["choices"][0]["delta"].get("content", "")
        for payload in payloads
        if payload.get("choices")
    )

    assert content == "hi"
    assert all(
        "reasoning_content" not in payload["choices"][0].get("delta", {})
        for payload in payloads
        if payload.get("choices")
    )
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"]["completion_tokens"] > 0


# --- get_processor factory tests ---


class TestGetProcessorAutoDetect:
    """GLM models should no longer auto-select GLMProcessor."""

    def test_glm_model_returns_default_processor(self):
        assert isinstance(get_processor("glm-4.7-flash"), DefaultProcessor)

    def test_glm5_model_returns_default_processor(self):
        assert isinstance(get_processor("glm-5"), DefaultProcessor)

    def test_qwen_coder_still_auto_detected(self):
        assert isinstance(get_processor("qwen3-coder-30b"), QwenCoderProcessor)

    def test_minimax_still_auto_detected(self):
        assert isinstance(get_processor("minimax-m2"), ThinkBlockProcessor)

    def test_unknown_model_returns_default(self):
        assert isinstance(get_processor("llama-3.3-70b"), DefaultProcessor)


class TestGetProcessorOverride:
    """Explicit override bypasses auto-detection."""

    def test_override_default(self):
        assert isinstance(get_processor("glm-4.7-flash", override="default"), DefaultProcessor)

    def test_override_glm(self):
        assert isinstance(get_processor("llama-3.3-70b", override="glm"), GLMProcessor)

    def test_override_qwen_coder(self):
        assert isinstance(get_processor(None, override="qwen_coder"), QwenCoderProcessor)

    def test_override_think_block(self):
        assert isinstance(get_processor(None, override="think_block"), ThinkBlockProcessor)

    def test_invalid_override_raises(self):
        with pytest.raises(ValueError, match="Unknown processor override"):
            get_processor("glm-4.7-flash", override="nonexistent")
