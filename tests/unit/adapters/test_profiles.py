"""Regression tests for serving.adapters.profiles."""

from __future__ import annotations

import pytest

from serving.adapters.profiles import (
    ProviderProfile,
    function_call_delta_to_tool_calls,
    get_stream_idle_timeout_seconds,
    normalize_tools_for_profile,
    normalize_usage_deepseek,
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


def test_stream_idle_timeout_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STREAM_IDLE_TIMEOUT_SECONDS", raising=False)
    assert get_stream_idle_timeout_seconds(ProviderProfile.OPENROUTER) is None


def test_stream_idle_timeout_parses_positive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "300.5")
    assert get_stream_idle_timeout_seconds(ProviderProfile.OPENROUTER) == 300.5


@pytest.mark.parametrize("raw", ["", "  ", "0", "-1", "nope"])
def test_stream_idle_timeout_ignores_disabled_or_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", raw)
    assert get_stream_idle_timeout_seconds(ProviderProfile.OPENROUTER) is None


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
    # Provider reported nothing → not a miss, and to_dict omits the field.
    assert info.cache_read_reported is False
    assert "cache_read_tokens" not in info.to_dict()


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


def test_normalize_usage_default_sglang_cached_tokens_serializes_compat_field() -> None:
    """SGLang exposes cache hits as usage.cached_tokens; keep that public field."""
    usage_data = {
        "prompt_tokens": 914,
        "completion_tokens": 1,
        "total_tokens": 915,
        "cached_tokens": 913,
    }

    info = normalize_usage_default(usage_data)

    assert info.cache_read_tokens == 913
    result = info.to_dict()
    assert result["cached_tokens"] == 913
    # OpenAI-standard nested shape so SDK clients reading
    # usage.prompt_tokens_details.cached_tokens see the hit.
    assert result["prompt_tokens_details"]["cached_tokens"] == 913


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
    """A provider-reported cache_read of 0 is preserved through to_dict.

    Lets downstream logging distinguish a reported miss (0) from "provider did
    not report" (absent).
    """
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
    assert info.cache_write_tokens == 0
    assert info.cache_read_reported is True
    assert info.to_dict()["cache_read_tokens"] == 0


def test_normalize_usage_deepseek_miss_only_reported() -> None:
    """DeepSeek reporting only prompt_cache_miss_tokens is still a confirmed miss."""
    info = normalize_usage_deepseek(
        {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
            "prompt_cache_miss_tokens": 50,
        }
    )
    assert info.cache_read_tokens == 0
    assert info.cache_read_reported is True
    assert info.to_dict()["cache_read_tokens"] == 0


# ---------------------------------------------------------------------------
# Tool schema normalization: normalize_tools_for_profile
# ---------------------------------------------------------------------------


def test_normalize_tools_default_profile_passthrough() -> None:
    """Non-Kimi/MiniMax profiles forward tool defs unchanged."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "anyOf"}}}]
    assert normalize_tools_for_profile(ProviderProfile.DEFAULT, tools) is tools


def test_normalize_tools_empty_or_none_passthrough() -> None:
    """No tools on the request -> nothing to normalize."""
    assert normalize_tools_for_profile(ProviderProfile.KIMI, None) is None
    assert normalize_tools_for_profile(ProviderProfile.KIMI, []) == []


def test_normalize_tools_kimi_strips_type_beside_anyof() -> None:
    """Moonshot rejects a discriminated-union schema (Claude Code's actor tool):

    'tools.function.parameters is not a valid moonshot flavored json schema,
    details: <At path 'properties.operation': when using anyOf, type should
    be defined in anyOf items instead of the parent schema>' (prod
    req_77edb9933df642cba5f418ea072f5803). Every anyOf branch below already
    declares its own "type", so the redundant parent "type" is dropped.
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "actor",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "object",
                            "anyOf": [
                                {"type": "object", "required": ["action"]},
                                {"type": "object", "required": ["actor_id"]},
                            ],
                        }
                    },
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.KIMI, tools)
    operation_schema = result[0]["function"]["parameters"]["properties"]["operation"]
    assert "type" not in operation_schema
    assert len(operation_schema["anyOf"]) == 2
    # Nested "type" declarations inside each anyOf branch are untouched.
    assert all(branch["type"] == "object" for branch in operation_schema["anyOf"])
    # Original input is not mutated.
    assert "type" in tools[0]["function"]["parameters"]["properties"]["operation"]


