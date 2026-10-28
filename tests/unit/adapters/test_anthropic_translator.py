"""Unit tests for serving.adapters.anthropic_translator.

Covers Anthropic Messages format -> OpenAI Chat Completions format translation.
"""

from __future__ import annotations

import copy
import json as _json
import logging

import pytest

from serving.adapters.anthropic_translator import (
    OpenAIToAnthropicStreamTranslator,
    _ToolArguments,
    anthropic_request_to_openai,
    extract_anthropic_usage_from_sse,
    normalize_inline_system,
)


def test_request_text_only_single_turn():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    messages, params = anthropic_request_to_openai(body)
    assert messages == [{"role": "user", "content": "Hello"}]
    assert params["max_tokens"] == 100


def test_request_system_string_prepended():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "system": "Be concise.",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0] == {"role": "system", "content": "Be concise."}
    assert messages[1] == {"role": "user", "content": "Hi"}


def test_request_multi_turn_preserved():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert len(messages) == 3
    assert messages[2]["content"] == "How are you?"


def test_request_image_data_url_block():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    parts = messages[0]["content"]
    assert parts[0] == {"type": "text", "text": "What is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_request_image_url_block():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.com/x.png"},
                    },
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/x.png"},
    }


def test_request_tools_translated():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "Get the weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]


def test_request_cache_control_blocks_dropped(caplog):
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hi", "cache_control": {"type": "ephemeral"}},
                ],
            }
        ],
    }
    with caplog.at_level(logging.WARNING, logger="serving.adapters.anthropic_translator"):
        messages, _ = anthropic_request_to_openai(body)
    # cache_control silently stripped from the text block.
    assert messages[0]["content"] == "Hi"


def test_request_thinking_field_dropped():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "thinking": {"type": "enabled", "budget_tokens": 5000},
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert "thinking" not in params


def test_request_system_array_concatenated():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "system": [
            {"type": "text", "text": "You are helpful."},
            {"type": "text", "text": "Be concise."},
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0] == {"role": "system", "content": "You are helpful.\n\nBe concise."}


def test_request_tool_choice_auto():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "auto"


def test_request_tool_choice_any_becomes_required():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "any"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "required"


def test_request_tool_choice_named_tool():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "tool", "name": "get_weather"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }


def test_request_tool_choice_none_is_translated_not_dropped():
    # Issue #876 P3: {"type":"none"} is a real instruction — "the caller
    # attached tool definitions but this turn must not use them" — and the
    # OpenAI dialect spells it "none". Dropping it silently degraded the
    # request to the model's default tool behavior.
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "none"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "none"


def test_request_disable_parallel_tool_use_maps_to_parallel_tool_calls():
    # Issue #876 P3 (same bullet): Anthropic expresses "one tool call per
    # turn" as tool_choice.disable_parallel_tool_use; the OpenAI dialect's
    # spelling is the top-level parallel_tool_calls flag.
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "auto"
    assert params["parallel_tool_calls"] is False


def test_request_parallel_tool_use_flag_only_set_when_true():
    # Anthropic only ever sends the flag to disable; when it is absent or
    # false the OpenAI request must not carry a parallel_tool_calls key at
    # all — a provider default of True must not be second-guessed, and an
    # explicit False would be indistinguishable from the disabled spelling.
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
    }
    _, params = anthropic_request_to_openai(body)
    assert "parallel_tool_calls" not in params


def test_request_metadata_user_id_to_user_field():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "metadata": {"user_id": "u-abc"},
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["user"] == "u-abc"


def test_request_stop_sequences_renamed():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "stop_sequences": ["END", "STOP"],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["stop"] == ["END", "STOP"]


def test_request_null_sampling_fields_are_left_unset():
    """A client that serializes unset fields sends them as null, as on the chat surface.

    Passed on as None, they made the adapter's clamp raise TypeError before any
    upstream call, and that failure was charged to the endpoint's breaker.
    """
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "temperature": None,
        "top_p": None,
        "stop_sequences": None,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert not {"temperature", "top_p", "stop"} & set(params)


def test_request_tool_use_and_tool_result_round_trip():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "get_weather",
                        "input": {"city": "SF"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "72F sunny",
                    }
                ],
            },
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    # Assistant tool_use becomes assistant message with tool_calls.
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking"
    assert assistant["tool_calls"][0] == {
        "id": "toolu_01",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
    }
    # tool_result becomes a tool-role message.
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "toolu_01"
    assert tool_msg["content"] == "72F sunny"


def _translated_tool_arguments(tool_input):
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "read_dom",
                        "input": tool_input,
                    }
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    return messages[0]["tool_calls"][0]["function"]["arguments"]


def test_request_tool_use_json_object_string_recovered():
    arguments = _translated_tool_arguments('{"tab_id": 7}')
    assert _json.loads(arguments) == {"tab_id": 7}


def test_request_tool_use_malformed_string_falls_back_to_empty_object():
    # Regression: one DeepSeek-V4/SGLang stream emitted this exact malformed
    # fragment, and Claude Code echoed it into every later request in the session.
    arguments = _translated_tool_arguments('{}""')
    assert _json.loads(arguments) == {}


def test_request_tool_use_non_object_values_fall_back_to_empty_object():
    for tool_input in ('["tab"]', '"tab"', "null", ["tab"], None):
        arguments = _translated_tool_arguments(tool_input)
        assert _json.loads(arguments) == {}


# ---------------------------------------------------------------------------
# Response translation: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

from serving.adapters.anthropic_translator import openai_response_to_anthropic


def test_response_text_only():
    resp = {
        "id": "chatcmpl-1",
        "model": "glm-4.7",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "glm-4.7"
    assert out["id"].startswith("msg_")
    assert out["content"] == [{"type": "text", "text": "Hello there"}]
    assert out["stop_reason"] == "end_turn"
    assert out["stop_sequence"] is None
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_response_tool_calls_only():
    resp = {
        "id": "chatcmpl-2",
        "model": "glm-4.7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    assert out["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "SF"},
        }
    ]
    assert out["stop_reason"] == "tool_use"


def test_response_mixed_text_and_tool_calls():
    resp = {
        "id": "chatcmpl-3",
        "model": "glm-4.7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    types = [b["type"] for b in out["content"]]
    assert types == ["text", "tool_use"]


def test_response_finish_reason_map():
    cases = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
        "function_call": "end_turn",  # legacy fallback
    }
    for fr, expected in cases.items():
        resp = {
            "id": "x",
            "model": "m",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": fr}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        assert openai_response_to_anthropic(resp, model="m")["stop_reason"] == expected


def test_response_cached_tokens_mapped():
    resp = {
        "id": "x",
        "model": "m",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["usage"]["cache_read_input_tokens"] == 80
    assert out["usage"]["input_tokens"] == 20


def test_response_top_level_cache_tokens_mapped():
    """UsageInfo.to_dict emits cache fields at the top level — must be picked up."""
    resp = {
        "id": "x",
        "model": "m",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cache_read_tokens": 60,
            "cached_tokens": 60,
            "cache_write_tokens": 10,
        },
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["usage"]["cache_read_input_tokens"] == 60
    assert out["usage"]["cache_creation_input_tokens"] == 10
    # input_tokens disjoint from cache subsets.
    assert out["usage"]["input_tokens"] == 30


def test_response_cache_underflow_clamped_to_zero():
    """Inconsistent upstream usage (cache > prompt) clamps input_tokens to 0."""
    resp = {
        "id": "x",
        "model": "m",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "cache_read_tokens": 80,
        },
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["usage"]["input_tokens"] == 0
    assert out["usage"]["cache_read_input_tokens"] == 80


def test_response_id_preserves_msg_prefix_if_present():
    resp = {
        "id": "msg_already",
        "model": "m",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["id"] == "msg_already"


# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------


def _events(byte_iter):
    """Parse our emitted SSE bytes into a list of (event_name, parsed_json)."""
    text = b"".join(byte_iter).decode("utf-8")
    out = []
    cur_event = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            payload = line[len("data: ") :].strip()
            if payload and payload != "[DONE]":
                out.append((cur_event, _json.loads(payload)))
            cur_event = None
    return out


def _openai_chunk(delta_obj, finish_reason=None, usage=None):
    """Build one OpenAI SSE chunk byte-string."""
    obj = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "model": "m",
        "choices": [{"index": 0, "delta": delta_obj, "finish_reason": finish_reason}],
    }
    if usage is not None:
        obj["usage"] = usage
    return ("data: " + _json.dumps(obj) + "\n\n").encode("utf-8")


def test_stream_text_only():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    chunks = []
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"content": "Hi"}),
        _openai_chunk({"content": " there"}),
        _openai_chunk(
            {},
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        ),
    ):
        chunks.append(b"".join(t.feed(c)))
    chunks.append(b"".join(t.finalize()))
    events = _events(chunks)
    names = [e[0] for e in events]
    assert names[0] == "message_start"
    assert "content_block_start" in names
    assert any(e[0] == "content_block_delta" and e[1]["delta"]["text"] == "Hi" for e in events)
    assert any(e[0] == "content_block_delta" and e[1]["delta"]["text"] == " there" for e in events)
    assert names[-1] == "message_stop"
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["delta"]["stop_reason"] == "end_turn"
    assert msg_delta[1]["usage"]["output_tokens"] == 2


