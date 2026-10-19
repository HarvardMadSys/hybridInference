"""OpenAI-to-Claude request conversion, and Claude-to-OpenAI tool calls back.

Claude refuses a conversation unless user and assistant turns alternate and
every ``tool_use`` is answered by a ``tool_result`` in the very next user turn.
``convert_messages`` has to produce that from OpenAI's shape, where tool
results are separate ``tool`` messages that may be interleaved with other
turns. Most of that reordering, the system/tool/tool_choice conversions and
the response-side tool calls had no test.
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import Any

import pytest

from serving.adapters import claude_format as fmt


def _tool_call(call_id: str, name: str, args: str = "{}") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _tool(call_id: str, content: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _claude_rules_hold(converted: list[dict[str, Any]]) -> None:
    """The two constraints Claude enforces on a conversation."""
    roles = [message["role"] for message in converted]
    assert all(a != b for a, b in pairwise(roles)), roles
    for i, message in enumerate(converted):
        used = [b["id"] for b in message["content"] if b.get("type") == "tool_use"]
        if not used:
            continue
        answered = (
            [
                b["tool_use_id"]
                for b in converted[i + 1]["content"]
                if b.get("type") == "tool_result"
            ]
            if i + 1 < len(converted)
            else []
        )
        assert set(used) <= set(answered), (used, answered)


# --- conversations ---------------------------------------------------------------


def test_parallel_tool_calls_are_answered_together_in_the_next_turn():
    converted = fmt.convert_messages(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "weather in Paris and Rome?"},
            {
                "role": "assistant",
                "content": "Checking both.",
                "tool_calls": [
                    _tool_call("c1", "weather", '{"city": "Paris"}'),
                    _tool_call("c2", "weather", '{"city": "Rome"}'),
                ],
            },
            _tool("c1", "18C"),
            _tool("c2", "24C"),
            {"role": "user", "content": "thanks"},
        ]
    )

    _claude_rules_hold(converted)
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assistant = converted[1]["content"]
    assert assistant[0] == {"type": "text", "text": "Checking both."}
    assert [(b["id"], b["input"]) for b in assistant[1:]] == [
        ("c1", {"city": "Paris"}),
        ("c2", {"city": "Rome"}),
    ]
    results = [b for b in converted[2]["content"] if b["type"] == "tool_result"]
    assert [(r["tool_use_id"], r["content"][0]["text"]) for r in results] == [
        ("c1", "18C"),
        ("c2", "24C"),
    ]
    assert converted[2]["content"][-1] == {"type": "text", "text": "thanks"}


def test_a_tool_result_that_arrives_after_the_next_assistant_turn_is_moved_up():
    """Claude needs the result before the assistant speaks again."""
    converted = fmt.convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("c1", "run")]},
            {"role": "assistant", "content": "still working"},
            _tool("c1", "done"),
        ]
    )

    _claude_rules_hold(converted)


def test_a_result_that_trails_the_next_assistant_turn_joins_its_siblings():
    """Two calls, one answered, the assistant speaks, then the other answer arrives."""
    converted = fmt.convert_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("c1", "a"), _tool_call("c2", "b")],
            },
            _tool("c1", "one"),
            {"role": "assistant", "content": "waiting on b"},
            _tool("c2", "two"),
        ]
    )

    _claude_rules_hold(converted)
    assert [m["role"] for m in converted] == ["user", "assistant", "user", "assistant"]
    assert [b["tool_use_id"] for b in converted[2]["content"]] == ["c1", "c2"]


def test_consecutive_turns_of_one_role_are_merged_text_before_tools():
    converted = fmt.convert_messages(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "x", "tool_calls": [_tool_call("c1", "t")]},
            {"role": "assistant", "content": "y"},
            _tool("c1", "r"),
        ]
    )

    _claude_rules_hold(converted)
    assert [b["text"] for b in converted[0]["content"]] == ["a", "b"]
    assert [b.get("text") or b["type"] for b in converted[1]["content"]] == ["x", "y", "tool_use"]


@pytest.mark.parametrize(
    ("arguments", "content", "expected_input", "expected_text"),
    [
        ("{not json", "ok", {}, "ok"),
        ('{"q": 1}', None, {"q": 1}, '""'),
        ('{"q": 1}', {"rows": 2}, {"q": 1}, '{"rows": 2}'),
    ],
    ids=["bad-arguments", "empty-result", "object-result"],
)
def test_odd_tool_payloads_still_convert(arguments, content, expected_input, expected_text):
    converted = fmt.convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "t", arguments)]},
            _tool("c1", content),
        ]
    )

    _claude_rules_hold(converted)
    assert converted[1]["content"][0]["input"] == expected_input
    assert converted[2]["content"][0]["content"][0]["text"] == expected_text


def test_a_tool_result_is_never_sent_twice():
    converted = fmt.convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "t")]},
            _tool("c1", "first"),
            _tool("c1", "duplicate"),
        ]
    )

    results = [b for m in converted for b in m["content"] if b.get("type") == "tool_result"]
    assert [r["content"][0]["text"] for r in results] == ["first"]


# --- system, tools and tool_choice -------------------------------------------------


def test_every_system_message_is_collected_in_order():
    system = fmt.extract_system(
        [
            {"role": "system", "content": "one"},
            {"role": "user", "content": "ignored"},
            {
                "role": "system",
                "content": [{"type": "text", "text": "two"}, {"type": "image_url", "x": 1}],
            },
            {"role": "system", "content": {"rules": ["be kind"]}},
            {"role": "system", "content": ""},
        ]
    )

    assert system == 'one\n\ntwo\n\n{"type": "image_url", "x": 1}\n\n{"rules": ["be kind"]}'
    assert fmt.extract_system([{"role": "user", "content": "hi"}]) is None


def test_function_tools_become_claude_tools_and_others_are_dropped():
    tools = fmt.convert_tools(
        [
            {
                "type": "function",
                "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
            },
            {"type": "function", "function": {"name": "bare"}},
            {"type": "function", "function": {"description": "nameless"}},
            {"type": "retrieval"},
        ]
    )

    assert tools == [
        {"name": "f", "description": "d", "input_schema": {"type": "object"}},
        {"name": "bare", "description": "", "input_schema": {}},
    ]


@pytest.mark.parametrize(
    ("choice", "tool_choice", "tools_kept"),
    [
        ("auto", None, True),
        ("required", {"type": "any"}, True),
        ("any", {"type": "any"}, True),
        ("none", None, False),  # Claude has no "none": the tools are withheld
        ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}, True),
        ({"type": "function", "function": {}}, None, True),
    ],
    ids=["auto", "required", "any", "none", "named", "unnamed"],
)
def test_tool_choice(choice, tool_choice, tools_kept):
    payload: dict[str, Any] = {"tools": [{"name": "f"}]}

    fmt.convert_tool_choice(choice, payload)

    assert payload.get("tool_choice") == tool_choice
    assert ("tools" in payload) is tools_kept


# --- the response side ---------------------------------------------------------------


def test_tool_use_blocks_come_back_as_indexed_openai_tool_calls():
    text, calls = fmt.parse_response_content(
        [
            {"type": "text", "text": "Calling "},
            {"type": "tool_use", "id": "t1", "name": "a", "input": {"x": 1}},
            {"type": "text", "text": "two."},
            {"type": "tool_use", "id": "t2", "name": "b"},
        ]
    )

    assert text == "Calling two."
    assert [(c["index"], c["id"], c["function"]["name"]) for c in calls] == [
        (0, "t1", "a"),
        (1, "t2", "b"),
    ]
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"x": 1}, {}]
    assert fmt.parse_response_content([{"type": "text", "text": "plain"}]) == ("plain", None)


# --- data URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("data:IMAGE/PNG;base64,QUJD", {"media_type": "image/png", "data": "QUJD"}),
        ("data:image/png,ABC", {"media_type": "image/png", "data": "QUJD"}),  # percent form
        ("data:application/pdf;base64,QUJD", "[Unsupported image type: application/pdf]"),
        ("not a data url", "[Invalid data URL format]"),
    ],
)
def test_data_urls(url, expected):
    block = fmt.parse_data_url(url)

    if isinstance(expected, str):
        assert block == {"type": "text", "text": expected}
    else:
        assert block["type"] == "image"
        assert block["source"] == {"type": "base64", **expected}
