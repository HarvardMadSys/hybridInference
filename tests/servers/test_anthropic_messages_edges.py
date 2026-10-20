"""/v1/messages: what the stream accumulator keeps, and the error answers in between.

When a streaming client disconnects, the logged usage is estimated from the
response accumulated off the forwarded SSE. Only its text path had a test: a
tool call or thinking block left out of the accumulator would make an aborted
agent turn log near-zero output. The rest covers the SSE splitter's CRLF and
size cap, the malformed-request 400s, count_tokens charging tool schemas, and
the two gateway-side refusals (no slot free, every key muted) on both the
buffered and the streaming path.
"""

from __future__ import annotations

import json

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.key_pool import KeyPoolExhausted
from serving.adapters.upstream_limiter import UpstreamSaturated
from serving.servers.routers import anthropic_messages as amod

NATIVE_MODEL = "claude-opus-4.7"
OPENAI_MODEL = "glm-4.7"


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _feed(events: list[tuple[str, dict]]) -> dict | None:
    acc = None
    for event_type, payload in events:
        acc = amod._apply_sse_event(acc, event_type, json.dumps(payload))
    return acc


def _start() -> tuple[str, dict]:
    return (
        "message_start",
        {
            "message": {
                "id": "msg_1",
                "model": "claude",
                "role": "assistant",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            }
        },
    )


def _block(idx: int, block: dict) -> tuple[str, dict]:
    return ("content_block_start", {"index": idx, "content_block": block})


def _delta(idx: int, delta: dict) -> tuple[str, dict]:
    return ("content_block_delta", {"index": idx, "delta": delta})


# --- the accumulator ----------------------------------------------------------------


def test_text_thinking_and_tool_blocks_are_all_accumulated():
    acc = _feed(
        [
            _start(),
            _block(0, {"type": "thinking", "thinking": ""}),
            _delta(0, {"type": "thinking_delta", "thinking": "Need the "}),
            _delta(0, {"type": "thinking_delta", "thinking": "weather."}),
            ("content_block_stop", {"index": 0}),
            _block(1, {"type": "text"}),
            _delta(1, {"type": "text_delta", "text": "Checking."}),
            ("content_block_stop", {"index": 1}),
            _block(2, {"type": "tool_use", "id": "toolu_1", "name": "weather"}),
            _delta(2, {"type": "input_json_delta", "partial_json": '{"city": '}),
            _delta(2, {"type": "input_json_delta", "partial_json": '"Rome"}'}),
            ("content_block_stop", {"index": 2}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 30, "cache_read_input_tokens": 400},
                },
            ),
        ]
    )

    assert acc["content"] == [
        {"type": "thinking", "thinking": "Need the weather."},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "Rome"}},
    ]
    assert acc["stop_reason"] == "tool_use"
    assert acc["usage"] == {
        "input_tokens": 12,
        "output_tokens": 30,
        "cache_read_input_tokens": 400,
    }
    assert amod._accumulated_output_text(acc) == 'Need the weather.Checking.{"city": "Rome"}'


def test_a_tool_call_cut_off_mid_arguments_still_counts_as_output():
    """A client that leaves mid tool call must not log ~0 output tokens."""
    acc = _feed(
        [
            _start(),
            _block(0, {"type": "tool_use", "id": "toolu_1", "name": "write_file"}),
            _delta(0, {"type": "input_json_delta", "partial_json": '{"path": "a.py", "body": "'}),
            _delta(0, {"type": "input_json_delta", "partial_json": "x = 1\\n" * 200}),
        ]
    )

    final = amod._finalize_response_acc(acc)

    [block] = final["content"]
    assert isinstance(block["input"], str) and block["input"].startswith('{"path": "a.py"')
    assert "_partial_json" not in block
    usage, estimated = amod._resolve_stream_usage({}, final, None)
    assert estimated is True
    assert usage["input_tokens"] == 12  # seen on message_start
    assert usage["output_tokens"] > 100


def test_a_tool_call_with_no_arguments_finalizes_to_an_empty_object():
    acc = _feed(
        [
            _start(),
            _block(0, {"type": "tool_use", "id": "toolu_1", "name": "now"}),
            ("content_block_stop", {"index": 0}),
        ]
    )

    assert acc["content"] == [{"type": "tool_use", "id": "toolu_1", "name": "now", "input": {}}]
    assert amod._accumulated_output_text(acc) == ""


@pytest.mark.parametrize(
    "events",
    [
        [_delta(0, {"type": "text_delta", "text": "before start"})],  # nothing to add to yet
        [_start(), ("content_block_start", ["not", "an", "object"])],
        [_start(), ("content_block_start", {"index": "0", "content_block": {"type": "text"}})],
        [_start(), ("content_block_delta", {"index": 0, "delta": "text"})],
        [_start(), ("content_block_stop", {"index": None})],
        [_start(), ("message_start", {"message": "not an object"})],
    ],
    ids=[
        "before-start",
        "non-object",
        "string-index",
        "string-delta",
        "no-stop-index",
        "bad-restart",
    ],
)
def test_malformed_events_are_ignored_not_raised(events):
    acc = _feed(events)

    assert acc is None or acc["content"] == []