def test_stream_tool_call_fragmented_json():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ]
            }
        ),
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"ci'}}]}),
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty":"SF"}'}}]}),
        _openai_chunk(
            {},
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        ),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    starts = [e for e in events if e[0] == "content_block_start"]
    assert any(s[1]["content_block"]["type"] == "tool_use" for s in starts)
    deltas = [e for e in events if e[0] == "content_block_delta"]
    json_pieces = [
        e[1]["delta"]["partial_json"] for e in deltas if e[1]["delta"]["type"] == "input_json_delta"
    ]
    assert "".join(json_pieces) == '{"city":"SF"}'
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["delta"]["stop_reason"] == "tool_use"


def test_stream_multi_tool_calls():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "a", "arguments": "{}"},
                    }
                ]
            }
        ),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "b", "arguments": "{}"},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    starts = [
        e
        for e in events
        if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use"
    ]
    assert [s[1]["content_block"]["name"] for s in starts] == ["a", "b"]
    indices = [e[1]["index"] for e in events if e[0] == "content_block_stop"]
    assert sorted(set(indices)) == [0, 1]


def test_stream_empty_response():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    out += b"".join(
        t.feed(
            _openai_chunk(
                {},
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            )
        )
    )
    out += b"".join(t.finalize())
    events = _events([out])
    names = [e[0] for e in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"


def test_stream_done_sentinel_ignored():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    out += b"".join(t.feed(_openai_chunk({"role": "assistant"})))
    out += b"".join(t.feed(_openai_chunk({"content": "x"})))
    out += b"".join(t.feed(b"data: [DONE]\n\n"))
    out += b"".join(t.finalize())
    events = _events([out])
    assert any(e[0] == "message_stop" for e in events)


# ---------------------------------------------------------------------------
# Anthropic-native SSE usage extraction
# ---------------------------------------------------------------------------


def test_stream_text_then_tool_then_text_emits_correct_block_ordering():
    """Text -> tool -> text interleaving must close tool block before opening second text block.

    Anthropic SSE requires exactly one content block open at a time and
    content_block_stop events emitted in the order blocks were opened.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        # First text segment.
        _openai_chunk({"content": "Let me check."}),
        # Tool call opens — text block must be closed first.
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                    }
                ]
            }
        ),
        # Second text segment after tool — tool block must be closed first.
        _openai_chunk({"content": "Done!"}),
        _openai_chunk({}, finish_reason="stop"),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])

    names = [e[0] for e in events]
    # Must start with message_start.
    assert names[0] == "message_start"
    # Must end with message_stop.
    assert names[-1] == "message_stop"

    # Extract indices for starts to verify ordering.
    starts = [
        (e[1]["index"], e[1]["content_block"]["type"])
        for e in events
        if e[0] == "content_block_start"
    ]

    # Expect: text(0), tool(1), text(2) — three distinct blocks.
    assert len(starts) == 3
    assert starts[0] == (0, "text")
    assert starts[1] == (1, "tool_use")
    assert starts[2] == (2, "text")

    # Each block must have a matching stop, and stops precede the next start.
    # Block 0 (text) stopped before block 1 (tool) opened.
    start_event_positions = {
        e_data["index"]: i
        for i, (e_name, e_data) in enumerate(events)
        if e_name == "content_block_start"
    }
    stop_event_positions: dict[int, int] = {}
    for i, (e_name, e_data) in enumerate(events):
        if e_name == "content_block_stop":
            stop_event_positions[e_data["index"]] = i

    # stop(0) < start(1) < stop(1) < start(2)
    assert stop_event_positions[0] < start_event_positions[1]
    assert stop_event_positions[1] < start_event_positions[2]

    # Both text segments present.
    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta"
    ]
    assert "Let me check." in text_deltas
    assert "Done!" in text_deltas


def test_stream_text_mid_tool_call_does_not_reopen_an_orphan_tool_block():
    """Issue #876 P1: text interleaved with one tool call's argument fragments.

    Some providers emit a content fragment between two argument-continuation
    deltas of the SAME tool call (index 0). The arguments are still incomplete,
    so the text cannot be forwarded without closing the open tool block and
    later reopening an orphan for the continuation. It is queued in arrival
    order and flushed after the tool block closes; the next argument fragment
    for the same OpenAI index continues the block that was already started.
    The old code cleared its open-tool map on text, so the continuation
    reopened an orphan tool_use block with an empty id and name — an
    unparseable call on the client.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city"'},
                    }
                ]
            }
        ),
        # Interleaved text mid-tool-call.
        _openai_chunk({"content": "Checking the weather..."}),
        # Continuation of the SAME tool call (same index 0).
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": ':"SF"}'}}]}),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])

    tool_starts = [
        e
        for e in events
        if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use"
    ]
    # Exactly ONE tool block for the one upstream call — not a reopened orphan.
    assert len(tool_starts) == 1
    assert tool_starts[0][1]["content_block"]["id"] == "call_1"
    assert tool_starts[0][1]["content_block"]["name"] == "get_weather"

    # The two argument fragments join into one valid JSON object on the
    # single tool block.
    json_pieces = [
        e[1]["delta"]["partial_json"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta"
    ]
    assert "".join(json_pieces) == '{"city":"SF"}'
    assert all(
        e[1]["index"] == tool_starts[0][1]["index"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta"
    )

    # The interleaved text still made it out as its own text block.
    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta"
    ]
    assert text_deltas == ["Checking the weather..."]

    # And the wire still respects one-open-block-at-a-time with ordered stops.
    open_at = set()
    for name, data in events:
        if name == "content_block_start":
            assert data["index"] not in open_at, f"block {data['index']} reopened"
            open_at.add(data["index"])
        elif name == "content_block_stop":
            open_at.discard(data["index"])
    assert not open_at, "a block never closed"


def test_stream_text_between_two_tool_calls_flushes_before_second_block():
    """Content buffered behind tool 0 is emitted before tool 1 opens."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city"'},
                    }
                ]
            }
        ),
        _openai_chunk({"content": "between tools"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for chunk in parts:
        out += b"".join(t.feed(chunk))
    out += b"".join(t.finalize())
    events = _events([out])

    starts = [
        (event["index"], event["content_block"]["type"])
        for name, event in events
        if name == "content_block_start"
    ]
    assert starts == [(0, "tool_use"), (1, "text"), (2, "tool_use")]

    open_block: set[int] = set()
    for name, event in events:
        if name == "content_block_start":
            assert not open_block, f"overlapping block at {event['index']}"
            open_block.add(event["index"])
        elif name == "content_block_stop":
            open_block.remove(event["index"])


def test_stream_thinking_between_two_tool_calls_flushes_before_second_block():
    """Thinking and text buffered behind tool 0 precede tool 1 without overlap."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ]
            }
        ),
        _openai_chunk({"reasoning_content": "reason first"}),
        _openai_chunk({"content": "then text"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for chunk in parts:
        out += b"".join(t.feed(chunk))
    out += b"".join(t.finalize())
    events = _events([out])

    starts = [
        (event["index"], event["content_block"]["type"])
        for name, event in events
        if name == "content_block_start"
    ]
    assert starts == [(0, "tool_use"), (1, "thinking"), (2, "text"), (3, "tool_use")]

    open_block: set[int] = set()
    for name, event in events:
        if name == "content_block_start":
            assert not open_block, f"overlapping block at {event['index']}"
            open_block.add(event["index"])
        elif name == "content_block_stop":
            open_block.remove(event["index"])


def test_stream_thinking_mid_tool_call_does_not_reopen_an_orphan_tool_block():
    """Issue #876 P1, thinking variant: reasoning interleaved mid-arguments.

    DeepSeek-R1-style providers emit reasoning_content fragments between
    argument-continuation deltas of the same tool call. Closing the tool
    block there used to orphan it exactly like the text variant: the next
    fragment reopened a tool_use with an empty id and name. The reasoning
    buffers behind the open tool block and flushes in arrival order with any
    interleaved text once the tool block closes.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city"'},
                    }
                ]
            }
        ),
        _openai_chunk({"reasoning_content": "Need the city name."}),
        _openai_chunk({"content": "Checking..."}),
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": ':"SF"}'}}]}),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])

    tool_starts = [
        e
        for e in events
        if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0][1]["content_block"]["id"] == "call_1"
    json_pieces = [
        e[1]["delta"]["partial_json"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta"
    ]
    assert "".join(json_pieces) == '{"city":"SF"}'

    thinking_deltas = [
        e[1]["delta"]["thinking"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "thinking_delta"
    ]
    assert thinking_deltas == ["Need the city name."]
    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta"
    ]
    assert text_deltas == ["Checking..."]

    # Ordering between the flushed blocks: thinking chronologically preceded
    # the text, so its block must start before the text block's.
    block_order = [
        (e[1]["index"], e[1]["content_block"]["type"])
        for e in events
        if e[0] == "content_block_start"
    ]
    assert block_order == [(0, "tool_use"), (1, "thinking"), (2, "text")], block_order

    # One start per index; every block that starts closes; nothing reopens.
    open_at = set()
    for name, data in events:
        if name == "content_block_start":
            assert data["index"] not in open_at, f"block {data['index']} reopened"
            open_at.add(data["index"])
        elif name == "content_block_stop":
            open_at.discard(data["index"])
    assert not open_at, "a block never closed"


def _assert_anthropic_lifecycle(events) -> None:
    """Exactly one content block is open at a time and every start closes."""
    open_at: set[int] = set()
    for name, data in events:
        if name == "content_block_start":
            assert not open_at, f"block {data['index']} opened while {open_at} was open"
            open_at.add(data["index"])
        elif name == "content_block_stop":
            assert data["index"] in open_at, f"block {data['index']} stopped but was not open"
            open_at.discard(data["index"])
    assert not open_at, f"blocks never closed: {open_at}"


def _tool_start_and_args(events):
    starts = [
        (data["index"], data["content_block"]["id"], data["content_block"]["name"])
        for name, data in events
        if name == "content_block_start" and data["content_block"]["type"] == "tool_use"
    ]
    args: dict[int, str] = {}
    for name, data in events:
        if name != "content_block_delta":
            continue
        delta = data["delta"]
        if delta.get("type") == "input_json_delta":
            args[data["index"]] = args.get(data["index"], "") + delta["partial_json"]
    return starts, args


def _block_starts(events):
    """Ordered content-block identities: (type, tool id, tool name)."""
    starts = []
    for name, data in events:
        if name != "content_block_start":
            continue
        block = data["content_block"]
        starts.append((block["type"], block.get("id"), block.get("name")))
    return starts


def _tool_args_chunk(index: int, arguments: str):
    """One OpenAI tool-argument continuation for an already-started call."""
    return _openai_chunk({"tool_calls": [{"index": index, "function": {"arguments": arguments}}]})


def test_stream_argument_completion_immediately_releases_deferred_content():
    """A completed input releases waiting content without another frame or EOF."""
    translator = OpenAIToAnthropicStreamTranslator(model="m")
    list(
        translator.feed(
            _openai_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"x":'},
                        }
                    ]
                }
            )
        )
    )
    list(translator.feed(_openai_chunk({"content": "waiting"})))
    released = list(
        translator.feed(
            _openai_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": "1}"},
                        }
                    ]
                }
            )
        )
    )
    assert any(event.get("delta", {}).get("text") == "waiting" for _, event in _events(released))
    assert not any(name == "message_stop" for name, _ in _events(released))


def test_stream_complete_tool_then_text_thinking_text_streams_in_arrival_order():
    """Complete args must not hold A/B/C until finalize, and typing keeps order.

    A fixed thinking-first flush would emit reasoning B before text A. The
    client's baseline (no tool interleaving) is A, B, C: the fragments must
    keep their arrival order even though they were deferred behind the tool.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    tool_out = b"".join(
        t.feed(
            _openai_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"x":1}'},
                        }
                    ]
                }
            )
        )
    )
    # Every one of these is safe once the JSON above is complete; each must
    # produce its own delta before finalize is ever called.
    text_a = b"".join(t.feed(_openai_chunk({"content": "A"})))
    thinking_b = b"".join(t.feed(_openai_chunk({"reasoning_content": "B"})))
    text_c = b"".join(t.feed(_openai_chunk({"content": "C"})))

    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("text") == "A"
        for e in _events([text_a])
    )
    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("thinking") == "B"
        for e in _events([thinking_b])
    )
    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("text") == "C"
        for e in _events([text_c])
    )

    events = _events([tool_out, text_a, thinking_b, text_c, b"".join(t.finalize())])
    starts = [
        data["content_block"]["type"] for name, data in events if name == "content_block_start"
    ]
    assert starts == ["tool_use", "text", "thinking", "text"]

    emitted = []
    for name, data in events:
        if name != "content_block_delta":
            continue
        delta = data["delta"]
        if delta.get("type") == "text_delta":
            emitted.append(("text", delta["text"]))
        elif delta.get("type") == "thinking_delta":
            emitted.append(("thinking", delta["thinking"]))
    assert emitted == [("text", "A"), ("thinking", "B"), ("text", "C")]

    starts_with_id, args = _tool_start_and_args(events)
    assert len(starts_with_id) == 1
    assert starts_with_id[0][1:] == ("call_1", "lookup")
    assert list(args.values()) == ['{"x":1}']
    _assert_anthropic_lifecycle(events)


