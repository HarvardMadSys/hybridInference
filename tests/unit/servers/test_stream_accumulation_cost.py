"""Accumulating a streamed answer costs time linear in its length.

Each streaming path keeps the text it has relayed -- for the request log, the
usage estimate when the upstream reports none, and the bill of a stream the
client closes early. They appended each piece to a string held in an attribute,
a closure variable or a dict item, which CPython copies whole on every append:
100k pieces took about 3 s of event loop, against milliseconds for a list.

Each test below drives one path's real accumulator through as many pieces as a
very long generation sends, then checks what it kept. The time bounds are
generous -- many times what the list needs on a busy machine -- and the old
string appends overshoot them several times over, so only a return to copying
on every piece can fail them.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.servers.routers import anthropic_messages as amod
from serving.servers.routers.completions_stream import ToolCallAccumulator, _joined
from tests.unit.servers.test_completions_stream import _make_session

pytestmark = pytest.mark.unit

PIECE = "x" * 40
PIECES = 60_000  # 2.4 MB per field; the old appends copied ~70 GB to build it
BOUND_S = 2.5


def test_joined_keeps_one_string_and_appends_after_it():
    parts = ["a", "b", "c"]

    assert _joined(parts) == "abc"
    assert parts == ["abc"]  # a second read joins nothing
    parts.append("d")
    assert _joined(parts) == "abcd"
    assert _joined([]) == ""


def test_tool_call_arguments_join_on_read_and_keep_growing():
    acc = ToolCallAccumulator()
    acc.add([{"index": 0, "id": "c", "function": {"name": "wr", "arguments": '{"a":'}}])
    acc.add([{"index": 0, "function": {"name": "ite", "arguments": "1"}}])

    assert acc.to_list()[0]["function"] == {"name": "write", "arguments": '{"a":1'}

    acc.add([{"index": 0, "function": {"arguments": "}"}}])
    assert acc.to_list()[0]["function"] == {"name": "write", "arguments": '{"a":1}'}


def test_the_chat_stream_session_accumulates_in_linear_time():
    session = _make_session()
    content = {"choices": [{"index": 0, "delta": {"content": PIECE}}]}
    reasoning = {"choices": [{"index": 0, "delta": {"reasoning_content": PIECE}}]}
    arguments = {
        "choices": [
            {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": PIECE}}]}}
        ]
    }

    start = time.perf_counter()
    for _ in range(PIECES):
        session._accumulate_state_from_chunk(content)
        session._accumulate_state_from_chunk(reasoning)
        session._accumulate_state_from_chunk(arguments)
    response = session._build_response_for_db()
    elapsed = time.perf_counter() - start

    message = response["choices"][0]["message"]
    assert message["content"] == PIECE * PIECES
    assert message["reasoning_content"] == PIECE * PIECES
    assert message["tool_calls"][0]["function"]["arguments"] == PIECE * PIECES
    assert elapsed < BOUND_S, f"{3 * PIECES} deltas took {elapsed:.2f}s to accumulate"


def test_a_cut_off_chat_stream_estimates_from_everything_it_relayed():
    """The cancel bill reads the same accumulators, mid-stream."""
    session = _make_session()
    for piece in ("Hel", "lo ", "world"):
        session._accumulate_state_from_chunk({"choices": [{"delta": {"content": piece}}]})
    session._accumulate_state_from_chunk({"choices": [{"delta": {"reasoning": "hmm"}}]})

    usage, estimated = session._cancelled_usage()

    assert estimated is True
    assert session._final_text == "Hello world"
    assert session._final_reasoning == "hmm"
    assert usage["completion_tokens"] > 0


def test_a_non_string_content_delta_still_fails_at_its_chunk():
    """What the string append raised, the chunk that carried it still raises."""
    session = _make_session()

    with pytest.raises(TypeError):
        session._accumulate_state_from_chunk({"choices": [{"delta": {"content": ["x"]}}]})


def _event(event_type: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return event_type, payload


def test_the_messages_accumulator_accumulates_in_linear_time():
    acc = amod._apply_sse_payload(
        None,
        "message_start",
        {"message": {"id": "m", "role": "assistant", "usage": {"input_tokens": 3}}},
    )
    starts = [
        (0, {"type": "thinking", "thinking": ""}),
        (1, {"type": "text", "text": ""}),
        (2, {"type": "tool_use", "id": "t", "name": "write"}),
    ]
    for idx, block in starts:
        acc = amod._apply_sse_payload(
            acc, "content_block_start", {"index": idx, "content_block": block}
        )
    deltas = [
        {"index": 0, "delta": {"type": "thinking_delta", "thinking": PIECE}},
        {"index": 1, "delta": {"type": "text_delta", "text": PIECE}},
        {"index": 2, "delta": {"type": "input_json_delta", "partial_json": PIECE}},
    ]

    start = time.perf_counter()
    for _ in range(PIECES):
        for delta in deltas:
            acc = amod._apply_sse_payload(acc, "content_block_delta", delta)
    final = amod._finalize_response_acc(acc)
    elapsed = time.perf_counter() - start

    thinking, text, tool = final["content"]
    assert thinking == {"type": "thinking", "thinking": PIECE * PIECES}
    assert text == {"type": "text", "text": PIECE * PIECES}
    # Not JSON, so it is kept raw, as before; the sentinel is gone either way.
    assert tool["input"] == PIECE * PIECES and "_partial_json" not in tool
    assert elapsed < BOUND_S, f"{3 * PIECES} deltas took {elapsed:.2f}s to accumulate"


def test_a_messages_block_keeps_text_it_started_with_and_drops_bad_deltas():
    acc = amod._apply_sse_event(None, "message_start", json.dumps({"message": {"id": "m"}}))
    for event_type, payload in [
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": "Hi"}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": ", "}}),
        # Not a string: dropped, as adding it to the text always dropped it.
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": 7}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "you"}}),
        ("content_block_stop", {"index": 0}),
    ]:
        acc = amod._apply_sse_event(acc, event_type, json.dumps(payload))

    assert acc["content"] == [{"type": "text", "text": "Hi, you"}]


# ---------------------------------------------------------------------------
# The OpenAI-compatible adapter: its usage-fallback accumulators
# ---------------------------------------------------------------------------


class _StubHTTP:
    """``stream_post`` replaying pre-framed SSE lines, as ``serving.http`` yields them."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def stream_post(self, *_args: Any, **_kwargs: Any):
        for line in self._lines:
            yield line


