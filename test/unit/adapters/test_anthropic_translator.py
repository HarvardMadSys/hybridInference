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
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Hi", "cache_control": {"type": "ephemeral"}},
            ],
        }],
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
        "model": "glm-4.7", "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "auto"


def test_request_tool_choice_any_becomes_required():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "any"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "required"


def test_request_tool_choice_named_tool():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
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
        "model": "glm-4.7", "max_tokens": 100,
        "metadata": {"user_id": "u-abc"},
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["user"] == "u-abc"


def test_request_stop_sequences_renamed():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
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