def test_stream_reasoning_immediately_after_complete_tool_streams_before_finalize():
    """Reasoning after a complete tool call must not wait for finalize either."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                )
            )
        )
    ]
    reasoning_out = b"".join(t.feed(_openai_chunk({"reasoning_content": "thinking now"})))
    out.append(reasoning_out)
    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("thinking") == "thinking now"
        for e in _events([reasoning_out])
    )
    out.append(b"".join(t.finalize()))
    events = _events(out)

    starts = [
        data["content_block"]["type"] for name, data in events if name == "content_block_start"
    ]
    assert starts == ["tool_use", "thinking"]
    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("thinking") == "thinking now"
        for e in events
    )
    _assert_anthropic_lifecycle(events)


@pytest.mark.parametrize(
    "fragments",
    [
        [("content", "A"), ("reasoning_content", "B"), ("content", "C")],
        [("reasoning_content", "B"), ("content", "A"), ("reasoning_content", "C")],
    ],
    ids=["text-thinking-text", "thinking-text-thinking"],
)
def test_stream_incomplete_tool_interleaved_fragments_keep_arrival_order(fragments):
    """Genuinely incomplete args defer typed fragments, not arrival order."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"a":'},
                            }
                        ]
                    }
                )
            )
        )
    ]
    for position, (field, value) in enumerate(fragments):
        chunk = b"".join(t.feed(_openai_chunk({field: value})))
        out.append(chunk)
        if position == 0:
            # Buffered content must still tell the router the upstream is
            # alive, without forwarding an invented content delta.
            assert b": keepalive" in chunk
            assert b"text_delta" not in chunk
            assert b"thinking_delta" not in chunk

    out.append(b"".join(t.feed(_tool_args_chunk(0, "1}"))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    expected_types = [
        "thinking" if field == "reasoning_content" else "text" for field, _ in fragments
    ]
    starts = [
        data["content_block"]["type"] for name, data in events if name == "content_block_start"
    ]
    assert starts == ["tool_use", *expected_types]

    emitted = []
    for name, data in events:
        if name != "content_block_delta":
            continue
        delta = data["delta"]
        if delta.get("type") == "text_delta":
            emitted.append(("text", delta["text"]))
        elif delta.get("type") == "thinking_delta":
            emitted.append(("thinking", delta["thinking"]))
    expected_values = [
        ("thinking" if field == "reasoning_content" else "text", value)
        for field, value in fragments
    ]
    assert emitted == expected_values

    tool_starts, args = _tool_start_and_args(events)
    assert len(tool_starts) == 1
    assert tool_starts[0][1:] == ("call_1", "lookup")
    assert list(args.values()) == ['{"a":1}']
    _assert_anthropic_lifecycle(events)


def test_stream_split_json_escapes_and_trailing_whitespace_do_not_reopen_tool():
    """Completeness survives escaped quotes and harmless trailing whitespace."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = []
    tool_chunks = [
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "write", "arguments": '{"city":"S'},
                    }
                ]
            }
        ),
        _openai_chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": 'F","note":"a\\"b"}'}}]}
        ),
        _tool_args_chunk(0, " "),
    ]
    for chunk in tool_chunks:
        out.append(b"".join(t.feed(chunk)))

    text_out = b"".join(t.feed(_openai_chunk({"content": "after tool"})))
    assert any(
        e[0] == "content_block_delta" and e[1]["delta"].get("text") == "after tool"
        for e in _events([text_out])
    )
    # Whitespace arriving after the block was sealed carries no value bytes.
    out.append(text_out)
    out.append(b"".join(t.feed(_tool_args_chunk(0, "\n"))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    tool_starts, args = _tool_start_and_args(events)
    assert len(tool_starts) == 1
    assert tool_starts[0][1:] == ("call_1", "write")
    assert next(iter(args.values())).strip() == '{"city":"SF","note":"a\\"b"}'
    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["after tool"]
    _assert_anthropic_lifecycle(events)


def test_stream_non_whitespace_after_complete_json_is_not_treated_as_complete():
    """Trailing invalid bytes keep the arguments incomplete and are preserved."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"a":1}'},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_tool_args_chunk(0, "x"))))
    deferred_text = b"".join(t.feed(_openai_chunk({"content": "must wait"})))
    assert b": keepalive" in deferred_text
    assert b"text_delta" not in deferred_text
    out.append(deferred_text)
    out.append(b"".join(t.finalize()))
    events = _events(out)

    tool_starts, args = _tool_start_and_args(events)
    assert len(tool_starts) == 1
    assert list(args.values()) == ['{"a":1}x']
    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["must wait"]
    _assert_anthropic_lifecycle(events)


def test_stream_non_whitespace_after_closed_tool_raises_not_second_call():
    """A complete JSON object cannot be extended after its block closed.

    Emitting the suffix in a second tool_use block with the same id would look
    like a duplicate invocation to the client, so this is a protocol error.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"x":1}'},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_openai_chunk({"content": "answer"}))))
    with pytest.raises(ValueError, match="non-whitespace arguments"):
        list(t.feed(_tool_args_chunk(0, '{"x":2}')))

    # The failed stream can still clean up already-received content, but it is
    # terminal and must not be revivable into a successful message.
    out.append(b"".join(t.abort()))
    assert list(t.finalize()) == []
    events = _events(out)
    starts, args = _tool_start_and_args(events)
    assert len(starts) == 1
    assert list(args.values()) == ['{"x":1}']
    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["answer"]
    _assert_anthropic_lifecycle(events)


def test_translator_abort_is_terminal_and_idempotent():
    """After abort, neither abort/finalize nor feed may revive the stream."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"x":'},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_openai_chunk({"content": "received before error"}))))

    out.append(b"".join(t.abort()))
    assert b'"text": "received before error"' in b"".join(out)
    assert list(t.abort()) == []
    assert list(t.finalize()) == []
    assert list(t.feed(_openai_chunk({"content": "must not appear"}))) == []

    events = _events(out)
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)
    assert not any(
        name == "content_block_delta" and data["delta"].get("text") == "must not appear"
        for name, data in events
    )
    _assert_anthropic_lifecycle(events)


def test_stream_tc0_text_tc1_tc0_continuation_keeps_earlier_call():
    """tc1 starting must not close/forget an incomplete tc0.

    Previously the text flushed at the new tool index, all tool state was
    cleared, and tc0's continuation reopened an empty id/name block.
    """
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "first", "arguments": '{"a":'},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_openai_chunk({"content": "between"}))))
    out.append(
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "second", "arguments": '{"b":2}'},
                            }
                        ]
                    }
                )
            )
        )
    )
    out.append(b"".join(t.feed(_tool_args_chunk(0, "1}"))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    starts = [
        (data["index"], data["content_block"]["id"], data["content_block"]["name"])
        for name, data in events
        if name == "content_block_start" and data["content_block"]["type"] == "tool_use"
    ]
    assert len(starts) == 2
    assert starts[0][1:] == ("call_0", "first")
    assert starts[1][1:] == ("call_1", "second")
    assert all(call_id and call_name for _, call_id, call_name in starts), starts

    _, args = _tool_start_and_args(events)
    assert args[starts[0][0]] == '{"a":1}'
    assert args[starts[1][0]] == '{"b":2}'

    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["between"]
    _assert_anthropic_lifecycle(events)


def test_stream_back_to_back_parallel_tools_keep_all_args_without_overlap():
    """Back-to-back calls must stop one block before the next starts."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = []
    for delta in (
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "first", "arguments": '{"a":'},
                }
            ]
        },
        {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]},
        {
            "tool_calls": [
                {
                    "index": 1,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "second", "arguments": '{"b":'},
                }
            ]
        },
        {"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]},
        {},
    ):
        out.append(b"".join(t.feed(_openai_chunk(delta))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    starts, args = _tool_start_and_args(events)
    assert [start[1:] for start in starts] == [("call_0", "first"), ("call_1", "second")]
    assert args[starts[0][0]] == '{"a":1}'
    assert args[starts[1][0]] == '{"b":2}'
    _assert_anthropic_lifecycle(events)


def _feed_two_call_prefix(t):
    """tc0 incomplete, tc1 complete pending, then tc0 completes.

    The completion of tc0 must release tc1 before anything later can overtake
    it. Frames are returned for the full event stream assertions.
    """
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "first", "arguments": '{"x":'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "second", "arguments": '{"x":1}'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(t.feed(_tool_args_chunk(0, "0}"))),
    ]
    return out


def test_stream_parallel_tool_markers_keep_first_seen_fifo_order():
    """Trace 1: call_1 must not be overtaken by a later call_2."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = _feed_two_call_prefix(t)
    out.append(
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 2,
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "third", "arguments": '{"x":2}'},
                            }
                        ]
                    }
                )
            )
        )
    )
    out.append(b"".join(t.finalize()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("tool_use", "call_1", "second"),
        ("tool_use", "call_2", "third"),
    ]
    starts, args = _tool_start_and_args(events)
    by_id = {call_id: args[index] for index, call_id, _ in starts}
    assert by_id == {
        "call_0": '{"x":0}',
        "call_1": '{"x":1}',
        "call_2": '{"x":2}',
    }
    _assert_anthropic_lifecycle(events)


def test_stream_text_after_pending_parallel_tool_stays_after_it():
    """Trace 2: text after both tools must not jump ahead of call_1."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = _feed_two_call_prefix(t)
    out.append(b"".join(t.feed(_openai_chunk({"content": "after both tools"}))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("tool_use", "call_1", "second"),
        ("text", None, None),
    ]
    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["after both tools"]
    _assert_anthropic_lifecycle(events)


def test_stream_abort_preserves_complete_pending_parallel_tool():
    """Trace 3: a pending complete call must not be dropped by abort()."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = _feed_two_call_prefix(t)
    out.append(b"".join(t.abort()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("tool_use", "call_1", "second"),
    ]
    starts, args = _tool_start_and_args(events)
    by_id = {call_id: args[index] for index, call_id, _ in starts}
    assert by_id == {"call_0": '{"x":0}', "call_1": '{"x":1}'}
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)
    _assert_anthropic_lifecycle(events)


def test_stream_mixed_content_straddling_pending_tool_keeps_fifo_order():
    """Text before a pending marker stays before it; text after stays after."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "first", "arguments": '{"a":'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(t.feed(_openai_chunk({"content": "before"}))),
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "second", "arguments": '{"b":1}'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(t.feed(_openai_chunk({"content": "after"}))),
        b"".join(t.feed(_tool_args_chunk(0, "0}"))),
    ]
    out.append(b"".join(t.finalize()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("text", None, None),
        ("tool_use", "call_1", "second"),
        ("text", None, None),
    ]
    text_values = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"].get("type") == "text_delta"
    ]
    assert text_values == ["before", "after"]
    starts, args = _tool_start_and_args(events)
    by_id = {call_id: args[index] for index, call_id, _ in starts}
    assert by_id == {"call_0": '{"a":0}', "call_1": '{"b":1}'}
    _assert_anthropic_lifecycle(events)


def test_stream_three_parallel_tools_drain_in_marker_order_not_completion_order():
    """All three calls keep first-seen order even when they complete out of order."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "first", "arguments": '{"a":'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "second", "arguments": '{"b":'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 2,
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "third", "arguments": '{"c":2}'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(t.feed(_tool_args_chunk(1, "1}"))),
        b"".join(t.feed(_tool_args_chunk(0, "0}"))),
    ]
    out.append(b"".join(t.finalize()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("tool_use", "call_1", "second"),
        ("tool_use", "call_2", "third"),
    ]
    starts, args = _tool_start_and_args(events)
    by_id = {call_id: args[index] for index, call_id, _ in starts}
    assert by_id == {
        "call_0": '{"a":0}',
        "call_1": '{"b":1}',
        "call_2": '{"c":2}',
    }
    _assert_anthropic_lifecycle(events)


# -- incremental argument completeness --------------------------------------


def _reference_arguments_complete(arguments: str) -> bool:
    """The completeness definition the incremental tracker must reproduce."""
    if not arguments.strip():
        return False
    try:
        return isinstance(_json.loads(arguments), dict)
    except ValueError:
        return False


_ARGUMENT_CORPUS = [
    "",
    "   \n\t",
    "{}",
    " {} \n",
    '{"a":1}',
    '{"a":1} x',
    '{"a":1}}',
    '{"a":1}{}',
    '{"s":"}"}',
    '{"s":"{"}',
    '{"s":"a\\"}"}',
    '{"s":"a\\\\"}',
    '{"s":"\\\\\\""}',
    '{"u":"\\u007d\\u0022"}',
    '{"a":{"b":[1,{"c":"]"}],"d":[]}}',
    '{"a":[}',
    '{"a":]}',
    "{]",
    '{"a":}',
    "{'a':1}",
    '{"a":1,}',
    '{"a":NaN}',
    '{"a":"line\nbreak"}',
    '{"a":"x',
    '{"a":"x\\',
    "[1,2]",
    '"str"',
    "123",
    "null",
    chr(0xA0) + "{}",  # Unicode, not JSON, whitespace
    "{}" + chr(0xA0),
    chr(0xFEFF) + "{}",  # a BOM json.loads refuses
    '{"a":"é\U0001f600"}',
]


@pytest.mark.parametrize("arguments", _ARGUMENT_CORPUS)
def test_tool_arguments_completeness_matches_json_on_every_split(arguments):
    """Every prefix, under every two-way split, agrees with a full json.loads.

    Covers quoted braces, escapes (including one split between fragments),
    nesting, JSON-vs-Unicode whitespace, trailing data and non-objects.
    """
    for split in range(len(arguments) + 1):
        tracked = _ToolArguments()
        for fragment in (arguments[:split], arguments[split:]):
            if fragment:
                tracked.append(fragment)
        assert tracked.text() == arguments
        assert tracked.complete() is _reference_arguments_complete(arguments), (
            arguments,
            split,
        )

    # One character per fragment, checked after every one of them.
    tracked = _ToolArguments()
    for position, char in enumerate(arguments, start=1):
        tracked.append(char)
        assert tracked.complete() is _reference_arguments_complete(arguments[:position]), (
            arguments,
            position,
        )


@pytest.mark.parametrize("seed", range(8))
def test_tool_arguments_completeness_matches_json_on_random_streams(seed):
    """Random JSON-ish byte streams and random fragmenting: exact agreement."""
    import random

    rng = random.Random(seed)
    alphabet = '{}[]":,\\ \nab1'
    for _ in range(400):
        tokens = ['{"k":', '"', "\\", "{", "}", "[", "]", ",", ":", " ", "1", "a", '"x"']
        if rng.random() < 0.5:
            arguments = "".join(rng.choice(tokens) for _ in range(rng.randint(0, 12)))
        else:
            arguments = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        if rng.random() < 0.3:
            arguments = _json.dumps({"v": arguments, "n": [arguments]}) + rng.choice(
                ["", " ", "\n", "x", "}"]
            )
        tracked = _ToolArguments()
        received = ""
        while len(received) < len(arguments):
            fragment = arguments[len(received) : len(received) + rng.randint(1, 4)]
            tracked.append(fragment)
            received += fragment
            assert tracked.complete() is _reference_arguments_complete(received), (
                seed,
                received,
            )


def test_stream_empty_initial_arguments_are_not_closed_by_later_events():
    """A call that opened with no arguments yet must still receive them."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "first", "arguments": ""},
                            }
                        ]
                    }
                )
            )
        )
    ]
    text_out = b"".join(t.feed(_openai_chunk({"content": "after first"})))
    assert b": keepalive" in text_out
    assert b"text_delta" not in text_out
    out.append(text_out)
    out.append(
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "second", "arguments": ""},
                            }
                        ]
                    }
                )
            )
        )
    )
    # Whitespace alone is still not an arguments value.
    out.append(b"".join(t.feed(_tool_args_chunk(0, " "))))
    assert b"text_delta" not in out[-1]
    out.append(b"".join(t.feed(_tool_args_chunk(0, '{"a":1}'))))
    out.append(b"".join(t.feed(_tool_args_chunk(1, '{"b":2}'))))
    out.append(b"".join(t.finalize()))
    events = _events(out)

    assert _block_starts(events) == [
        ("tool_use", "call_0", "first"),
        ("text", None, None),
        ("tool_use", "call_1", "second"),
    ]
    starts, args = _tool_start_and_args(events)
    by_id = {call_id: args[index] for index, call_id, _ in starts}
    assert by_id == {"call_0": ' {"a":1}', "call_1": '{"b":2}'}
    _assert_anthropic_lifecycle(events)


@pytest.mark.parametrize(
    ("fragments", "expected"),
    [
        # A brace inside a string, then an escaped quote split at a boundary.
        (['{"s":"a}', "\\", '""}'], '{"s":"a}\\""}'),
        # An escaped backslash ends exactly at a boundary: the next quote is real.
        (['{"s":"a\\\\', '"', "}"], '{"s":"a\\\\"}'),
        # Nested objects/arrays with brackets inside strings.
        (['{"a":{"b":[1,', '{"c":"]}"}', "]}", "}"], '{"a":{"b":[1,{"c":"]}"}]}}'),
    ],
    ids=["quoted-brace-split-escape", "split-escaped-backslash", "nested"],
)
def test_stream_lexical_edges_hold_content_until_the_object_closes(fragments, expected):
    """Content waits until exactly the fragment that closes the object."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "write", "arguments": fragments[0]},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_openai_chunk({"content": "after"}))))
    for position, fragment in enumerate(fragments[1:], start=2):
        chunk = b"".join(t.feed(_tool_args_chunk(0, fragment)))
        out.append(chunk)
        released = any(
            data["delta"].get("text") == "after"
            for name, data in _events([chunk])
            if name == "content_block_delta"
        )
        assert released is (position == len(fragments)), (position, fragment)
    out.append(b"".join(t.finalize()))
    events = _events(out)

    starts, args = _tool_start_and_args(events)
    assert [start[1:] for start in starts] == [("call_0", "write")]
    assert list(args.values()) == [expected]
    assert _json.loads(expected)
    _assert_anthropic_lifecycle(events)


@pytest.mark.parametrize(
    "fragments",
    [['{"a":', "}"], ["{'a'", ":1}"], ['{"a":[', "}"], ["[1,", "2]"]],
    ids=["missing-value", "single-quotes", "mismatched-bracket", "array"],
)
def test_stream_malformed_arguments_keep_content_queued_until_finalize(fragments):
    """A closing brace on malformed JSON is not completeness; bytes are kept."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "write", "arguments": fragments[0]},
                            }
                        ]
                    }
                )
            )
        )
    ]
    out.append(b"".join(t.feed(_openai_chunk({"content": "after"}))))
    for fragment in fragments[1:]:
        chunk = b"".join(t.feed(_tool_args_chunk(0, fragment)))
        assert b"text_delta" not in chunk
        out.append(chunk)
    out.append(b"".join(t.finalize()))
    events = _events(out)

    starts, args = _tool_start_and_args(events)
    assert len(starts) == 1
    assert list(args.values()) == ["".join(fragments)]
    assert _block_starts(events)[-1] == ("text", None, None)
    _assert_anthropic_lifecycle(events)


def _large_tool_stream(mode: str, fragment_count: int):
    """A Write-like call growing by 128-byte fragments, optionally with a queue.

    ``text`` queues a text fragment and ``parallel`` a second call behind the
    open one, so every fragment makes the drain ask whether the open call is
    complete -- the case that used to re-parse the whole prefix each time.
    """
    frames = [
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_0",
                        "type": "function",
                        "function": {"name": "write", "arguments": '{"file":"'},
                    }
                ]
            }
        )
    ]
    if mode == "text":
        frames.append(_openai_chunk({"content": "after"}))
    elif mode == "parallel":
        frames.append(
            _openai_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ]
                }
            )
        )
    body = 'x\\"{}[]\\n' * 12 + "y" * 20
    assert len(body) == 128
    frames.extend(_tool_args_chunk(0, body) for _ in range(fragment_count))
    frames.append(_tool_args_chunk(0, '"}'))
    return frames, 9 + 128 * fragment_count + 2


@pytest.mark.parametrize("mode", ["none", "text", "parallel"])
def test_stream_large_arguments_do_linear_parse_and_join_work(mode, monkeypatch):
    """Deterministic work counters, not timing: each byte is parsed/joined O(1) times.

    Counts every ``json.loads`` of tool-argument text (whatever helper calls
    it) and every byte materialized by joining argument fragments. The old
    path parsed the full prefix once per fragment while text or a second call
    was queued, and copied the full prefix once per fragment on append.
    """
    real_loads = _json.loads
    parsed = {"calls": 0, "bytes": 0}

    def counting_loads(s, *args, **kwargs):
        if isinstance(s, str) and s.startswith('{"file"'):
            parsed["calls"] += 1
            parsed["bytes"] += len(s)
        return real_loads(s, *args, **kwargs)

    real_text = _ToolArguments.text
    joined = {"calls": 0, "bytes": 0}

    def counting_text(self):
        value = real_text(self)
        joined["calls"] += 1
        joined["bytes"] += len(value)
        return value

    def run(fragment_count):
        parsed.update(calls=0, bytes=0)
        joined.update(calls=0, bytes=0)
        frames, total = _large_tool_stream(mode, fragment_count)
        t = OpenAIToAnthropicStreamTranslator(model="m")
        out = []
        for position, frame in enumerate(frames):
            chunk = b"".join(t.feed(frame))
            out.append(chunk)
            if position >= len(frames) - fragment_count - 1 and position < len(frames) - 1:
                # The open call's deltas keep streaming as they arrive.
                assert b"input_json_delta" in chunk
        out.append(b"".join(t.finalize()))
        return _events(out), total, dict(parsed), dict(joined)

    monkeypatch.setattr(_json, "loads", counting_loads)
    monkeypatch.setattr(_ToolArguments, "text", counting_text)
    small = run(256)
    large = run(1024)
    monkeypatch.undo()

    for events, total, parse_work, join_work in (small, large):
        starts, args = _tool_start_and_args(events)
        assert len(args[starts[0][0]]) == total
        assert real_loads(args[starts[0][0]])["file"].startswith('x"{}[]\n')
        assert parse_work["calls"] <= 1
        assert parse_work["bytes"] <= total
        # The open call is joined at most twice (first emission, validation).
        assert join_work["calls"] <= 4
        assert join_work["bytes"] <= 2 * total + 64
        _assert_anthropic_lifecycle(events)
        if mode == "text":
            assert _block_starts(events)[-1] == ("text", None, None)
        if mode == "parallel":
            assert [start[1:] for start in starts] == [("call_0", "write"), ("call_1", "read")]

    # Four times the input costs at most about four times the work.
    assert large[2]["bytes"] <= 4 * small[2]["bytes"] + 64
    assert large[3]["bytes"] <= 4 * small[3]["bytes"] + 64


# -- tool-call index validation ----------------------------------------------


def _tool_chunk_with_index(index, *, call_id="call_bad", arguments='{"a":1}'):
    tc = {"id": call_id, "type": "function", "function": {"name": "bad", "arguments": arguments}}
    tc["index"] = index
    return _openai_chunk({"tool_calls": [tc]})


@pytest.mark.parametrize(
    "index",
    [None, "0", "1", 0.0, 1.5, True, False, [0], {"i": 0}],
    ids=["null", "str0", "str1", "float0", "float", "true", "false", "list", "dict"],
)
def test_stream_non_integer_tool_index_is_rejected_before_any_state(index):
    """An explicit non-integer index is a protocol error, not call 0."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    started = b"".join(t.feed(_openai_chunk({"content": "before"})))
    with pytest.raises(ValueError, match="tool call index must be an integer"):
        list(t.feed(_tool_chunk_with_index(index)))
    assert t._tool_calls == {}
    assert all(kind != "tool" for kind, _ in t._pending_events)

    events = _events([started, b"".join(t.abort())])
    assert not any(
        name == "content_block_start" and data["content_block"]["type"] == "tool_use"
        for name, data in events
    )
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)
    assert list(t.finalize()) == []
    _assert_anthropic_lifecycle(events)


