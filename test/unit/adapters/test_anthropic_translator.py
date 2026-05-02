"""Unit tests for serving.adapters.anthropic_translator.

Covers Anthropic Messages format -> OpenAI Chat Completions format translation.
"""

from __future__ import annotations

import logging

from serving.adapters.anthropic_translator import anthropic_request_to_openai


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
