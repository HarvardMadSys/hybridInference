"""Tests for Codex format translator (pure functions, no I/O)."""

from __future__ import annotations

import pytest

from serving.adapters.codex_translator import (
    _extract_usage,
    _map_model_slug,
    _map_stop_reason,
    _translate_content,
    translate_request,
    translate_response,
    translate_stream_event,
)

# ---------------------------------------------------------------------------
# translate_request
# ---------------------------------------------------------------------------


class TestTranslateRequest:
    def test_basic_system_and_user(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        body = translate_request(messages, "gpt-5.1-codex")

        assert body["model"] == "gpt-5.1-codex"
        assert body["store"] is False
        assert body["stream"] is True

        # system → top-level instructions (not in input)
        assert body["instructions"] == "You are helpful."

        # input only contains user message
        assert len(body["input"]) == 1
        assert body["input"][0]["role"] == "user"
        assert body["input"][0]["content"] == [{"type": "input_text", "text": "Hello"}]

    def test_no_system_message_gives_empty_instructions(self):
        messages = [{"role": "user", "content": "Hello"}]
        body = translate_request(messages, "gpt-5.1-codex")
        assert body["instructions"] == ""
        assert len(body["input"]) == 1

    def test_tool_calls_from_assistant(self):
        messages = [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                    }
                ],
            },
        ]
        body = translate_request(messages, "gpt-5-codex")

        # user message + assistant message + function_call
        assert len(body["input"]) == 3
        assert body["input"][1]["type"] == "message"
        assert body["input"][1]["role"] == "assistant"
        assert body["input"][2]["type"] == "function_call"
        assert body["input"][2]["id"] == "call_1"
        assert body["input"][2]["name"] == "get_weather"
        assert body["input"][2]["arguments"] == '{"city":"NYC"}'

    def test_tool_results(self):
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 72}'},
        ]
        body = translate_request(messages, "gpt-5-codex")

        assert len(body["input"]) == 1
        item = body["input"][0]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "call_1"
        assert item["output"] == '{"temp": 72}'

    def test_reasoning_effort_from_param(self):
        body = translate_request(
            [{"role": "user", "content": "hi"}],
            "gpt-5.1-codex",
            reasoning_effort="low",
        )
        assert body["model"] == "gpt-5.1-codex"
        assert body["reasoning"] == {"effort": "low"}

    def test_reasoning_effort_high_from_param(self):
        body = translate_request(
            [{"role": "user", "content": "hi"}],
            "gpt-5.1-codex",
            reasoning_effort="high",
        )
        assert body["model"] == "gpt-5.1-codex"
        assert body["reasoning"] == {"effort": "high"}

    def test_no_reasoning_when_not_specified(self):
        body = translate_request([{"role": "user", "content": "hi"}], "gpt-5.1-codex")
        assert "reasoning" not in body

    def test_multimodal_content(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                ],
            }
        ]
        body = translate_request(messages, "gpt-5.1-codex")
        content = body["input"][0]["content"]
        assert content[0] == {"type": "input_text", "text": "Describe this image"}
        assert content[1] == {"type": "input_image", "image_url": "https://example.com/img.png"}

    def test_optional_params(self):
        body = translate_request(
            [{"role": "user", "content": "hi"}],
            "gpt-5-codex",
            temperature=0.5,
            max_tokens=100,
            stream=True,
            tools=[{"type": "function", "function": {"name": "f"}}],
            tool_choice="auto",
        )
        assert body["temperature"] == 0.5
        assert "max_output_tokens" not in body
        assert body["stream"] is True
        assert body["tools"] == [{"type": "function", "name": "f"}]
        assert body["tool_choice"] == "auto"


# ---------------------------------------------------------------------------
# translate_response
# ---------------------------------------------------------------------------


class TestTranslateResponse:
    def test_text_output(self):
        codex_resp = {
            "id": "resp_abc123",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            ],
            "stop_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = translate_response(codex_resp, "gpt-5.1-codex")

        assert result["id"] == "chatcmpl-resp_abc123"
        assert result["model"] == "gpt-5.1-codex"
        assert result["choices"][0]["message"]["content"] == "Hello world"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 5
        assert result["usage"]["total_tokens"] == 15

    def test_tool_calls_output(self):
        codex_resp = {
            "id": "resp_xyz",
            "output": [
                {
                    "type": "function_call",
                    "id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"NYC"}',
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        result = translate_response(codex_resp, "gpt-5-codex")

        msg = result["choices"][0]["message"]
        assert msg["content"] is None
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_stop_reason_max_output_tokens(self):
        codex_resp = {
            "id": "r1",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "partial"}]},
            ],
            "stop_reason": "max_output_tokens",
            "usage": {"input_tokens": 5, "output_tokens": 100},
        }
        result = translate_response(codex_resp, "gpt-5-codex")
        assert result["choices"][0]["finish_reason"] == "length"


# ---------------------------------------------------------------------------
# translate_stream_event
# ---------------------------------------------------------------------------


class TestTranslateStreamEvent:
    def test_text_delta(self):
        event = {"type": "response.output_text.delta", "delta": "Hello"}
        content, tool, done = translate_stream_event(event)
        assert content == "Hello"
        assert tool is None
        assert done is False

    def test_function_call_delta(self):
        event = {
            "type": "response.function_call_arguments.delta",
            "delta": '{"city":',
            "output_index": 0,
            "item_id": "call_1",
            "name": "get_weather",
        }
        content, tool, done = translate_stream_event(event)
        assert content is None
        assert tool is not None
        assert tool["function"]["arguments"] == '{"city":'
        assert tool["id"] == "call_1"
        assert done is False

    def test_completed_event(self):
        event = {"type": "response.completed", "response": {"usage": {}}}
        content, tool, done = translate_stream_event(event)
        assert content is None
        assert tool is None
        assert done is True

    def test_unknown_event(self):
        event = {"type": "response.created"}
        content, tool, done = translate_stream_event(event)
        assert content is None
        assert tool is None
        assert done is False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestMapModelSlug:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("gpt-5.1-codex", ("gpt-5.1-codex", None)),
            ("gpt-5-codex", ("gpt-5-codex", None)),
            ("o4-mini-codex", ("o4-mini-codex", None)),
        ],
    )
    def test_known_slugs(self, model_id, expected):
        assert _map_model_slug(model_id) == expected

    def test_unknown_passthrough(self):
        assert _map_model_slug("future-model") == ("future-model", None)


class TestMapStopReason:
    def test_stop(self):
        assert _map_stop_reason("stop") == "stop"

    def test_max_output_tokens(self):
        assert _map_stop_reason("max_output_tokens") == "length"

    def test_tool_use(self):
        assert _map_stop_reason("tool_use") == "tool_calls"

    def test_unknown(self):
        assert _map_stop_reason("unknown_reason") == "stop"


class TestTranslateContent:
    def test_string(self):
        assert _translate_content("hello") == [{"type": "input_text", "text": "hello"}]

    def test_none(self):
        assert _translate_content(None) == []

    def test_multimodal(self):
        blocks = [
            {"type": "text", "text": "Look at this"},
            {"type": "image_url", "image_url": {"url": "https://img.png"}},
        ]
        result = _translate_content(blocks)
        assert result == [
            {"type": "input_text", "text": "Look at this"},
            {"type": "input_image", "image_url": "https://img.png"},
        ]


class TestExtractUsage:
    def test_basic(self):
        assert _extract_usage({"input_tokens": 100, "output_tokens": 50}) == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }

    def test_empty(self):
        assert _extract_usage({}) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