def test_stream_invalid_index_behind_open_tool_aborts_with_a_closed_lifecycle():
    """Rejection mid-stream drains received content and closes every block."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(t.feed(_tool_chunk_with_index(0, call_id="call_0", arguments='{"a":'))),
        b"".join(t.feed(_openai_chunk({"content": "queued"}))),
    ]
    with pytest.raises(ValueError, match="tool call index must be an integer"):
        list(t.feed(_tool_chunk_with_index(None)))
    out.append(b"".join(t.abort()))
    events = _events(out)

    assert _block_starts(events) == [("tool_use", "call_0", "bad"), ("text", None, None)]
    _, args = _tool_start_and_args(events)
    assert list(args.values()) == ['{"a":']
    assert not any(name in ("message_delta", "message_stop") for name, _ in events)
    _assert_anthropic_lifecycle(events)


def test_stream_missing_tool_index_still_means_call_zero():
    """Compatibility: providers that omit the index stream a single call."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = [
        b"".join(
            t.feed(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"a":'},
                            }
                        ]
                    }
                )
            )
        ),
        b"".join(t.feed(_openai_chunk({"tool_calls": [{"function": {"arguments": "1}"}}]}))),
        b"".join(t.finalize()),
    ]
    events = _events(out)
    starts, args = _tool_start_and_args(events)
    assert [start[1:] for start in starts] == [("call_0", "lookup")]
    assert list(args.values()) == ['{"a":1}']
    _assert_anthropic_lifecycle(events)


