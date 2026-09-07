"""Tests for the system-message ordering an OpenAI-compatible upstream accepts.

sglang and vLLM reject a message list carrying more than one ``system``
message, or one that is not first, with ``System message must be at the
beginning`` and a 400 -- the whole turn fails rather than degrading. Clients
posting straight to ``/v1/chat/completions`` produce both shapes, so the
adapter normalizes them on the way out. The assertions are on the body that
actually went upstream, on both the streaming and non-streaming paths, because
each builds its payload separately.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter

_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "42"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


def _adapter(*, provider: str = "sglang") -> OpenAICompatAdapter:
    config = ModelConfig(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        provider=provider,
        base_url="http://mock.local/v1",
        provider_model_id="deepseek-v4-flash",
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


async def _sent_messages(
    adapter: OpenAICompatAdapter, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run a non-streaming completion and return the messages that went upstream."""
    mock_post = AsyncMock(return_value=_RESPONSE)
    adapter._post_with_pool = mock_post
    await adapter.chat_completion(messages)
    return mock_post.call_args.args[1]["messages"]


async def _sent_stream_messages(
    adapter: OpenAICompatAdapter, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run a streaming completion and return the messages that went upstream."""
    captured: dict[str, Any] = {}

    async def _stream_post(*, url, json, headers, timeout):
        captured.update(json)
        yield "data: [DONE]"

    adapter.http.stream_post = _stream_post
    async for _ in adapter.stream_chat_completion(messages):
        pass
    return captured["messages"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_transcript_system_message_is_hoisted():
    # A client re-sending a whole transcript can leave a system message after
    # the first user turn -- the shape the reported 400 came from.
    sent = await _sent_messages(
        _adapter(),
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "again"},
        ],
    )

    assert sent == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_transcript_system_message_is_hoisted_when_streaming():
    sent = await _sent_stream_messages(
        _adapter(),
        [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "be terse"},
        ],
    )

    assert sent == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_several_leading_system_messages_collapse_into_one():
    sent = await _sent_messages(
        _adapter(),
        [
            {"role": "system", "content": "you are a router"},
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
    )

    assert sent == [
        {"role": "system", "content": "you are a router\n\nbe terse"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_accepted_shape_reaches_upstream_untouched():
    # The overwhelming majority of traffic: normalizing must not rewrite it.
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]

    assert await _sent_messages(_adapter(), list(messages)) == messages
    assert await _sent_stream_messages(_adapter(), list(messages)) == messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transcript_without_a_system_message_is_untouched():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    assert await _sent_messages(_adapter(), list(messages)) == messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_transcript_keeps_its_ordering_around_the_hoist():
    # Hoisting must not disturb the assistant/tool adjacency other providers
    # validate: only the system message moves, everything else keeps its order.
    sent = await _sent_messages(
        _adapter(),
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "system", "content": "be terse"},
        ],
    )

    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[2]["tool_calls"][0]["id"] == "call_1"
    assert sent[3]["tool_call_id"] == "call_1"
