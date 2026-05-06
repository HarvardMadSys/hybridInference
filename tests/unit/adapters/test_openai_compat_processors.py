"""Tests for OpenAICompatAdapter processor selection and stream handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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


def _make_chunk(
    *,
    delta: dict,
    finish_reason: str | None = None,
    usage: dict | None = None,
) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "glm-4.7-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}"


def _make_adapter(
    *,
    processor: str | None,
    provider_profile: str | None = None,
    chat_path: str | None = None,
    supported_params: list[str] | None = None,
    include_usage_in_stream: bool = False,
) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="glm-4.7-flash",
        name="GLM-4.7-Flash",
        provider="openai_compat",
        base_url="http://mock.local/v1",
        provider_model_id="glm-4.7-flash",
        processor=processor,
        provider_profile=provider_profile,
        chat_path=chat_path,
        supported_params=supported_params or ["temperature", "top_p", "max_tokens"],
        include_usage_in_stream=include_usage_in_stream,
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

    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]

    assert chunks[-1] == "data: [DONE]\n\n"

    payloads = [json.loads(chunk[6:]) for chunk in chunks[:-1]]
    content = "".join(
        payload["choices"][0]["delta"].get("content", "")
        for payload in payloads
        if payload.get("choices")
    )

    assert content == "hi"
    assert any(
        payload["choices"][0].get("delta", {}).get("reasoning_content")
        for payload in payloads
        if payload.get("choices")
    )
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"]["completion_tokens"] > 0


# --- include_usage_in_stream capability gate tests ---


@pytest.mark.asyncio
async def test_default_profile_streaming_omits_stream_options_by_default():
    """Regression: DEFAULT profile must NOT inject stream_options unless route opts in.

    Some upstreams (Ollama, Chutes, Featherless, openai_compat) strictly validate
    the request body and reject unknown fields. The capability is opt-in per route.
    """

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"content": "hi"})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor=None, provider_profile=None)
    adapter.http.stream_post = MagicMock(side_effect=fake_stream_post)

    _ = [c async for c in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])]

    stream_payload = adapter.http.stream_post.call_args.kwargs["json"]
    assert "stream_options" not in stream_payload


@pytest.mark.asyncio
async def test_default_profile_streaming_includes_stream_options_when_opted_in():
    """When include_usage_in_stream=True, DEFAULT profile sends stream_options."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"content": "hi"})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor=None, provider_profile=None, include_usage_in_stream=True)
    adapter.http.stream_post = MagicMock(side_effect=fake_stream_post)

    _ = [c async for c in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])]

    stream_payload = adapter.http.stream_post.call_args.kwargs["json"]
    assert stream_payload["stream_options"] == {"include_usage": True}


# --- DeepSeek profile usage normalization tests ---


@pytest.mark.asyncio
async def test_deepseek_profile_non_streaming_usage_normalizes_cache_fields():
    """DeepSeek profile converts prompt_cache_hit/miss_tokens to cache_read_tokens and prompt_tokens."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 10,
            "total_tokens": 210,
            "prompt_cache_hit_tokens": 150,
            "prompt_cache_miss_tokens": 50,
        },
    }
    config = ModelConfig(
        id="deepseek-chat",
        name="DeepSeek Chat",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        provider_model_id="deepseek-chat",
        provider_profile="deepseek",
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = AsyncMock(return_value=response)

    result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    usage = result["usage"]
    # Cache-inclusive: keep upstream prompt_tokens (200 = 150 cache_hit + 50 cache_miss)
    assert usage["prompt_tokens"] == 200
    assert usage["cache_read_tokens"] == 150
    assert usage["completion_tokens"] == 10
    assert usage["total_tokens"] == 210


@pytest.mark.asyncio
async def test_deepseek_profile_streaming_usage_normalizes_cache_fields():
    """Streaming final chunk uses normalized DeepSeek usage, not raw prompt_cache_* fields."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"content": "hi"})
        yield _make_chunk(
            delta={},
            finish_reason="stop",
            usage={
                "prompt_tokens": 200,
                "completion_tokens": 2,
                "total_tokens": 202,
                "prompt_cache_hit_tokens": 180,
                "prompt_cache_miss_tokens": 20,
            },
        )
        yield "data: [DONE]"

    config = ModelConfig(
        id="deepseek-chat",
        name="DeepSeek Chat",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        provider_model_id="deepseek-chat",
        provider_profile="deepseek",
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter.http.stream_post = fake_stream_post

    chunks = [c async for c in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])]

    final_payloads = [
        json.loads(c[6:]) for c in chunks[:-1] if c.startswith("data: ") and c != "data: [DONE]\n\n"
    ]
    usage_chunks = [p for p in final_payloads if "usage" in p]
    assert len(usage_chunks) >= 1
    usage = usage_chunks[-1]["usage"]
    # Cache-inclusive: keep upstream prompt_tokens (200 = 180 cache_hit + 20 cache_miss)
    assert usage["prompt_tokens"] == 200
    assert usage["cache_read_tokens"] == 180
    assert "prompt_cache_hit_tokens" not in usage
    assert "prompt_cache_miss_tokens" not in usage


