"""Unit tests for serving.responses_translator.

Covers OpenAI Responses API <-> Chat Completions translation: request input/
params, non-streaming response shaping, and the streaming event state machine.
"""

from __future__ import annotations

import json
import re

from serving.responses_translator import (
    ResponsesStreamTranslator,
    assistant_message_from_chat,
    chat_response_to_responses,
    responses_input_to_messages,
    responses_request_to_chat_params,
)

# --- request: input -> messages -------------------------------------------


def test_input_string_with_instructions():
    msgs = responses_input_to_messages("hello", instructions="be nice")
    assert msgs == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hello"},
    ]


def test_input_string_no_instructions():
    assert responses_input_to_messages("hi") == [{"role": "user", "content": "hi"}]


def test_input_multimodal_parts():
    msgs = responses_input_to_messages(
        [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this"},
                    {"type": "input_image", "image_url": "http://x/y.png"},
                ],
            }
        ]
    )
    assert msgs == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        }
    ]


def test_input_image_url_object_with_detail():
    msgs = responses_input_to_messages(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": {"url": "u"}, "detail": "high"}],
            }
        ]
    )
    assert msgs[0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "u", "detail": "high"},
    }


def test_input_function_call_roundtrip():
    msgs = responses_input_to_messages(
        [
            {"type": "message", "role": "user", "content": "weather?"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city":"sf"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
        ]
    )
    assert msgs == [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"sf"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]


def test_input_developer_role_mapped_to_system():
    msgs = responses_input_to_messages(
        [{"type": "message", "role": "developer", "content": "be terse"}]
    )
    assert msgs == [{"role": "system", "content": "be terse"}]


def test_input_function_call_output_non_string_serialized():
    msgs = responses_input_to_messages(
        [{"type": "function_call_output", "call_id": "c", "output": {"k": 1}}]
    )
    assert msgs == [{"role": "tool", "tool_call_id": "c", "content": '{"k": 1}'}]


# --- request: params -------------------------------------------------------


def test_params_full_mapping():
    params, dropped = responses_request_to_chat_params(
        {
            "max_output_tokens": 100,
            "temperature": 0.5,
            "top_p": 0.9,
            "reasoning": {"effort": "high"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "Foo",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
            "tools": [
                {
                    "type": "function",
                    "name": "f",
                    "description": "d",
                    "parameters": {"type": "object"},
                },
                {"type": "web_search"},
            ],
            "tool_choice": {"type": "function", "name": "f"},
        }
    )
    assert params["max_tokens"] == 100
    assert params["temperature"] == 0.5
    assert params["top_p"] == 0.9
    assert params["reasoning_effort"] == "high"
    assert params["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Foo", "schema": {"type": "object"}, "strict": True},
    }
    assert params["tools"] == [
        {
            "type": "function",
            "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    assert params["tool_choice"] == {"type": "function", "function": {"name": "f"}}
    assert dropped == ["web_search"]


def test_params_json_object_format():
    params, _ = responses_request_to_chat_params({"text": {"format": {"type": "json_object"}}})
    assert params["response_format"] == {"type": "json_object"}


def test_params_text_format_yields_no_response_format():
    params, _ = responses_request_to_chat_params({"text": {"format": {"type": "text"}}})
    assert "response_format" not in params


def test_params_tool_choice_string_passthrough():
    params, _ = responses_request_to_chat_params({"tool_choice": "required"})
    assert params["tool_choice"] == "required"


# --- non-streaming response translation ------------------------------------


def test_chat_response_text():
    chat = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "glm-4.7",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }
    r = chat_response_to_responses(
        chat,
        response_id="resp_1",
        created_at=123,
        model="glm-4.7",
        request_body={"instructions": "x"},
    )
    assert r["object"] == "response"
    assert r["status"] == "completed"
    assert r["id"] == "resp_1"
    assert r["instructions"] == "x"
    assert r["output"][0]["type"] == "message"
    assert r["output"][0]["content"][0] == {
        "type": "output_text",
        "text": "Hello world",
        "annotations": [],
    }
    assert r["usage"] == {
        "input_tokens": 9,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 12,
    }


def test_chat_response_nested_usage_details_preserved():
    chat = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
            "prompt_tokens_details": {"cached_tokens": 12},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }
    r = chat_response_to_responses(chat, response_id="r", created_at=1, model="m", request_body={})
    assert r["usage"]["input_tokens_details"]["cached_tokens"] == 12
    assert r["usage"]["output_tokens_details"]["reasoning_tokens"] == 5


def test_chat_response_length_is_incomplete():
    chat = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "x"},
                "finish_reason": "length",
            }
        ]
    }
    r = chat_response_to_responses(chat, response_id="r", created_at=1, model="m", request_body={})
    assert r["status"] == "incomplete"
    assert r["incomplete_details"] == {"reason": "max_output_tokens"}


