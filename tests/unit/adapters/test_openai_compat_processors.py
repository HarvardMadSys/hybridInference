"""Tests for OpenAICompatAdapter processor selection and stream handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.key_pool import KeyPoolExhausted
from serving.adapters.openai_compat import OpenAICompatAdapter, _key_pool_provider_label
from serving.adapters.processors import (
    DefaultProcessor,
    GLMProcessor,
    QwenCoderProcessor,
    ThinkBlockProcessor,
    get_processor,
)
from serving.utils.tokens import estimate_text_tokens


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
    extra_body: dict | None = None,
    api_keys: list[str] | None = None,
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
        extra_body=extra_body or {},
        api_keys=api_keys,
        include_usage_in_stream=include_usage_in_stream,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


def test_key_pool_provider_label_falls_back_to_route_metadata_and_base_url():
    metadata_config = ModelConfig(
        id="minimax-fast",
        name="MiniMax Fast",
        provider="",
        base_url="https://openrouter.ai/api/v1",
        provider_model_id="minimax/minimax-m2.5",
        route_metadata={"key_provider": "openrouter"},
    )
    assert _key_pool_provider_label(metadata_config) == "openrouter"

    host_config = ModelConfig(
        id="minimax-fast",
        name="MiniMax Fast",
        provider="",
        base_url="https://api.featherless.ai/v1",
        provider_model_id="MiniMaxAI/MiniMax-M2.5",
    )
    assert _key_pool_provider_label(host_config) == "featherless"

    malformed_config = ModelConfig(
        id="minimax-fast",
        name="MiniMax Fast",
        provider="",
        base_url="http://[broken",
        provider_model_id="MiniMaxAI/MiniMax-M2.5",
        endpoint_id="minimax-fast:broken-api",
    )
    assert _key_pool_provider_label(malformed_config) == "minimax-fast:broken-api"


@pytest.mark.asyncio
async def test_post_with_pool_raises_clear_error_when_pool_empty():
    adapter = _make_adapter(processor="default", api_keys=["sk-one"])
    assert adapter._key_pool is not None
    adapter._key_pool.remove_key("sk-one")

    with pytest.raises(KeyPoolExhausted, match="No active API keys"):
        await adapter._post_with_pool(
            "http://mock.local/v1/chat/completions",
            {"model": "glm-4.7-flash"},
        )


@pytest.mark.asyncio
async def test_stream_with_pool_raises_clear_error_when_pool_empty():
    adapter = _make_adapter(processor="default", api_keys=["sk-one"])
    assert adapter._key_pool is not None
    adapter._key_pool.remove_key("sk-one")

    with pytest.raises(KeyPoolExhausted, match="No active API keys"):
        async for _ in adapter._open_stream_with_pool(
            "http://mock.local/v1/chat/completions",
            {"model": "glm-4.7-flash"},
        ):
            pass


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


@pytest.mark.asyncio
async def test_streaming_default_processor_preserves_openrouter_reasoning_delta():
    """OpenRouter MiniMax streams reasoning in delta.reasoning before content."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"role": "assistant", "content": ""})
        yield _make_chunk(delta={"reasoning": "Thinking..."})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="default")
    adapter.http.stream_post = fake_stream_post

    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]

    payloads = [json.loads(chunk[6:]) for chunk in chunks[:-1]]
    assert any(
        payload["choices"][0].get("delta", {}).get("reasoning") == "Thinking..."
        for payload in payloads
        if payload.get("choices")
    )