@pytest.mark.asyncio
async def test_default_profile_preserves_standard_usage():
    """Default profile passes through standard OpenAI usage unchanged."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
        },
    }
    adapter = _make_adapter(processor=None, provider_profile=None)
    adapter.http.json_post_with_retry = AsyncMock(return_value=response)

    result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    usage = result["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 105


@pytest.mark.asyncio
async def test_deepseek_profile_response_format_only_json_object_no_guided_json():
    """DeepSeek profile forwards only {type: json_object}, never schema/guided_json."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    config = ModelConfig(
        id="deepseek-chat",
        name="DeepSeek Chat",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        provider_model_id="deepseek-chat",
        provider_profile="deepseek",
        supports_structured_output=True,
    )
    adapter = OpenAICompatAdapter(config)
    mock_post = AsyncMock(return_value=response)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = mock_post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_object",
            "schema": {"type": "object", "properties": {"x": {}}},
        },
    )

    call_kwargs = mock_post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert "guided_json" not in payload
    assert "schema" not in payload.get("response_format", {})


@pytest.mark.asyncio
async def test_deepseek_profile_omits_non_json_object_response_format():
    """DeepSeek profile omits response_format when type is not json_object."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    config = ModelConfig(
        id="deepseek-chat",
        name="DeepSeek Chat",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        provider_model_id="deepseek-chat",
        provider_profile="deepseek",
        supports_structured_output=True,
    )
    adapter = OpenAICompatAdapter(config)
    mock_post = AsyncMock(return_value=response)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = mock_post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema", "schema": {"type": "object"}},
    )

    payload = mock_post.call_args.kwargs["json"]
    assert "response_format" not in payload


@pytest.mark.asyncio
async def test_zai_profile_uses_chat_path_and_forwards_supported_extra_params():
    """ZAI-compatible routes can override the chat path and pass through GLM params."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    config = ModelConfig(
        id="glm-5",
        name="GLM-5",
        provider="zai",
        base_url="https://api.z.ai/api/coding/paas/v4",
        provider_model_id="glm-5",
        provider_profile="zai",
        chat_path="/chat/completions",
        supported_params=[
            "temperature",
            "top_p",
            "max_tokens",
            "thinking",
            "tool_stream",
        ],
    )
    adapter = OpenAICompatAdapter(config)
    mock_post = AsyncMock(return_value=response)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = mock_post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        thinking={"type": "enabled"},
        tool_stream=True,
    )

    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert call_kwargs["json"]["thinking"] == {"type": "enabled"}
    assert call_kwargs["json"]["tool_stream"] is True


@pytest.mark.asyncio
async def test_reasoning_effort_forwarded_when_supported():
    response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-5.5",
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    config = ModelConfig(
        id="gpt-5.5",
        name="GPT-5.5",
        provider="cliproxy",
        base_url="http://cliproxy.local/v1",
        provider_model_id="gpt-5.5",
        supported_params=["max_tokens", "reasoning_effort"],
    )
    adapter = OpenAICompatAdapter(config)
    mock_post = AsyncMock(return_value=response)
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = mock_post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        reasoning_effort="high",
    )

    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["url"] == "http://cliproxy.local/v1/chat/completions"
    assert call_kwargs["json"]["reasoning_effort"] == "high"


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
        assert isinstance(get_processor("minimax-m2.7"), ThinkBlockProcessor)

    def test_unknown_model_returns_default(self):
        assert isinstance(get_processor("some-random-model"), DefaultProcessor)


class TestGetProcessorOverride:
    """Explicit override bypasses auto-detection."""

    def test_override_default(self):
        assert isinstance(get_processor("glm-4.7-flash", override="default"), DefaultProcessor)

    def test_override_glm(self):
        assert isinstance(get_processor("some-model", override="glm"), GLMProcessor)

    def test_override_qwen_coder(self):
        assert isinstance(get_processor(None, override="qwen_coder"), QwenCoderProcessor)

    def test_override_think_block(self):
        assert isinstance(get_processor(None, override="think_block"), ThinkBlockProcessor)

    def test_invalid_override_raises(self):
        with pytest.raises(ValueError, match="Unknown processor override"):
            get_processor("glm-4.7-flash", override="nonexistent")
