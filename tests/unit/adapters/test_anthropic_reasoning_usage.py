"""The reasoning count an OpenAI-compatible upstream reports survives /v1/messages.

Anthropic usage has no reasoning field and no total, so the translators keep
the upstream's own counts in an internal record that the adapter hands to the
router through ``usage_sink``; the Anthropic events and response stay
unchanged in shape. Nothing here derives a count from thinking text.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from serving.adapters.anthropic_translator import (
    OPENAI_USAGE_SINK_KEY,
    OpenAIToAnthropicStreamTranslator,
    anthropic_output_tokens,
    openai_response_to_anthropic,
    openai_usage_record,
)
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.adapters.claude_format import parse_usage
from serving.adapters.profiles import normalize_usage_default

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

ANTHROPIC_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}
THINKING = "step one, step two, step three " * 10


# --- the usage record ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "reasoning"),
    [
        ({"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}, None),
        ({"prompt_tokens": 5, "completion_tokens": 7, "reasoning_tokens": 0}, 0),
        ({"prompt_tokens": 5, "completion_tokens": 7, "reasoning_tokens": 4}, 4),
        ({"completion_tokens_details": {"reasoning_tokens": 0}}, 0),
        ({"completion_tokens_details": {"reasoning_tokens": 6}}, 6),
        ({"reasoning_tokens": -3}, None),
        ({"reasoning_tokens": "many"}, None),
        ({"reasoning_tokens": None}, None),
    ],
    ids=[
        "unreported",
        "zero",
        "top-level",
        "nested-zero",
        "nested",
        "negative",
        "text",
        "null",
    ],
)
def test_the_record_keeps_only_a_reported_reasoning_count(usage, reasoning):
    assert openai_usage_record(usage)["reasoning_tokens"] == reasoning


def test_the_record_keeps_the_reported_total_and_cache_counts():
    record = openai_usage_record(
        {
            "prompt_tokens": 900,
            "completion_tokens": 40,
            "total_tokens": 940,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert record == {
        "prompt_tokens": 900,
        "completion_tokens": 40,
        "total_tokens": 940,
        "reasoning_tokens": None,
        "cache_read_tokens": 800,
    }
    assert openai_usage_record({"prompt_tokens": 1})["total_tokens"] is None


@pytest.mark.parametrize(
    ("usage", "output"),
    [
        # DeepSeek / OpenAI: total = prompt + completion proves inclusion.
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 30,
                "total_tokens": 40,
                "reasoning_tokens": 20,
            },
            30,
        ),
        # Gemini: total = prompt + completion + reasoning proves exclusion.
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 30,
                "total_tokens": 60,
                "reasoning_tokens": 20,
            },
            50,
        ),
        # No total: the billing rule's conservative default, shared with chat.
        ({"prompt_tokens": 10, "completion_tokens": 30, "reasoning_tokens": 20}, 50),
        ({"prompt_tokens": 10, "completion_tokens": 30, "reasoning_tokens": 0}, 30),
        ({"prompt_tokens": 10, "completion_tokens": 30}, 30),
    ],
    ids=["included", "excluded", "no-total", "zero", "unreported"],
)
def test_anthropic_output_counts_reasoning_exactly_once(usage, output):
    assert anthropic_output_tokens(openai_usage_record(usage)) == output


# --- the translators --------------------------------------------------------------------


def _response(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x",
        "choices": [
            {
                "message": {"role": "assistant", "reasoning_content": THINKING, "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def test_the_non_streaming_response_stays_anthropic_shaped():
    out = openai_response_to_anthropic(
        _response(
            {
                "prompt_tokens": 10,
                "completion_tokens": 30,
                "total_tokens": 60,
                "reasoning_tokens": 20,
            }
        ),
        model="m",
    )
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 50}
    assert "reasoning_tokens" not in json.dumps(out)


def _chunk(delta: dict[str, Any] | None = None, **extra: Any) -> bytes:
    payload: dict[str, Any] = {"choices": [{"index": 0, "delta": delta or {}}] if delta else []}
    payload.update(extra)
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_the_stream_translator_has_no_record_until_usage_arrives():
    translator = OpenAIToAnthropicStreamTranslator(model="m", input_tokens_estimate=99)
    list(translator.feed(_chunk({"reasoning_content": THINKING})))

    # The seeded input estimate is not a reported count.
    assert translator.openai_usage is None


def test_the_stream_translator_merges_usage_split_across_chunks():
    translator = OpenAIToAnthropicStreamTranslator(model="m")
    wire = b"".join(
        [
            *translator.feed(_chunk({"reasoning_content": THINKING})),
            *translator.feed(_chunk(usage={"prompt_tokens": 10, "total_tokens": 60})),
            *translator.feed(
                _chunk(
                    usage={
                        "completion_tokens": 30,
                        "completion_tokens_details": {"reasoning_tokens": 20},
                    }
                )
            ),
            *translator.finalize(),
        ]
    )

    assert translator.openai_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 30,
        "total_tokens": 60,
        "reasoning_tokens": 20,
    }
    delta = next(
        json.loads(line[len(b"data: ") :])
        for line in wire.split(b"\n")
        if b'"message_delta"' in line
    )
    assert set(delta["usage"]) <= ANTHROPIC_USAGE_KEYS
    assert delta["usage"]["output_tokens"] == 50
    assert b"reasoning_tokens" not in wire
    assert b"total_tokens" not in wire


def test_a_reasoning_count_after_the_completion_count_still_reaches_output():
    translator = OpenAIToAnthropicStreamTranslator(model="m")
    for chunk in (
        _chunk({"content": "hi"}),
        _chunk(usage={"prompt_tokens": 10, "completion_tokens": 30}),
        _chunk(usage={"total_tokens": 60, "reasoning_tokens": 20}),
    ):
        list(translator.feed(chunk))

    assert translator.usage["output_tokens"] == 50
    assert translator.openai_usage["reasoning_tokens"] == 20


# --- the adapter's sink -----------------------------------------------------------------


class _Adapter(BaseAdapter):
    """An OpenAI-native adapter whose upstream is a scripted list of SSE chunks."""

    def __init__(self, chunks: list[bytes | Exception], response: dict[str, Any] | None = None):
        super().__init__(ModelConfig(id="m", name="m", provider="p", base_url="http://x"))
        self._chunks = chunks
        self._response = response or {}

    async def chat_completion(self, messages, **params) -> dict[str, Any]:
        return self._response

    async def stream_chat_completion(self, messages, **params) -> AsyncGenerator[str, None]:
        for item in self._chunks:
            if isinstance(item, Exception):
                raise item
            yield item.decode()


BODY = {"model": "m", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
USAGE = {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40, "reasoning_tokens": 20}


def _stream_chunks(usage: dict[str, Any] | None = USAGE) -> list[bytes | Exception]:
    chunks: list[bytes | Exception] = [
        _chunk({"reasoning_content": THINKING}),
        _chunk({"content": "answer"}),
    ]
    if usage is not None:
        chunks.append(_chunk(usage=usage))
    return chunks


@pytest.mark.asyncio
async def test_the_sink_has_the_record_before_the_terminal_events_are_sent():
    """A client that leaves while message_delta is in flight still gets exact usage."""
    sink: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    stream = _Adapter(_stream_chunks()).stream_messages(BODY, request_id="r", usage_sink=sink)
    async for frame in stream:
        if b"message_delta" in frame:
            break
    await stream.aclose()

    assert sink[OPENAI_USAGE_SINK_KEY]["reasoning_tokens"] == 20
    assert (sink["input_tokens"], sink["output_tokens"]) == (10, 30)


@pytest.mark.asyncio
async def test_a_stream_closed_before_usage_arrives_leaves_the_sink_unflushed():
    sink: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    stream = _Adapter(_stream_chunks()).stream_messages(BODY, request_id="r", usage_sink=sink)
    async for frame in stream:
        if b"thinking_delta" in frame:
            break
    await stream.aclose()

    # The router recovers this case by estimation; nothing was reported.
    assert sink == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.asyncio
async def test_a_failed_stream_does_not_flush_usage_it_saw():
    sink: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    chunks = [*_stream_chunks(), RuntimeError("upstream reset")]
    with pytest.raises(RuntimeError):
        async for _ in _Adapter(chunks).stream_messages(BODY, request_id="r", usage_sink=sink):
            pass

    assert OPENAI_USAGE_SINK_KEY not in sink


@pytest.mark.asyncio
async def test_a_stream_without_usage_reports_no_record():
    sink: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    async for _ in _Adapter(_stream_chunks(None)).stream_messages(
        BODY, request_id="r", usage_sink=sink
    ):
        pass

    assert OPENAI_USAGE_SINK_KEY not in sink


@pytest.mark.asyncio
async def test_non_streaming_messages_hand_the_record_to_the_sink_only():
    adapter = _Adapter([], response=_response(USAGE))
    sink: dict[str, Any] = {}

    resp = await adapter.messages(BODY, request_id="r", usage_sink=sink)

    assert set(resp["usage"]) <= ANTHROPIC_USAGE_KEYS
    assert OPENAI_USAGE_SINK_KEY not in resp
    assert sink == {
        OPENAI_USAGE_SINK_KEY: {
            "prompt_tokens": 10,
            "completion_tokens": 30,
            "total_tokens": 40,
            "reasoning_tokens": 20,
        }
    }
    # Without a sink the call is unchanged.
    assert await adapter.messages(BODY, request_id="r") == resp


@pytest.mark.asyncio
async def test_each_call_fills_only_its_own_sink():
    first: dict[str, Any] = {}
    second: dict[str, Any] = {}
    await _Adapter([], response=_response(USAGE)).messages(BODY, request_id="a", usage_sink=first)
    await _Adapter([], response=_response({"prompt_tokens": 1, "completion_tokens": 2})).messages(
        BODY, request_id="b", usage_sink=second
    )

    assert first[OPENAI_USAGE_SINK_KEY]["reasoning_tokens"] == 20
    assert second[OPENAI_USAGE_SINK_KEY]["reasoning_tokens"] is None


# --- a reported zero is not "unreported" -------------------------------------------------


def test_usage_info_emits_a_reported_zero_and_omits_an_unreported_count():
    assert UsageInfo(reasoning_reported=True).to_dict()["reasoning_tokens"] == 0
    assert "reasoning_tokens" not in UsageInfo().to_dict()
    assert UsageInfo(reasoning_tokens=5).to_dict()["reasoning_tokens"] == 5


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"completion_tokens_details": {"reasoning_tokens": 0}}, 0),
        ({"completion_tokens_details": {"reasoning_tokens": 9}}, 9),
        ({"completion_tokens_details": {"reasoning_tokens": -1}}, None),
        ({}, None),
    ],
    ids=["zero", "positive", "negative", "absent"],
)
def test_the_default_profile_keeps_a_reported_zero(usage, expected):
    out = normalize_usage_default({"prompt_tokens": 1, "completion_tokens": 2, **usage}).to_dict()
    assert out.get("reasoning_tokens") == expected
    assert ("reasoning_tokens" in out) is (expected is not None)


def test_claude_usage_keeps_a_reported_zero_thinking_count():
    assert (
        parse_usage({"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 0}).to_dict()[
            "reasoning_tokens"
        ]
        == 0
    )
    assert "reasoning_tokens" not in parse_usage({"input_tokens": 1, "output_tokens": 2}).to_dict()