def test_extract_usage_from_anthropic_sse():
    chunk = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"x","usage":{"input_tokens":42,'
        b'"cache_creation_input_tokens":3,"cache_read_input_tokens":7}}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","usage":{"output_tokens":99}}\n\n'
    )
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    extract_anthropic_usage_from_sse(chunk, usage)
    assert usage == {
        "input_tokens": 42,
        "output_tokens": 99,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 7,
    }


def test_extract_usage_cache_tokens_from_message_delta():
    """Cache fields can also arrive on the final message_delta — must be extracted."""
    chunk = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"x","usage":{"input_tokens":10}}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","usage":{"output_tokens":99,'
        b'"cache_read_input_tokens":40,"cache_creation_input_tokens":5}}\n\n'
    )
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    extract_anthropic_usage_from_sse(chunk, usage)
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 99,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 40,
    }


def test_extract_usage_silently_ignores_garbage():
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    extract_anthropic_usage_from_sse(b"garbage\n\n", usage)
    assert usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Anthropic-surface correctness fixes (confirmed-finding regression tests)
# ---------------------------------------------------------------------------


def test_request_tool_result_and_text_ordering():
    """C9: tool messages must precede user text so tool/assistant adjacency holds."""
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"},
                    {"type": "text", "text": "<system-reminder>be brief</system-reminder>"},
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    # tool message first, then the user text message.
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "toolu_1"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "<system-reminder>be brief</system-reminder>"


