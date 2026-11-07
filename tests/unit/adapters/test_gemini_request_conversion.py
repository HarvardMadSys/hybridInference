"""OpenAI-to-Gemini request conversion: messages, schemas, sampling and tool choice.

Gemini 400s on what OpenAI accepts (an array schema without ``items``, an empty
text part, a functionResponse that names no prior call), and some OpenAI
settings have no field of the same name. Most of these conversions had no test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter


@pytest.fixture
def adapter() -> GeminiAdapter:
    adapter = GeminiAdapter(
        ModelConfig(
            id="gemini-test",
            name="Gemini Test",
            provider="gemini",
            base_url="https://mock",
            api_key="test-key",
        )
    )
    adapter.http = MagicMock()
    return adapter


# --- messages --------------------------------------------------------------------


def test_text_is_flattened_and_empty_parts_are_dropped(adapter):
    body = adapter._convert_messages_to_gemini(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    "and this",
                    {"type": "text", "text": ""},
                ],
            },
            {"role": "user", "content": ""},  # an empty turn would 400
            {"role": "assistant", "content": None},
            {"role": "user", "content": {"text": "a mapping"}},
            {"role": "user", "content": {"no": "text field"}},
        ]
    )

    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "look\nand this"}]},
        {"role": "user", "parts": [{"text": "a mapping"}]},
        {"role": "user", "parts": [{"text": '{"no": "text field"}'}]},
    ]
    assert "systemInstruction" not in body


def test_every_system_message_is_kept(adapter):
    """A later system message must not silently replace an earlier one."""
    body = adapter._convert_messages_to_gemini(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "text", "text": "Answer in French."}]},
        ]
    )

    assert body["systemInstruction"] == {
        "role": "system",
        "parts": [{"text": "You are terse.\n\nAnswer in French."}],
    }


@pytest.mark.parametrize(
    ("content", "response"),
    [
        ('{"temp": 18}', {"temp": 18}),
        ({"temp": 18}, {"temp": 18}),
        ("18 degrees", {"output": "18 degrees"}),
        (None, {}),
        # FunctionResponse.response is a Struct: an object, never an array or a scalar.
        ('[{"title": "a"}]', {"output": [{"title": "a"}]}),
        ("42", {"output": 42}),
        ("true", {"output": True}),
        (
            [{"type": "text", "text": "18 "}, {"type": "text", "text": "degrees"}],
            {"output": "18 degrees"},
        ),
    ],
    ids=[
        "json-text",
        "object",
        "plain-text",
        "none",
        "json-array",
        "json-number",
        "json-bool",
        "content-parts",
    ],
)
def test_a_tool_result_becomes_a_function_response(adapter, content, response):
    body = adapter._convert_messages_to_gemini(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{bad"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": content},
        ]
    )

    assert body["contents"][0]["parts"] == [{"functionCall": {"name": "weather", "args": {}}}]
    assert body["contents"][1] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "weather", "response": response}}],
    }


# --- tools -------------------------------------------------------------------------


def test_tools_become_one_set_of_function_declarations(adapter):
    tools = adapter._convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "find things",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "tags": {"type": "array"},  # no items: Gemini would 400
                            "limit": {"type": ["integer", "null"], "maximum": 50, "default": 10},
                            "mode": {"type": "string", "enum": ["fast", "deep"]},
                            "filter": {
                                "type": "object",
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "integer"}}
                                },
                                "required": ["ids"],
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {"type": "function", "function": {"name": "ping"}},
            {"type": "function", "function": {"description": "nameless"}},
            {"type": "retrieval"},
        ]
    )

    [group] = tools
    search, ping = group["functionDeclarations"]
    assert ping == {"name": "ping", "description": ""}
    props = search["parameters"]["properties"]
    assert search["parameters"]["required"] == ["query"]
    assert props["query"] == {"type": "string", "minLength": 1}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["limit"] == {"type": "integer", "maximum": 50, "default": 10}
    assert props["mode"] == {"type": "string", "enum": ["fast", "deep"]}
    assert props["filter"] == {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["ids"],
    }
    assert adapter._convert_tools([{"type": "retrieval"}]) == []


# --- sampling and tool choice --------------------------------------------------------


def test_sampling_parameters_map_to_generation_config(adapter):
    assert adapter._apply_generation_config(
        {
            "max_tokens": 64,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": "40",
            "stop": "END",
            "response_format": {"type": "json_object"},
        }
    ) == {
        "maxOutputTokens": 64,
        "temperature": 0.2,
        "topP": 0.9,
        "topK": 40,
        "stopSequences": ["END"],
        "responseMimeType": "application/json",
    }
    assert adapter._apply_generation_config({"top_k": 0, "stop": ["a", "b"]}) == {
        "stopSequences": ["a", "b"]
    }
    assert adapter._apply_generation_config({"top_k": "many"}) == {}


@pytest.mark.parametrize(
    ("choice", "config"),
    [
        (None, {}),
        ("auto", {}),
        ("none", {"toolConfig": {"functionCallingConfig": {"mode": "NONE"}}}),
        # OpenAI's "required" means a tool must be called: Gemini's ANY, not AUTO.
        ("required", {"toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}),
        (
            {"type": "function", "function": {"name": "search"}},
            {
                "toolConfig": {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["search"]}
                }
            },
        ),
        ({"type": "function", "function": {}}, {}),
    ],
    ids=["unset", "auto", "none", "required", "named", "unnamed"],
)
def test_tool_choice_maps_to_tool_config(adapter, choice, config):
    params: dict[str, Any] = {"tool_choice": choice} if choice is not None else {}

    assert adapter._apply_tool_config(params) == config