def test_normalize_tools_kimi_pushes_parent_type_into_typeless_branches() -> None:
    """Dropping the parent "type" must not loosen validation.

    When an anyOf branch relies on the parent for its only type constraint
    (object-only keywords like "required" don't reject non-objects), the
    parent "type" is pushed down into that branch rather than simply deleted,
    so the object constraint is preserved while still satisfying Moonshot's
    "type in the anyOf items, not beside them" rule.
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "parameters": {
                    "type": "object",
                    "anyOf": [
                        {"required": ["a"]},  # no own "type" -> must inherit object
                        {"type": "string"},  # own "type" -> left as-is
                    ],
                },
            },
        }
    ]
    params = normalize_tools_for_profile(ProviderProfile.KIMI, tools)[0]["function"]["parameters"]
    assert "type" not in params
    assert params["anyOf"][0] == {"type": "object", "required": ["a"]}
    assert params["anyOf"][1] == {"type": "string"}


def test_normalize_tools_kimi_normalizes_nested_union_after_pushdown() -> None:
    """A branch that gains a parent "type" beside its own nested "anyOf" is
    itself normalized, so no "type beside anyOf" survives at any depth."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "parameters": {
                    "type": "object",
                    "anyOf": [
                        # No own "type": inherits "object", which then sits
                        # beside this branch's own "anyOf" and must be stripped.
                        {"anyOf": [{"type": "object", "required": ["x"]}]},
                    ],
                },
            },
        }
    ]
    params = normalize_tools_for_profile(ProviderProfile.KIMI, tools)[0]["function"]["parameters"]
    assert "type" not in params
    inner = params["anyOf"][0]
    assert "type" not in inner  # pushed down again, not left beside the nested anyOf
    assert inner["anyOf"][0] == {"type": "object", "required": ["x"]}


def test_normalize_tools_kimi_leaves_plain_schemas_untouched() -> None:
    """A schema with "type" but no "anyOf" sibling is unaffected."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.KIMI, tools)
    assert result[0]["function"]["parameters"]["type"] == "object"
    assert result[0]["function"]["parameters"]["properties"]["query"]["type"] == "string"


def test_normalize_tools_kimi_tool_without_parameters_passthrough() -> None:
    """A tool with no "parameters" key at all has nothing to sanitize."""
    tools = [{"type": "function", "function": {"name": "no_args_tool"}}]
    result = normalize_tools_for_profile(ProviderProfile.KIMI, tools)
    assert result[0] == tools[0]


def test_normalize_tools_minimax_defaults_missing_parameters() -> None:
    """MiniMax rejects a tool def with no "parameters" key at all:

    '{"type":"error","error":{"type":"bad_request_error","message":"invalid
    params, function name or parameters is empty (2013)"...}}' (prod
    req_534feefacd484f0891c240344f12e02c, tool {"name": "web_search",
    "description": ""} with no "parameters" key).
    """
    tools = [{"type": "function", "function": {"name": "web_search", "description": ""}}]
    result = normalize_tools_for_profile(ProviderProfile.MINIMAX, tools)
    assert result[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert result[0]["function"]["name"] == "web_search"
    # Original input is not mutated.
    assert "parameters" not in tools[0]["function"]


def test_normalize_tools_minimax_defaults_empty_parameters_dict() -> None:
    """An explicit empty {} parameters schema is just as "empty" as a missing key."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    result = normalize_tools_for_profile(ProviderProfile.MINIMAX, tools)
    assert result[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_normalize_tools_minimax_leaves_populated_parameters_untouched() -> None:
    """A tool that already declares a real parameters schema passes through as-is."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        }
    ]
    result = normalize_tools_for_profile(ProviderProfile.MINIMAX, tools)
    assert result[0]["function"]["parameters"]["properties"]["x"]["type"] == "string"
