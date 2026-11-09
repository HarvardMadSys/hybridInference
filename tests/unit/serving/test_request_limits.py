"""Element-count limits on inference request bodies, checked before validation."""

from __future__ import annotations

import time

import pytest

from serving.request_limits import (
    MAX_MESSAGES,
    MAX_PARTS_PER_MESSAGE,
    MAX_PARTS_PER_REQUEST,
    MAX_STOP_SEQUENCES,
    MAX_TOOLS,
    chat_limit_problem,
    messages_limit_problem,
    responses_limit_problem,
)

_TEXT = {"type": "text", "text": "x"}
_CALL = {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
_TOOL = {"type": "function", "function": {"name": "f", "parameters": {}}}


def _chat(**fields):
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}], **fields}


# --- chat ------------------------------------------------------------------------------------


def test_an_ordinary_chat_request_is_within_every_limit():
    body = _chat(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": [_TEXT, {"type": "image_url", "image_url": {"url": "u"}}]},
            {"role": "assistant", "content": None, "tool_calls": [_CALL, _CALL]},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
        ],
        tools=[_TOOL] * 40,
        stop=["\n\n", "END"],
    )

    assert chat_limit_problem(body) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            _chat(messages=[{"role": "user", "content": "hi"}] * (MAX_MESSAGES + 1)),
            f"Too many messages: {MAX_MESSAGES + 1}, at most {MAX_MESSAGES}",
        ),
        (
            _chat(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "user", "content": [_TEXT] * (MAX_PARTS_PER_MESSAGE + 1)},
                ]
            ),
            "Too many content parts and tool calls in messages[1]: "
            f"more than {MAX_PARTS_PER_MESSAGE}",
        ),
        (
            _chat(
                messages=[
                    {
                        "role": "assistant",
                        "content": [_TEXT] * (MAX_PARTS_PER_MESSAGE // 2),
                        "tool_calls": [_CALL] * (MAX_PARTS_PER_MESSAGE // 2 + 1),
                    }
                ]
            ),
            "Too many content parts and tool calls in messages[0]: "
            f"more than {MAX_PARTS_PER_MESSAGE}",
        ),
        (
            _chat(
                messages=[{"role": "user", "content": [_TEXT] * MAX_PARTS_PER_MESSAGE}]
                * (MAX_PARTS_PER_REQUEST // MAX_PARTS_PER_MESSAGE + 1)
            ),
            f"Too many content parts and tool calls: more than {MAX_PARTS_PER_REQUEST} in all",
        ),
        (
            _chat(tools=[_TOOL] * (MAX_TOOLS + 1)),
            f"Too many tools: {MAX_TOOLS + 1}, at most {MAX_TOOLS}",
        ),
        (
            _chat(stop=["x"] * (MAX_STOP_SEQUENCES + 1)),
            f"Too many stop sequences: {MAX_STOP_SEQUENCES + 1}, at most {MAX_STOP_SEQUENCES}",
        ),
    ],
    ids=["messages", "parts-in-a-message", "parts-and-calls", "parts-in-all", "tools", "stop"],
)
def test_a_chat_request_over_a_limit_is_named(body, expected):
    assert chat_limit_problem(body) == expected


def test_a_chat_request_at_every_limit_passes():
    per_message = MAX_PARTS_PER_REQUEST // MAX_MESSAGES
    body = _chat(
        messages=[{"role": "user", "content": [_TEXT] * per_message}] * MAX_MESSAGES,
        tools=[_TOOL] * MAX_TOOLS,
        stop=["x"] * MAX_STOP_SEQUENCES,
    )

    assert chat_limit_problem(body) is None


@pytest.mark.parametrize(
    "body",
    [[], "text", None, {"messages": "hi"}, {"messages": [1, "x", None]}, {"stop": "END"}],
)
def test_a_field_of_another_type_is_left_to_validation(body):
    assert chat_limit_problem(body) is None


def test_the_check_reads_lengths_before_it_walks_anything():
    # About what 32 MiB of {"role":"user"} decodes to.
    body = _chat(messages=[{"role": "user"}] * 2_000_000)

    started = time.perf_counter()
    problem = chat_limit_problem(body)

    assert time.perf_counter() - started < 0.1
    assert problem == f"Too many messages: 2000000, at most {MAX_MESSAGES}"


# --- Anthropic Messages ----------------------------------------------------------------------


def _anthropic(**fields):
    return {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "hi"}],
        **fields,
    }


def test_tool_result_blocks_count_toward_their_message():
    tool_result = {
        "type": "tool_result",
        "tool_use_id": "t",
        "content": [_TEXT] * MAX_PARTS_PER_MESSAGE,
    }
    body = _anthropic(messages=[{"role": "user", "content": [tool_result]}])

    assert messages_limit_problem(body) == (
        f"Too many content blocks in messages[0]: more than {MAX_PARTS_PER_MESSAGE}"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            _anthropic(messages=[{"role": "user", "content": "hi"}] * (MAX_MESSAGES + 1)),
            f"Too many messages: {MAX_MESSAGES + 1}, at most {MAX_MESSAGES}",
        ),
        (
            _anthropic(system=[_TEXT] * (MAX_PARTS_PER_MESSAGE + 1)),
            f"Too many system blocks: {MAX_PARTS_PER_MESSAGE + 1}, at most {MAX_PARTS_PER_MESSAGE}",
        ),
        (
            _anthropic(tools=[{"name": "f", "input_schema": {}}] * (MAX_TOOLS + 1)),
            f"Too many tools: {MAX_TOOLS + 1}, at most {MAX_TOOLS}",
        ),
        (
            _anthropic(stop_sequences=["x"] * (MAX_STOP_SEQUENCES + 1)),
            f"Too many stop sequences: {MAX_STOP_SEQUENCES + 1}, at most {MAX_STOP_SEQUENCES}",
        ),
    ],
    ids=["messages", "system", "tools", "stop"],
)
def test_a_messages_request_over_a_limit_is_named(body, expected):
    assert messages_limit_problem(body) == expected


def test_an_ordinary_messages_request_is_within_every_limit():
    body = _anthropic(
        system=[_TEXT],
        messages=[
            {"role": "user", "content": [_TEXT]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "f", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": [_TEXT]}],
            },
        ],
        tools=[{"name": "f", "input_schema": {}}],
        stop_sequences=["END"],
    )

    assert messages_limit_problem(body) is None


# --- Responses -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"model": "m", "input": [{"role": "user", "content": "hi"}] * (MAX_MESSAGES + 1)},
            f"Too many input items: {MAX_MESSAGES + 1}, at most {MAX_MESSAGES}",
        ),
        (
            {
                "model": "m",
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "c",
                        "output": [{"type": "input_text", "text": "x"}]
                        * (MAX_PARTS_PER_MESSAGE + 1),
                    }
                ],
            },
            f"Too many content parts in input[0]: more than {MAX_PARTS_PER_MESSAGE}",
        ),
        (
            {"model": "m", "input": "hi", "tools": [{"type": "function"}] * (MAX_TOOLS + 1)},
            f"Too many tools: {MAX_TOOLS + 1}, at most {MAX_TOOLS}",
        ),
    ],
    ids=["input-items", "output-parts", "tools"],
)
def test_a_responses_request_over_a_limit_is_named(body, expected):
    assert responses_limit_problem(body) == expected


def test_a_plain_text_responses_input_is_within_the_limits():
    assert responses_limit_problem({"model": "m", "input": "x" * 100_000}) is None