def test_request_tool_result_image_block_becomes_placeholder():
    """C10: image blocks in a tool_result don't vanish into an empty string."""
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "x"}},
                        ],
                    }
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["role"] == "tool"
    assert messages[0]["content"] == "[image]"


def test_request_tool_result_is_error_prefixed():
    """C11: is_error tool results carry an explicit error marker."""
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": "bash: command not found",
                    }
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["content"] == "[tool error]\nbash: command not found"


def test_request_multiple_text_blocks_joined_with_blank_line():
    """C12: separate text blocks keep their boundary instead of fusing."""
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "fix the test"},
                    {"type": "text", "text": "<system-reminder>x</system-reminder>"},
                ],
            }
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["content"] == "fix the test\n\n<system-reminder>x</system-reminder>"


def test_response_reasoning_content_becomes_thinking_block():
    """C14: non-streaming reasoning_content is surfaced as a thinking block."""
    from serving.adapters.anthropic_translator import openai_response_to_anthropic

    resp = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "The answer is 4.",
                    "reasoning_content": "2+2 is 4",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["content"][0]["type"] == "thinking"
    assert out["content"][0]["thinking"] == "2+2 is 4"
    assert out["content"][1] == {"type": "text", "text": "The answer is 4."}


def test_stream_reasoning_deltas_become_thinking_blocks():
    """C1: reasoning deltas are emitted as thinking blocks (not dropped)."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"reasoning_content": "let me think "}),
        _openai_chunk({"reasoning_content": "about it"}),
        _openai_chunk({"content": "Answer"}),
        _openai_chunk({}, finish_reason="stop", usage={"prompt_tokens": 4, "completion_tokens": 1}),
    ):
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    # A thinking block opens, receives thinking_delta, and closes before text.
    starts = [e for e in events if e[0] == "content_block_start"]
    assert starts[0][1]["content_block"]["type"] == "thinking"
    thinking_deltas = [
        e[1]["delta"]["thinking"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "thinking_delta"
    ]
    assert "".join(thinking_deltas) == "let me think about it"
    # Thinking block is index 0, the text block that follows is index 1.
    text_start = next(s for s in starts if s[1]["content_block"]["type"] == "text")
    assert text_start[1]["index"] == 1


def test_stream_message_delta_reports_input_tokens():
    """C2: the terminal message_delta carries input_tokens, not just output_tokens."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"content": "hi"}),
        _openai_chunk(
            {}, finish_reason="stop", usage={"prompt_tokens": 37, "completion_tokens": 2}
        ),
    ):
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["usage"]["input_tokens"] == 37
    assert msg_delta[1]["usage"]["output_tokens"] == 2


