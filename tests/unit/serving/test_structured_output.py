"""The gateway's own structured-output checks: what counts as a conforming reply,
how a miss is asked again, and that a reply is never turned into an error."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

import pytest

from serving import structured_output
from serving.structured_output import (
    MAX_CHECKED_SCHEMA_CHARS,
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


# --- check_reply: the caller's schema runs on the event loop, so it is bounded ---------------


def _schema(schema: dict[str, Any]) -> RequestedFormat:
    return RequestedFormat("json_schema", schema=schema)


def _timed_check(content: str, wanted: RequestedFormat) -> tuple[float, Any]:
    started = time.perf_counter()
    check = check_reply(content, wanted)
    return time.perf_counter() - started, check


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        # Python's re backtracks: this pattern against these 41 characters takes hours.
        (
            {"type": "object", "properties": {"answer": {"type": "string", "pattern": "^(a+)+$"}}},
            json.dumps({"answer": "a" * 40 + "!"}),
        ),
        (
            {"type": "string", "pattern": r"^(\w+\s?)*$"},
            json.dumps(" ".join(["word"] * 40) + "."),
        ),
        (
            {"type": "object", "patternProperties": {"^(a+)+$": {"type": "integer"}}},
            json.dumps({"a" * 40 + "!": 1}),
        ),
        (
            {"type": "object", "propertyNames": {"pattern": "^(a+)+$"}},
            json.dumps({"a" * 40 + "!": 1}),
        ),
        (
            {
                "type": "object",
                "patternProperties": {"^(a+)+$": {}},
                "additionalProperties": False,
            },
            json.dumps({"a" * 40 + "!": 1}),
        ),
    ],
    ids=["pattern", "pattern-sentence", "patternProperties", "propertyNames", "additional"],
)
def test_a_regex_from_the_request_is_not_run(schema, content):
    elapsed, check = _timed_check(content, _schema(schema))

    assert elapsed < 1.0
    assert check.ok


def test_the_rest_of_the_schema_still_applies_beside_a_pattern():
    wanted = _schema(
        {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "pattern": "^(a+)+$"}},
        }
    )

    assert check_reply('{"answer": "anything"}', wanted).ok
    assert "schema" in check_reply('{"answer": 5}', wanted).reason
    assert "schema" in check_reply('{"other": "x"}', wanted).reason


def test_format_is_not_checked_regex_included():
    wanted = _schema(
        {"type": "object", "properties": {"re": {"type": "string", "format": "regex"}}}
    )

    assert check_reply('{"re": "(unclosed"}', wanted).ok


def test_a_remote_reference_is_not_fetched(monkeypatch):
    fetched: list[Any] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: fetched.append(a))
    wanted = _schema({"$ref": "http://169.254.169.254/latest/meta-data/"})

    check = check_reply('{"a": 1}', wanted)

    assert fetched == []
    assert check.ok


def test_a_schema_whose_check_grows_exponentially_stops_in_time():
    # allOf of two references to the next level: 2**30 steps.
    depth = 30
    defs: dict[str, Any] = {
        f"d{i}": {"allOf": [{"$ref": f"#/$defs/d{i + 1}"}, {"$ref": f"#/$defs/d{i + 1}"}]}
        for i in range(depth)
    }
    defs[f"d{depth}"] = {"type": "object"}

    elapsed, check = _timed_check('{"a": 1}', _schema({"$defs": defs, "$ref": "#/$defs/d0"}))

    assert elapsed < 2.0
    assert check.ok


def test_a_check_out_of_time_holds_the_reply_only_to_being_json(monkeypatch):
    monkeypatch.setattr(structured_output, "_CHECK_SECONDS", -1.0)

    assert check_reply('{"answer": "E"}', STRICT).ok
    assert not check_reply("prose", STRICT).ok


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "other.json"},
        {"$id": {"not": "a uri"}, "type": "object"},
    ],
    ids=["self-reference", "missing-target", "unknown-document", "bad-id"],
)
def test_a_schema_that_cannot_be_applied_holds_the_reply_only_to_being_json(schema):
    assert check_reply('{"a": 1}', _schema(schema)).ok
    assert not check_reply("prose", _schema(schema)).ok


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "required": "answer"},
        {"enum": "ABCD"},
        {"type": []},
        {"type": "object", "properties": {"answer": "string"}},
        {"type": "array", "minItems": True},
        {"anyOf": []},
    ],
)
def test_a_malformed_keyword_holds_the_reply_only_to_being_json(schema):
    assert check_reply('{"answer": "B"}', _schema(schema)).ok


def test_unique_items_is_linear_and_still_finds_a_duplicate():
    wanted = _schema({"type": "array", "uniqueItems": True})
    distinct = json.dumps([{"a": i, "b": [i, str(i)]} for i in range(20_000)])

    elapsed, check = _timed_check(distinct, wanted)

    assert elapsed < 2.0
    assert check.ok
    assert not check_reply('[{"a": 1, "b": 2}, {"b": 2, "a": 1}]', wanted).ok
    assert check_reply("[true, 1, 0, false]", wanted).ok


def test_the_schemas_own_dialect_is_dropped_and_a_nested_one_is_not_used():
    draft7 = "http://json-schema.org/draft-07/schema#"

    root = _schema({**SCHEMA, "$schema": draft7})
    assert not check_reply('{"answer": "E"}', root).ok
    assert check_reply('{"answer": "B"}', root).ok

    nested = _schema(
        {
            "$defs": {"s": {"$schema": draft7, "type": "string", "pattern": "^(a+)+$"}},
            "$ref": "#/$defs/s",
        }
    )
    elapsed, check = _timed_check(json.dumps("a" * 40 + "!"), nested)
    assert elapsed < 1.0
    assert check.ok


def test_a_schema_too_long_to_check_holds_the_reply_only_to_being_json():
    names = {f"p{i}": {"type": "integer"} for i in range(MAX_CHECKED_SCHEMA_CHARS // 10)}
    wanted = _schema({"type": "object", "properties": names, "required": ["p0"]})

    assert check_reply('{"other": 1}', wanted).ok


def test_a_long_mismatch_is_described_briefly():
    wanted = _schema({"enum": [f"value-{i}" for i in range(2_000)]})

    check = check_reply('"nope"', wanted)

    assert not check.ok
    assert len(check.reason) < 400


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