def test_chat_response_tool_calls():
    chat = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "g", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    r = chat_response_to_responses(chat, response_id="r", created_at=1, model="m", request_body={})
    assert r["output"][0]["type"] == "function_call"
    assert r["output"][0]["call_id"] == "call_9"
    assert r["output"][0]["name"] == "g"
    assert assistant_message_from_chat(chat)["tool_calls"][0]["id"] == "call_9"


# --- streaming -------------------------------------------------------------


def _collect(chunks):
    t = ResponsesStreamTranslator(
        response_id="resp_s", created_at=1, model="glm-4.7", request_body={}
    )
    out = []
    for c in chunks:
        out.extend(t.feed(c))
    out.extend(t.finalize())
    return t, "".join(out)


def test_stream_text_event_sequence():
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
        "data: [DONE]\n\n",
    ]
    t, joined = _collect(chunks)
    for ev in (
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ):
        assert f"event: {ev}" in joined, ev
    assert '"delta": "Hel"' in joined
    assert t.final_response["output"][0]["content"][0]["text"] == "Hello"
    assert t.final_response["usage"]["output_tokens"] == 2
    assert t.assistant_message == {"role": "assistant", "content": "Hello"}


def test_stream_sequence_numbers_monotonic_from_zero():
    _, joined = _collect(['data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'])
    seqs = [int(x) for x in re.findall(r'"sequence_number": (\d+)', joined)]
    assert seqs == sorted(seqs)
    assert seqs[0] == 0


def test_stream_keepalive_comment_swallowed():
    t = ResponsesStreamTranslator(response_id="r", created_at=1, model="m", request_body={})
    assert list(t.feed(": keepalive\n\n")) == []


def test_stream_tool_call():
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_7",'
        '"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"c\\":1}"}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    ]
    t, joined = _collect(chunks)
    assert '"type": "function_call"' in joined
    assert '"name": "get_weather"' in joined
    assert "event: response.function_call_arguments.delta" in joined
    assert "event: response.function_call_arguments.done" in joined
    fc = t.final_response["output"][0]
    assert fc["type"] == "function_call"
    assert fc["arguments"] == '{"c":1}'
    assert t.assistant_message["tool_calls"][0]["function"]["name"] == "get_weather"


def test_stream_split_tool_name_accumulates():
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c",'
        '"function":{"name":"get_"}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"weather"}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    ]
    t, _ = _collect(chunks)
    assert t.final_response["output"][0]["name"] == "get_weather"


def _output_item_added_events(joined: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in joined.splitlines()
        if line.startswith("data: ") and '"response.output_item.added"' in line
    ]


def test_stream_tool_only_first_item_at_output_index_0():
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    ]
    t, joined = _collect(chunks)
    added = _output_item_added_events(joined)
    assert added and added[0]["output_index"] == 0
    assert added[0]["item"]["type"] == "function_call"
    assert t.final_response["output"][0]["type"] == "function_call"


def test_stream_text_then_tool_indices_are_sequential():
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c",'
        '"type":"function","function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    ]
    t, joined = _collect(chunks)
    added = _output_item_added_events(joined)
    assert added[0]["output_index"] == 0
    assert added[0]["item"]["type"] == "message"
    assert added[1]["output_index"] == 1
    assert added[1]["item"]["type"] == "function_call"
    assert [o["type"] for o in t.final_response["output"]] == ["message", "function_call"]


def test_tool_choice_function_not_a_dict_does_not_raise():
    # A malformed nested tool_choice (function is a string) must not AttributeError.
    params, _ = responses_request_to_chat_params(
        {"tool_choice": {"type": "function", "function": "f"}}
    )
    assert "tool_choice" in params


def test_stream_error_emits_failed():
    t, joined = _collect(['data: {"error":{"message":"boom","code":500}}\n\n'])
    assert "event: response.failed" in joined
    assert "event: error" in joined
    assert '"message": "boom"' in joined
    assert t.failed is True


def test_stream_empty_finalizes_completed():
    t, joined = _collect([])
    assert t.final_response is not None
    assert t.final_response["status"] == "completed"
    assert t.final_response["output"] == []
    assert "event: response.completed" in joined


def test_stream_events_are_well_formed_sse():
    _, joined = _collect(['data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'])
    # Every event block is "event: <type>\ndata: {json}\n\n".
    blocks = [b for b in joined.split("\n\n") if b.strip()]
    for b in blocks:
        lines = b.split("\n")
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        json.loads(lines[1][len("data: ") :])