def test_request_all_empty_text_blocks_keep_empty_string_content():
    """Regression: an all-empty text buffer must yield content='' not None."""
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": [{"type": "text", "text": ""}]}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["content"] == ""
    assert "tool_calls" not in messages[0]


def test_stream_interleaved_thinking_after_text_closes_text_block():
    """Late/interleaved reasoning must close the open text block first (one block at a time)."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"content": "partial answer"}),
        _openai_chunk({"reasoning_content": "wait, reconsider"}),
        _openai_chunk({"content": "final answer"}),
        _openai_chunk({}, finish_reason="stop"),
    ):
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    # Walk block starts/stops: never two blocks open at once.
    open_blocks = 0
    max_open = 0
    for name, _payload in events:
        if name == "content_block_start":
            open_blocks += 1
            max_open = max(max_open, open_blocks)
        elif name == "content_block_stop":
            open_blocks -= 1
    assert max_open == 1
    assert open_blocks == 0


def test_stream_message_start_seeds_input_estimate_then_corrects_in_delta():
    """message_start carries the input estimate; message_delta carries the exact count."""
    t = OpenAIToAnthropicStreamTranslator(model="m", input_tokens_estimate=25)
    out = b""
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"content": "hi"}),
        _openai_chunk(
            {}, finish_reason="stop", usage={"prompt_tokens": 31, "completion_tokens": 2}
        ),
    ):
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    msg_start = next(e for e in events if e[0] == "message_start")
    assert msg_start[1]["message"]["usage"]["input_tokens"] == 25
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["usage"]["input_tokens"] == 31


# ---------------------------------------------------------------------------
# Backlog fixes: P4 (dict tool args), P5 (tool_use stop_reason)
# ---------------------------------------------------------------------------


def test_response_tool_calls_with_stop_finish_forces_tool_use():
    """P5: provider returns tool_calls with finish_reason 'stop' -> stop_reason tool_use."""
    from serving.adapters.anthropic_translator import openai_response_to_anthropic

    resp = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["stop_reason"] == "tool_use"
    assert out["content"][0]["type"] == "tool_use"


def test_response_tool_call_truncated_at_length_stays_max_tokens():
    """P5: a tool call truncated at max_tokens (finish 'length') keeps stop_reason max_tokens."""
    from serving.adapters.anthropic_translator import openai_response_to_anthropic

    resp = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"a":'},
                        }
                    ],
                },
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["stop_reason"] == "max_tokens"


def test_response_tool_call_dict_arguments_used_directly():
    """P4: non-string (already-decoded) tool arguments are used, not dropped to {}."""
    from serving.adapters.anthropic_translator import openai_response_to_anthropic

    resp = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": {"city": "SF"}},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["content"][0]["input"] == {"city": "SF"}


def test_stream_tool_call_dict_arguments_serialized_to_string():
    """P4 (streaming): a dict arguments delta becomes a JSON string partial_json, not an object."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": {"city": "SF"}},
                    }
                ]
            }
        ),
        _openai_chunk({}, finish_reason="tool_calls"),
    ):
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    pj = [
        e[1]["delta"]["partial_json"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "input_json_delta"
    ]
    assert pj == ['{"city": "SF"}']
    assert all(isinstance(x, str) for x in pj)


def _reminder(text: str) -> str:
    return f"<system-reminder>\n{text}\n</system-reminder>"


def test_an_inline_system_message_joins_the_user_turn_it_follows():
    """Clients send `role: "system"` inside `messages`, and upstreams reject it.

    Claude Code 2.1.220 sends its agent-type listing that way, in addition to
    the top-level `system` field. Passing it through in place puts a system
    message after a user message; strict upstreams answer "System message must
    be at the beginning" and the whole request fails. Captured from a real
    request that broke every agent job at its first model call.
    """
    body = {
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": [{"type": "text", "text": "Available agent types: …"}]},
            {"role": "assistant", "content": "hi"},
        ],
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages == [
        {"role": "system", "content": "You are Claude Code."},
        {"role": "user", "content": "hello\n\n" + _reminder("Available agent types: …")},
        {"role": "assistant", "content": "hi"},
    ]


def test_an_inline_system_message_needs_no_top_level_one():
    """No system message is invented to carry it."""
    body = {
        "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "Be terse."}]
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages == [{"role": "user", "content": "hi\n\n" + _reminder("Be terse.")}]


def test_a_leading_inline_system_message_joins_the_system_prompt():
    """Ahead of the first turn there is no conversation for it to be moved past."""
    body = {
        "system": "You are Claude Code.",
        "messages": [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}],
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages == [
        {"role": "system", "content": "You are Claude Code.\n\nBe terse."},
        {"role": "user", "content": "hi"},
    ]


def test_per_turn_system_notices_keep_the_prompt_append_only():
    """Each turn's prompt must extend the previous one, or the prefix cache misses.

    A Claude Code session can end every turn's `messages` with a system notice
    (a remaining token budget, an environment update). Hoisting each into the
    system prompt inserted it just ahead of the first message, so consecutive
    turns diverged there: a 212k-token turn read 20k tokens from the upstream's
    prefix cache and re-prefilled the other 192k, on every turn.
    """

    def notice(tokens_left: int) -> dict:
        text = f"<total_tokens>{tokens_left} tokens left</total_tokens>"
        return {"role": "system", "content": [{"type": "text", "text": text}]}

    def tool_round(call_id: str) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}],
            },
        ]

    system = [{"type": "text", "text": "You are Claude Code."}]
    earlier = [
        {"role": "user", "content": "fix the bug"},
        notice(900),
        *tool_round("t1"),
        notice(800),
    ]
    later = [*earlier, *tool_round("t2"), notice(700)]

    before, _ = anthropic_request_to_openai({"system": system, "messages": earlier})
    after, _ = anthropic_request_to_openai({"system": system, "messages": later})

    assert after[: len(before)] == before
    # A native Anthropic upstream is forwarded the normalized body itself.
    native_before = normalize_inline_system({"system": system, "messages": earlier})
    native_after = normalize_inline_system({"system": system, "messages": later})
    assert native_before["system"] == native_after["system"] == system
    assert native_after["messages"][: len(native_before["messages"])] == native_before["messages"]