@pytest.mark.asyncio
async def test_streaming_think_block_processor_preserves_openrouter_reasoning_delta():
    """MiniMax auto processor must not drop native OpenRouter reasoning fields."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"role": "assistant", "content": ""})
        yield _make_chunk(delta={"reasoning": "Thinking..."})
        yield _make_chunk(delta={"thinking": "Still thinking..."})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="think_block")
    adapter.http.stream_post = fake_stream_post

    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]

    payloads = [json.loads(chunk[6:]) for chunk in chunks[:-1]]
    deltas = [
        payload["choices"][0].get("delta", {}) for payload in payloads if payload.get("choices")
    ]
    assert any(delta.get("reasoning") == "Thinking..." for delta in deltas)
    assert any(delta.get("thinking") == "Still thinking..." for delta in deltas)


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


# --- fallback usage for tool-call-only streams (no upstream usage chunk) ---


def _final_usage_from_chunks(chunks: list[str]) -> dict:
    """Extract the usage dict from the streamed final chunk."""
    payloads = [
        json.loads(c[6:]) for c in chunks if c.startswith("data: ") and c.strip() != "data: [DONE]"
    ]
    usage_chunks = [p for p in payloads if p.get("usage")]
    assert usage_chunks, "expected a final chunk carrying usage"
    return usage_chunks[-1]["usage"]


@pytest.mark.asyncio
async def test_streaming_native_tool_calls_only_reports_nonzero_completion_tokens():
    """A tool-call-only stream with NO upstream usage must estimate tool tokens.

    Regression: tool-call deltas were never accumulated, so the fallback
    estimate saw an empty string and reported completion_tokens=0.
    """

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            }
        )
        yield _make_chunk(
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '{"city": "Paris", "unit": "celsius"}'},
                    }
                ],
            }
        )
        yield _make_chunk(delta={}, finish_reason="tool_calls")
        yield "data: [DONE]"

    # No include_usage_in_stream => provider sends no usage chunk => fallback path.
    adapter = _make_adapter(processor="default", provider_profile=None)
    adapter.http.stream_post = fake_stream_post

    chunks = [
        c async for c in adapter.stream_chat_completion([{"role": "user", "content": "weather?"}])
    ]

    usage = _final_usage_from_chunks(chunks)
    # Exact equality catches double-counting regressions: the accumulated tool
    # text is the concatenated function name + argument deltas, nothing more.
    assert usage["completion_tokens"] == estimate_text_tokens(
        'get_weather{"city": "Paris", "unit": "celsius"}'
    )
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


@pytest.mark.asyncio
async def test_streaming_glm_xml_flushed_tool_call_reports_nonzero_completion_tokens():
    """GLM XML tool calls surfaced by processor.flush() reach the fallback estimate.

    The GLMProcessor buffers `<tool_call>` XML and only emits the converted
    tool_calls chunk from flush() at end of stream; that chunk must still be
    accumulated for the fallback usage estimate when upstream sends no usage.
    """

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"role": "assistant", "content": "<tool_call>get_weather\n"})
        yield _make_chunk(
            delta={"content": "<arg_key>city</arg_key>\n<arg_value>Paris</arg_value>\n"}
        )
        yield _make_chunk(delta={"content": "</tool_call>"})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="glm", provider_profile=None)
    adapter.http.stream_post = fake_stream_post

    chunks = [
        c async for c in adapter.stream_chat_completion([{"role": "user", "content": "weather?"}])
    ]

    usage = _final_usage_from_chunks(chunks)
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_build_fallback_usage_counts_tool_text():
    """`_build_fallback_usage` adds tool-text tokens to the content estimate."""
    adapter = _make_adapter(processor="default", provider_profile=None)

    messages = [{"role": "user", "content": "hi"}]
    content_only = adapter._build_fallback_usage(
        messages=messages,
        total_content="",
        prompt_tokens_override=None,
    )
    tool_text = 'get_weather{"city": "Paris", "unit": "celsius"}'
    with_tools = adapter._build_fallback_usage(
        messages=messages,
        total_content="",
        prompt_tokens_override=None,
        tool_text=tool_text,
    )

    assert content_only["completion_tokens"] == 0
    # Exact equality catches double-counting regressions.
    assert with_tools["completion_tokens"] == estimate_text_tokens(tool_text)
    assert with_tools["total_tokens"] == (
        with_tools["prompt_tokens"] + with_tools["completion_tokens"]
    )


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
async def test_minimax_streaming_usage_survives_think_block_processor():
    """MiniMax final usage chunk must survive think-block stripping.

    Regression: MiniMax streams can end with an empty-delta chunk that carries
    usage only. If the ThinkBlockProcessor drops that chunk, the adapter falls
    back to estimated usage and recent requests lose cached token accounting.
    """

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(delta={"content": "visible answer"})
        yield _make_chunk(
            delta={},
            finish_reason="stop",
            usage={
                "prompt_tokens": 200,
                "completion_tokens": 12,
                "total_tokens": 212,
                "input_tokens_details": {"cached_tokens": 80},
            },
        )
        yield "data: [DONE]"

    config = ModelConfig(
        id="minimax-m2.7",
        name="MiniMax M2.7",
        provider="minimax",
        base_url="https://api.minimax.io/v1",
        provider_model_id="minimax-m2.7",
        provider_profile="minimax",
        include_usage_in_stream=True,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter.http.stream_post = fake_stream_post

    chunks = [c async for c in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])]

    payloads = [
        json.loads(c[6:]) for c in chunks[:-1] if c.startswith("data: ") and c != "data: [DONE]\n\n"
    ]
    usage_chunks = [p for p in payloads if "usage" in p]
    assert len(usage_chunks) >= 1

    usage = usage_chunks[-1]["usage"]
    assert usage["prompt_tokens"] == 200
    assert usage["completion_tokens"] == 12
    assert usage["cache_read_tokens"] == 80
    assert "input_tokens_details" not in usage


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


@pytest.mark.asyncio
async def test_extra_body_defaults_are_forwarded_to_non_streaming_requests():
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "42"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }
    adapter = _make_adapter(
        processor="default",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    mock_post = AsyncMock(return_value=response)
    adapter._post_with_pool = mock_post

    await adapter.chat_completion(
        [{"role": "user", "content": "What is 17 + 25?"}],
        temperature=0,
    )

    payload = mock_post.call_args.args[1]
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["temperature"] == 0


@pytest.mark.asyncio
async def test_extra_body_defaults_are_forwarded_to_streaming_requests():
    captured_payload: dict = {}

    async def fake_stream_post(*, url, json, headers, timeout):
        captured_payload.update(json)
        yield _make_chunk(delta={"role": "assistant", "content": "42"})
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(
        processor="default",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    adapter.http.stream_post = fake_stream_post

    chunks = [
        chunk
        async for chunk in adapter.stream_chat_completion(
            [{"role": "user", "content": "What is 17 + 25?"}],
            temperature=0,
        )
    ]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert captured_payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured_payload["stream"] is True
    assert captured_payload["temperature"] == 0


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


@pytest.mark.asyncio
async def test_streaming_tool_call_truncated_at_max_tokens_keeps_length_finish():
    """C13: saw_tool_calls must not overwrite a 'length' finish_reason as 'tool_calls'."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "write", "arguments": '{"a":'}}
                ],
            }
        )
        yield _make_chunk(delta={}, finish_reason="length")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="default")
    adapter.http.stream_post = fake_stream_post

    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]
    payloads = [json.loads(chunk[6:]) for chunk in chunks if chunk.strip() != "data: [DONE]"]
    final = payloads[-1]
    assert final["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_streaming_tool_call_with_stop_finish_normalized_to_tool_calls():
    """C13: the known 'provider streams tool_calls but reports stop' quirk still normalizes."""

    async def fake_stream_post(*args, **kwargs):
        yield _make_chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "write", "arguments": "{}"}}
                ],
            }
        )
        yield _make_chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    adapter = _make_adapter(processor="default")
    adapter.http.stream_post = fake_stream_post

    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]
    payloads = [json.loads(chunk[6:]) for chunk in chunks if chunk.strip() != "data: [DONE]"]
    final = payloads[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_single_key_completion_does_not_retry():
    """P2: the no-pool completion POST uses retries=1 (one attempt), never re-sending a generation."""
    from unittest.mock import AsyncMock

    adapter = _make_adapter(processor="default")  # no api_keys -> single-key path
    response = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "glm-4.7-flash",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    mock = AsyncMock(return_value=response)
    adapter.http.json_post_with_retry = mock
    await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert mock.await_count == 1
    assert mock.call_args.kwargs["retries"] == 1