def test_the_output_text_skips_what_is_not_a_block():
    acc = {
        "content": [
            "stray",
            {"type": "text", "text": "a"},
            {"type": "tool_use", "input": "raw-args"},
            {"type": "image"},
        ]
    }

    assert amod._accumulated_output_text(acc) == "araw-args"
    assert amod._accumulated_output_text(None) == ""


# --- the SSE splitter -------------------------------------------------------------------


def test_crlf_and_lf_separated_events_are_both_split():
    events, leftover = amod._parse_sse_chunk(
        b"",
        b'event: message_start\r\ndata: {"a": 1}\r\n\r\n'
        b"\n\n"  # an empty block between events
        b'event: ping\ndata: {"b": 2}\n\n'
        b"event: content_block_delta\ndata: one\ndata: two\n\n"
        b"event: partial\ndata: {",
    )

    assert events == [
        ("message_start", '{"a": 1}'),
        ("ping", '{"b": 2}'),
        ("content_block_delta", "one\ntwo"),
    ]
    assert leftover == b"event: partial\ndata: {"


def test_crlf_only_events_are_split():
    events, leftover = amod._parse_sse_chunk(
        b"",
        b'event: message_start\r\ndata: {"a": 1}\r\n\r\nevent: ping\r\ndata: {}\r\n\r\nevent: x',
    )

    assert events == [("message_start", '{"a": 1}'), ("ping", "{}")]
    assert leftover == b"event: x"


def test_a_stream_that_never_separates_its_events_is_not_buffered_forever():
    events, leftover = amod._parse_sse_chunk(b"", b"data: " + b"x" * (amod._SSE_LEFTOVER_CAP + 1))

    assert events == []
    assert leftover == b""


# --- malformed requests ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        ("/v1/messages", b"{not json", "Invalid JSON in request body"),
        ("/v1/messages", b'{"messages": [], "max_tokens": 5}', "Missing required field: model"),
        (
            "/v1/messages",
            json.dumps({"model": NATIVE_MODEL, "max_tokens": 5}).encode(),
            "Missing required field: messages",
        ),
        (
            "/v1/messages",
            json.dumps({"model": NATIVE_MODEL, "messages": []}).encode(),
            "Missing required field: max_tokens",
        ),
        ("/v1/messages/count_tokens", b"{not json", "Invalid JSON in request body"),
        (
            "/v1/messages/count_tokens",
            json.dumps({"model": NATIVE_MODEL}).encode(),
            "Missing required field: messages",
        ),
    ],
    ids=[
        "not-json",
        "no-model",
        "no-messages",
        "no-max-tokens",
        "count-not-json",
        "count-no-messages",
    ],
)
async def test_a_malformed_request_gets_an_anthropic_400(
    anthropic_test_client, path, content, message
):
    r = await anthropic_test_client.post(
        path, content=content, headers={**_auth(), "content-type": "application/json"}
    )

    assert r.status_code == 400
    assert r.json() == {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


@pytest.mark.asyncio
async def test_count_tokens_charges_for_tool_schemas(anthropic_test_client):
    body = {"model": OPENAI_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    tools = [
        {
            "name": f"tool_{i}",
            "description": "a reasonably long description of what this tool does " * 3,
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
        for i in range(5)
    ]

    plain = await anthropic_test_client.post(
        "/v1/messages/count_tokens", json=body, headers=_auth()
    )
    with_tools = await anthropic_test_client.post(
        "/v1/messages/count_tokens", json={**body, "tools": tools}, headers=_auth()
    )

    assert plain.status_code == with_tools.status_code == 200
    assert with_tools.json()["input_tokens"] > plain.json()["input_tokens"] + 100


# --- gateway-side refusals --------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_free_upstream_slot_is_a_429_with_a_one_second_retry(
    anthropic_test_client, monkeypatch
):
    async def saturated(self, body, **kwargs):
        raise UpstreamSaturated("no slot")

    monkeypatch.setattr(AnthropicAdapter, "messages", saturated)

    r = await anthropic_test_client.post(
        "/v1/messages",
        json={
            "model": NATIVE_MODEL,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(),
    )

    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert "no slot" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [KeyPoolExhausted("all keys muted"), UpstreamSaturated("no slot")],
    ids=["keys-muted", "no-slot"],
)
async def test_a_streaming_refusal_ends_in_a_rate_limit_error_event(
    anthropic_test_client, monkeypatch, refusal
):
    captured: dict = {}

    async def refused(self, body, **kwargs):
        raise refusal
        yield b""  # an async generator, like the real one

    monkeypatch.setattr(AnthropicAdapter, "stream_messages", refused)
    monkeypatch.setattr(amod, "_schedule_log_store_task", lambda store, **kw: captured.update(kw))

    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 5,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=body, headers=_auth()
    ) as r:
        text = b"".join([chunk async for chunk in r.aiter_bytes()]).decode()

    assert "event: error" in text
    error_line = next(
        line for line in text.splitlines() if line.startswith('data: {"type": "error"')
    )
    error = json.loads(error_line[len("data: ") :])["error"]
    assert error["type"] == "rate_limit_error"
    assert str(refusal) not in error["message"]  # the operator cause stays in the log
    assert captured["status_code"] == 429
    assert str(refusal) in captured["operator_error"]
