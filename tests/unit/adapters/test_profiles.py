"""Regression tests for serving.adapters.profiles."""

from __future__ import annotations

import pytest

from serving.adapters.profiles import (
    ProviderProfile,
    function_call_delta_to_tool_calls,
    normalize_tools_for_profile,
    normalize_usage_default,
)

# ---------------------------------------------------------------------------
# Streaming: function_call_delta_to_tool_calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "delta"),
    [
        (ProviderProfile.DEFAULT, {"name": "get_weather", "arguments": ""}),
        (ProviderProfile.ZAI, {"arguments": '{"cit'}),
        (ProviderProfile.DEEPSEEK, {"name": "fn", "arguments": "{}"}),
        (ProviderProfile.DEFAULT, {}),
    ],
)
def test_function_call_delta_returns_none(profile, delta) -> None:
    """Stub returns None for all profiles after Llama removal; guards regression."""
    assert function_call_delta_to_tool_calls(profile, delta) is None


# ---------------------------------------------------------------------------
# Usage normalization: normalize_usage_default
# ---------------------------------------------------------------------------


def test_normalize_usage_default_nested_only_zai_minimax_shape() -> None:
    """ZAI and MiniMax return reasoning/cached tokens only in nested details."""
    usage_data = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "completion_tokens_details": {"reasoning_tokens": 30},
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    info = normalize_usage_default(usage_data)
    assert info.prompt_tokens == 100
    assert info.completion_tokens == 50
    assert info.total_tokens == 150
    assert info.reasoning_tokens == 30
    assert info.cache_read_tokens == 40
    assert info.cache_write_tokens == 0


def test_normalize_usage_default_mixed_chutes_shape() -> None:
    """Chutes-style: top-level reasoning_tokens with nested prompt_tokens_details.cached_tokens."""
    usage_data = {
        "prompt_tokens": 200,
        "completion_tokens": 80,
        "total_tokens": 280,
        "reasoning_tokens": 25,
        "prompt_tokens_details": {"cached_tokens": 60},
    }
    info = normalize_usage_default(usage_data)
    assert info.prompt_tokens == 200
    assert info.completion_tokens == 80
    assert info.total_tokens == 280
    assert info.reasoning_tokens == 25
    assert info.cache_read_tokens == 60
    assert info.cache_write_tokens == 0


def test_normalize_usage_default_absent_ollama_shape() -> None:
    """Ollama-style: only basic fields, no reasoning/cache info anywhere."""
    usage_data = {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    info = normalize_usage_default(usage_data)
    assert info.prompt_tokens == 10
    assert info.completion_tokens == 20
    assert info.total_tokens == 30
    assert info.reasoning_tokens == 0
    assert info.cache_read_tokens == 0
    assert info.cache_write_tokens == 0


def test_normalize_usage_default_anthropic_cache_write_tokens() -> None:
    """Anthropic-style: cache_creation_input_tokens populates cache_write_tokens."""
    usage_data = {
        "prompt_tokens": 500,
        "completion_tokens": 100,
        "total_tokens": 600,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 80,
    }
    info = normalize_usage_default(usage_data)
    assert info.prompt_tokens == 500
    assert info.completion_tokens == 100
    assert info.total_tokens == 600
    assert info.reasoning_tokens == 0
    assert info.cache_read_tokens == 120
    assert info.cache_write_tokens == 80


def test_normalize_usage_default_empty_dict() -> None:
    """Empty usage dict should not raise and yield all zeros."""
    info = normalize_usage_default({})
    assert info.prompt_tokens == 0
    assert info.completion_tokens == 0
    assert info.total_tokens == 0
    assert info.reasoning_tokens == 0
    assert info.cache_read_tokens == 0
    assert info.cache_write_tokens == 0


def test_normalize_usage_default_explicit_zero_cache_recorded() -> None:
    """Explicit zero cache fields should still produce 0 (not None)."""
    usage_data = {
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    info = normalize_usage_default(usage_data)
    assert info.reasoning_tokens == 0
    assert info.cache_read_tokens == 0


# ---------------------------------------------------------------------------
# Tool normalization: DeepSeek JSON Schema cleaning
# ---------------------------------------------------------------------------


def test_deepseek_normalizes_tools_removes_dollar_schema() -> None:
    """DeepSeek rejects $schema in tool parameters."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.DEEPSEEK, tools)
    assert "$schema" not in result[0]["function"]["parameters"]
    assert result[0]["function"]["parameters"]["type"] == "object"


def test_deepseek_normalizes_tools_removes_non_def_ref() -> None:
    """DeepSeek rejects $ref that doesn't start with #/$def/."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_data",
                "parameters": {
                    "$ref": "#/definitions/SomeType",
                    "type": "object",
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.DEEPSEEK, tools)
    assert "$ref" not in result[0]["function"]["parameters"]


def test_deepseek_preserves_valid_def_ref() -> None:
    """DeepSeek accepts $ref that starts with #/$def/."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_data",
                "parameters": {
                    "$ref": "#/$def/SomeType",
                    "type": "object",
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.DEEPSEEK, tools)
    assert result[0]["function"]["parameters"]["$ref"] == "#/$def/SomeType"


def test_deepseek_normalizes_nested_schema() -> None:
    """DeepSeek cleaning recurses into nested objects and arrays."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "complex_tool",
                "parameters": {
                    "$schema": "https://example.com/schema",
                    "type": "object",
                    "properties": {
                        "nested": {
                            "$schema": "https://example.com/schema",
                            "type": "object",
                            "properties": {"item": {"type": "string"}},
                        },
                        "items": {
                            "type": "array",
                            "items": {
                                "$ref": "#/definitions/Item",
                                "type": "object",
                            },
                        },
                    },
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.DEEPSEEK, tools)
    params = result[0]["function"]["parameters"]
    assert "$schema" not in params
    assert "$schema" not in params["properties"]["nested"]
    assert "$ref" not in params["properties"]["items"]["items"]


def test_default_profile_does_not_modify_tools() -> None:
    """Non-DeepSeek profiles should not modify tool schemas."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$ref": "#/definitions/Location",
                    "type": "object",
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.DEFAULT, tools)
    assert result == tools
    assert "$schema" in result[0]["function"]["parameters"]
    assert "$ref" in result[0]["function"]["parameters"]
