"""Unit tests for serving.adapters.anthropic_translator.

Covers Anthropic Messages format -> OpenAI Chat Completions format translation.
"""

from __future__ import annotations

import json as _json
import logging

from serving.adapters.anthropic_translator import (
    OpenAIToAnthropicStreamTranslator,
    anthropic_request_to_openai,
    extract_anthropic_usage_from_sse,
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


def test_an_inline_system_message_is_hoisted_to_the_front():
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

    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant"], f"system must lead, got {roles}"
    # Both instructions survive — hoisting must not drop what it moves.
    assert "You are Claude Code." in messages[0]["content"]
    assert "Available agent types" in messages[0]["content"]
    assert sum(r == "system" for r in roles) == 1


def test_an_inline_system_message_works_without_a_top_level_one():
    """The hoist must create the leading system message when there is none."""
    body = {
        "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "Be terse."}]
    }

    messages, _ = anthropic_request_to_openai(body)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "Be terse."


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