def _adapter(lines: list[str]) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="local-model",
            name="Local Model",
            provider="vllm",
            base_url="http://localhost:8000/v1",
            provider_model_id="local-model",
            processor="default",
            supported_params=["temperature", "max_tokens"],
        )
    )
    adapter.http = _StubHTTP(lines)
    return adapter


def _line(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": "c",
        "object": "chat.completion.chunk",
        "model": "local-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}"


@pytest.mark.asyncio
async def test_the_adapter_usage_fallback_reads_everything_relayed(monkeypatch):
    seen: list[str] = []

    def _count(text: str) -> int:
        seen.append(text)
        return len(text)

    monkeypatch.setattr("serving.adapters.openai_compat.estimate_text_tokens", _count)
    lines = [
        _line({"reasoning_content": "think "}),
        _line({"content": "Hello "}),
        _line({"content": "world"}),
        _line(
            {"tool_calls": [{"index": 0, "id": "t", "function": {"name": "f", "arguments": ""}}]}
        ),
        _line({"tool_calls": [{"index": 0, "function": {"arguments": '{"a":1}'}}]}),
        _line({}, finish_reason="tool_calls"),
        "data: [DONE]",
    ]

    out = [
        c async for c in _adapter(lines).stream_chat_completion([{"role": "user", "content": "hi"}])
    ]

    final = json.loads(out[-2][6:])
    assert seen == ["think Hello world", 'f{"a":1}']
    assert final["usage"]["completion_tokens"] == len("think Hello world") + len('f{"a":1}')


@pytest.mark.asyncio
async def test_the_adapter_accumulates_in_linear_time(monkeypatch):
    """A long answer through the adapter's per-chunk path, usage estimated at the end.

    Bigger pieces and fewer of them than above: each chunk is also parsed and
    re-serialized, a cost linear in its size that both versions pay, while the
    old closure-variable append copied the answer so far on every chunk.
    """
    monkeypatch.setattr("serving.adapters.openai_compat.estimate_text_tokens", len)
    piece = "y" * 2000
    chunks = 12_000  # 24 MB of answer; the old appends copied ~150 GB to build it
    lines = [_line({"content": piece}) for _ in range(chunks)]
    lines += [_line({}, finish_reason="stop"), "data: [DONE]"]
    adapter = _adapter(lines)

    start = time.perf_counter()
    last = None
    async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
        if chunk.startswith("data: {"):
            last = chunk
    elapsed = time.perf_counter() - start

    assert json.loads(last[6:])["usage"]["completion_tokens"] == len(piece) * chunks
    assert elapsed < 8.0, f"{chunks} chunks took {elapsed:.2f}s through the adapter"


# ---------------------------------------------------------------------------
# The same pattern on the Claude adapters' tool calls and on /v1/responses
# ---------------------------------------------------------------------------


def test_a_claude_tool_call_accumulates_in_linear_time():
    from serving.adapters.claude_format import ToolCallAccumulator as ClaudeToolCalls

    acc = ClaudeToolCalls()
    acc.start_tool("toolu_1", "write_file")
    start = time.perf_counter()
    for _ in range(3 * PIECES):
        acc.accumulate_json(PIECE)
    acc.finish_tool()
    elapsed = time.perf_counter() - start

    (call,) = acc.get_completed()
    assert call["function"] == {"name": "write_file", "arguments": PIECE * 3 * PIECES}
    assert elapsed < BOUND_S, f"{3 * PIECES} fragments took {elapsed:.2f}s to accumulate"
    with pytest.raises(TypeError):  # as appending a non-string always raised
        acc.accumulate_json(None)


def test_a_responses_stream_accumulates_in_linear_time():
    from serving.responses_translator import ResponsesStreamTranslator

    translator = ResponsesStreamTranslator(
        response_id="resp_1", created_at=1, model="m", request_body={}
    )
    # Fewer, larger pieces: every content and argument delta is also an event
    # put on the wire, a fixed cost per delta that both versions pay.
    piece = "x" * 400
    pieces = PIECES // 2
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": piece}}]},
        {"choices": [{"index": 0, "delta": {"content": piece}}]},
    ]
    call = {"index": 0, "id": "call_1", "function": {"name": "f", "arguments": piece}}

    start = time.perf_counter()
    for _ in range(pieces):
        for chunk in chunks:
            for _event in translator._process_obj(chunk):
                pass
    for _ in range(pieces):
        for _event in translator._process_obj(
            {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}
        ):
            pass
    for _event in translator.finalize():
        pass
    elapsed = time.perf_counter() - start

    message = translator.assistant_message
    assert message["content"] == piece * pieces
    assert message["reasoning_content"] == piece * pieces
    assert message["tool_calls"][0]["function"]["arguments"] == piece * pieces
    assert elapsed < 2 * BOUND_S, f"{3 * pieces} deltas took {elapsed:.2f}s to translate"
