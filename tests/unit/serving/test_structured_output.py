"""The gateway's own structured-output checks: what counts as a conforming reply,
how a miss is asked again, and that a reply is never turned into an error."""

from __future__ import annotations

import json
from typing import Any

import pytest

from serving.structured_output import (
    MAX_STRUCTURED_RETRIES,
    RequestedFormat,
    check_reply,
    conform_reply,
    requested_format,
    with_instruction,
)

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"enum": ["A", "B", "C", "D"], "type": "string"}},
    "additionalProperties": False,
}
STRICT = RequestedFormat(kind="json_schema", schema=SCHEMA)
USER = [{"role": "user", "content": "Pick one."}]


def _reply(content: Any, finish_reason: str = "stop", **message: Any) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish_reason,
            }
        ]
    }


# --- requested_format -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, None),
        ({"response_format": {"type": "text"}}, None),
        ({"response_format": {"type": "json_object"}}, RequestedFormat("json_object")),
        (
            {"response_format": {"type": "json_schema", "json_schema": {"schema": SCHEMA}}},
            STRICT,
        ),
        ({"response_format": {"type": "json_schema", "schema": SCHEMA}}, STRICT),
        ({"response_format": {"type": "json_schema"}}, RequestedFormat("json_schema")),
    ],
)
def test_requested_format(params, expected):
    assert requested_format(params) == expected


# --- check_reply ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ['{"answer": "B"}', '{\n  "answer": "B"\n}\n', '  {"answer":"B"}  '],
)
def test_a_conforming_reply_is_returned_as_it_is(content):
    assert check_reply(content, STRICT).ok
    assert check_reply(content, STRICT).content == content


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"answer": "B"}\n```',
        'Sure! Here it is: {"answer": "B"} Hope that helps.',
        '```\n{"answer": "B"}',
    ],
)
def test_json_inside_a_fence_or_prose_is_unwrapped(content):
    check = check_reply(content, STRICT)

    assert check.ok
    assert json.loads(check.content) == {"answer": "B"}


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("B. Eremophilane O-acetyltransferase", "not JSON"),
        ("", "empty"),
        (None, "empty"),
        ('{"answer": "E"}', "schema"),
        ('{"answer": "B", "why": "x"}', "schema"),
        ("{\n  \n  \n  \n", "not JSON"),
    ],
)
def test_a_reply_that_misses_the_schema(content, reason):
    check = check_reply(content, STRICT)

    assert not check.ok
    assert reason in check.reason


def test_json_object_wants_an_object():
    wanted = RequestedFormat("json_object")

    assert check_reply('{"a": 1}', wanted).ok
    assert not check_reply("[1, 2]", wanted).ok


def test_an_invalid_schema_holds_the_reply_only_to_being_json():
    wanted = RequestedFormat("json_schema", schema={"type": 12})

    assert check_reply('{"a": 1}', wanted).ok


# --- with_instruction -----------------------------------------------------------------------


def test_the_instruction_goes_into_the_last_user_turn_without_touching_the_original():
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ]
    original = json.loads(json.dumps(messages))

    updated = with_instruction(messages, STRICT)

    assert messages == original
    assert updated[:3] == original[:3]
    assert updated[3]["content"][0] == {"type": "text", "text": "second"}
    assert '"enum":["A","B","C","D"]' in updated[3]["content"][1]["text"]


def test_with_no_user_turn_the_instruction_is_one():
    updated = with_instruction([{"role": "system", "content": "s"}], STRICT)

    assert updated[-1]["role"] == "user"


# --- conform_reply --------------------------------------------------------------------------


class Retry:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> Any:
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.mark.asyncio
async def test_a_conforming_first_reply_is_not_retried():
    retry = Retry()

    response, summary = await conform_reply(_reply('{"answer": "A"}'), STRICT, retry, USER)

    assert retry.calls == []
    assert summary == {"attempts": 1, "conforms": True}
    assert response["choices"][0]["message"]["content"] == '{"answer": "A"}'


@pytest.mark.asyncio
async def test_a_miss_is_asked_again_with_the_schema_in_the_prompt():
    retry = Retry(_reply('```json\n{"answer": "C"}\n```'))

    response, summary = await conform_reply(_reply("C. Anthrax"), STRICT, retry, USER)

    assert summary == {"attempts": 2, "conforms": True}
    assert response["choices"][0]["message"]["content"] == '{"answer": "C"}'
    assert "JSON Schema" in retry.calls[0][-1]["content"]


@pytest.mark.asyncio
async def test_after_the_retries_the_last_reply_is_returned_not_an_error():
    retry = Retry(*[_reply(f"prose {i}") for i in range(MAX_STRUCTURED_RETRIES)])

    response, summary = await conform_reply(_reply("prose"), STRICT, retry, USER)

    assert len(retry.calls) == MAX_STRUCTURED_RETRIES
    assert summary["attempts"] == MAX_STRUCTURED_RETRIES + 1
    assert summary["conforms"] is False
    assert response["choices"][0]["message"]["content"] == f"prose {MAX_STRUCTURED_RETRIES - 1}"


@pytest.mark.asyncio
async def test_a_retry_that_fails_keeps_the_reply_in_hand():
    first = _reply("prose")
    retry = Retry(RuntimeError("upstream down"))

    response, summary = await conform_reply(first, STRICT, retry, USER)

    assert response is first
    assert summary == {"attempts": 1, "conforms": False, "reason": "reply is not JSON"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first",
    [
        _reply(None, finish_reason="length", reasoning_content="thinking..."),
        _reply('{"answer": "', finish_reason="length"),
        _reply(None, tool_calls=[{"id": "t", "type": "function"}]),
    ],
    ids=["budget-spent-reasoning", "cut-off-mid-json", "tool-call"],
)
async def test_a_reply_another_attempt_cannot_fix_is_not_retried(first):
    retry = Retry()

    _, summary = await conform_reply(first, STRICT, retry, USER)

    assert retry.calls == []
    assert summary["conforms"] is False