def test_a_notice_between_a_tool_call_and_its_result_lands_behind_the_result():
    """`tool_result` blocks must open the message that answers a `tool_use`.

    Anthropic rejects a user message whose text precedes its tool results, and
    the translation must emit the `tool` message straight after the assistant's
    `tool_calls`; the reminder goes after the results, ahead of the text.
    """
    body = {
        "messages": [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            },
            {"role": "system", "content": "Be terse."},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    {"type": "text", "text": "and now?"},
                ],
            },
        ]
    }

    native = normalize_inline_system(body)
    messages, _ = anthropic_request_to_openai(body)

    assert native["messages"][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "text", "text": _reminder("Be terse.")},
        {"type": "text", "text": "and now?"},
    ]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[-1]["content"] == _reminder("Be terse.") + "\n\nand now?"


def test_a_notice_after_an_assistant_turn_opens_the_next_user_turn():
    body = {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "three"},
        ]
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages[-1] == {"role": "user", "content": _reminder("Be terse.") + "\n\nthree"}


def test_a_notice_with_no_user_turn_beside_it_becomes_one():
    body = {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "system", "content": "Be terse."},
        ]
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages[-1] == {"role": "user", "content": _reminder("Be terse.")}


def test_a_non_dict_entry_is_no_turn_for_a_system_message_to_follow():
    """The translator skips a non-dict entry, so placement must not count it either."""
    body = {
        "system": "You are Claude Code.",
        "messages": [
            "junk",
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "system", "content": "Mind the budget."},
            7,
            {"role": "user", "content": "three"},
        ],
    }

    messages, _ = anthropic_request_to_openai(body)

    assert messages == [
        {"role": "system", "content": "You are Claude Code.\n\nBe terse."},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": _reminder("Mind the budget.") + "\n\nthree"},
    ]


def test_a_folded_notice_keeps_the_client_cache_breakpoint():
    """Claude Code marks its last block for caching, and that block is the notice.

    A native Anthropic upstream caches up to the marked block, so dropping the
    marker, or leaving it in the system prompt, caches none of the conversation.
    The client's own body is not modified on the way.
    """
    marked = {"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}}
    body = {
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": [marked]}],
    }
    sent = copy.deepcopy(body)

    native = normalize_inline_system(body)

    assert native["messages"][-1]["content"][-1] == {
        "type": "text",
        "text": _reminder("Be terse."),
        "cache_control": {"type": "ephemeral"},
    }
    assert body == sent


def test_conversation_order_is_otherwise_untouched():
    """Hoisting must not reorder the turns around it."""
    body = {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    }

    messages, _ = anthropic_request_to_openai(body)

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "one"),
        ("assistant", "two"),
        ("user", "three"),
    ]


# ---------------------------------------------------------------------------
# Malformed content blocks degrade, they do not 500


def test_request_string_content_block_is_treated_as_text():
    """`content: ["hi"]` is not the documented shape, but clients send it.

    Every block went through `block.get("type")`, so a plain string raised
    AttributeError and 500'd a request whose intent is unambiguous.
    """
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": ["Hello", "world"]}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages == [{"role": "user", "content": "Hello\n\nworld"}]


def test_request_untranslatable_blocks_are_dropped_not_fatal():
    """A junk block degrades one block; the surrounding turn still translates."""
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": [None, 42, {"type": "text", "text": "kept"}, ["nested"]]}
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages == [{"role": "user", "content": "kept"}]


def test_request_non_dict_message_is_skipped():
    """`msg.get("role")` on a non-dict message was the same crash one frame up."""
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": ["junk", {"role": "user", "content": "real"}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages == [{"role": "user", "content": "real"}]


def test_stream_translator_survives_an_unreadable_usage_chunk():
    """An upstream's unreadable token count is billed as 0, not a broken stream."""
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b"".join(t.feed(_openai_chunk({"role": "assistant"})))
    out += b"".join(t.feed(_openai_chunk({"content": "hi"})))
    out += b"".join(
        t.feed(
            _openai_chunk(
                {},
                finish_reason="stop",
                usage={"prompt_tokens": "lots", "completion_tokens": "7"},
            )
        )
    )
    out += b"".join(t.feed(b"data: [DONE]\n\n"))
    out += b"".join(t.finalize())

    assert t.usage["input_tokens"] == 0
    assert t.usage["output_tokens"] == 7
    assert b"message_stop" in out


def test_stream_translator_keeps_the_output_count_when_a_chunk_reports_none():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    b"".join(t.feed(_openai_chunk({"content": "hi"}, usage={"completion_tokens": 4})))
    b"".join(t.feed(_openai_chunk({}, usage={"prompt_tokens": 2, "completion_tokens": None})))

    assert t.usage["output_tokens"] == 4
