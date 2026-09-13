"""Tests for the tool-call ``arguments`` an OpenAI-compatible upstream accepts.

sglang and vLLM validate ``function.arguments`` on *every* assistant tool call
in the request, historical ones included, and reject the whole turn with a 400
(``Assistant tool call function.arguments must be valid JSON.`` /
``... must be a JSON object.``). Clients replay the transcript, so one
malformed call wedges that conversation forever -- which is what produced the
bulk of a fortnight of user-visible failures on ``deepseek-v4-flash``.

The expectations below are the upstream's own, verified against a live sglang
node: the three poison shapes seen in production (``"{"``, a fragment missing
its brace, ``""``), plus non-object JSON, are what it rejects; a JSON object
string is what it accepts and must therefore reach it byte-identical.
"""

from __future__ import annotations

import json
import logging
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
        endpoint_id="sglang:gpu-1:30000",
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


def _transcript(arguments: Any) -> list[dict[str, Any]]:
    """Return the replayed transcript shape a poisoned conversation sends."""
    return [
        {"role": "user", "content": "what is the weather"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": arguments},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        {"role": "user", "content": "and tomorrow?"},
    ]


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


def _sent_arguments(sent: list[dict[str, Any]]) -> Any:
    return sent[1]["tool_calls"][0]["function"]["arguments"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "poison"),
    [
        # The three shapes observed in production, in order of frequency.
        ("truncated object", "{"),
        ("fragment missing its leading brace", '"text": "hello"}'),
        ("empty string", ""),
        # Absent/None arguments: not valid either, and cheap to cover here.
        ("none", None),
        # Valid JSON, but not an *object* -- a different upstream 400.
        ("array", "[1, 2]"),
        ("string scalar", '"Beijing"'),
        ("number scalar", "5"),
        ("null literal", "null"),
    ],
)
async def test_malformed_arguments_are_replaced_with_an_empty_object(label, poison):
    sent = await _sent_messages(_adapter(), _transcript(poison))

    assert _sent_arguments(sent) == "{}", label
    # Only ``arguments`` is rewritten: the id and name still pair the call with
    # its result, which the upstream also validates.
    call = sent[1]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_arguments_are_repaired_on_the_streaming_path():
    # Both paths build their payload separately; the repair lives in the one
    # method they share, and this is what proves it.
    sent = await _sent_stream_messages(_adapter(), _transcript("{"))

    assert _sent_arguments(sent) == "{}"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        "{}",
        '{"a": 1}',
        # Whitespace and key order are the client's; re-encoding would change
        # the bytes the upstream (and any prompt cache) sees for no reason.
        '{"city":"Beijing", "unit" : "c"}',
        '{"nested": {"b": [1, 2]}, "s": "}"}',
    ],
)
async def test_valid_object_arguments_pass_through_byte_identical(arguments):
    sent = await _sent_messages(_adapter(), _transcript(arguments))

    assert _sent_arguments(sent) == arguments


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decoded_object_arguments_are_re_encoded():
    # Some clients send the decoded object rather than the JSON string the
    # OpenAI schema asks for. The content is intact, so encode it rather than
    # throwing it away.
    sent = await _sent_messages(_adapter(), _transcript({"city": "Beijing"}))

    assert json.loads(_sent_arguments(sent)) == {"city": "Beijing"}
    assert isinstance(_sent_arguments(sent), str)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_callers_message_list_is_never_mutated():
    # The list handed in is the router's ``self._messages``: it is written
    # verbatim to ``api_logs.prompt`` and re-read on every fallback attempt, so
    # a repair that mutated it would corrupt the request log and change what
    # the next route sees.
    messages = _transcript("{")
    assert _sent_arguments(await _sent_messages(_adapter(), messages)) == "{}"

    assert messages[1]["tool_calls"][0]["function"]["arguments"] == "{"
    assert messages == _transcript("{")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_already_valid_transcript_is_handed_back_unchanged():
    # A no-op pass must not copy: the overwhelming majority of traffic is
    # already well-formed, and the identity check is how callers below tell a
    # repair happened.
    messages = _transcript('{"city": "Beijing"}')
    sent = await _sent_messages(_adapter(), messages)

    # ``_clean_message`` still copies each message dict (it strips the None
    # ``content``), but the tool_calls list and its entries are the originals.
    assert sent[1]["tool_calls"] is messages[1]["tool_calls"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_the_poisoned_tool_call_is_rebuilt():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "ok", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}},
                {"id": "bad", "type": "function", "function": {"name": "g", "arguments": "{"}},
            ],
        },
    ]
    sent = await _sent_messages(_adapter(), messages)

    calls = sent[1]["tool_calls"]
    assert calls[0] is messages[1]["tool_calls"][0]
    assert calls[1]["function"]["arguments"] == "{}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_transcript_without_tool_calls_is_untouched():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]

    assert await _sent_messages(_adapter(), list(messages)) == messages


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_calls",
    [
        # Every shape a permissive northbound schema lets through. None of
        # these may raise: this sits on the request path for every model.
        None,
        [],
        "not-a-list",
        {"id": "call_1"},
        [None],
        ["not-a-dict"],
        [{"id": "call_1"}],
        [{"id": "call_1", "function": None}],
        [{"id": "call_1", "function": "not-a-dict"}],
        [{"function": {"name": "f"}}],
    ],
)
async def test_malformed_tool_call_shapes_do_not_raise(tool_calls):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": tool_calls},
    ]

    sent = await _sent_messages(_adapter(), messages)

    assert sent[0] == {"role": "user", "content": "hi"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_repair_is_logged_without_the_argument_content(caplog):
    # The repair hides the producer's bug from the user; this line is the only
    # remaining evidence that a client is still emitting unparseable calls.
    with caplog.at_level(logging.WARNING, logger="serving.adapters.openai_compat"):
        await _sent_messages(_adapter(), _transcript('{"secret": "hunter2"'))

    records = [
        r for r in caplog.records if getattr(r, "event", None) == "tool_call_arguments_repaired"
    ]
    assert len(records) == 1
    assert records[0].tool_call_id == "call_1"
    assert records[0].tool_name == "get_weather"
    assert records[0].endpoint_id == "sglang:gpu-1:30000"
    # The arguments are user data and must not reach the log in any field.
    assert "hunter2" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_valid_transcript_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="serving.adapters.openai_compat"):
        await _sent_messages(_adapter(), _transcript('{"city": "Beijing"}'))

    assert not [
        r for r in caplog.records if getattr(r, "event", None) == "tool_call_arguments_repaired"
    ]
