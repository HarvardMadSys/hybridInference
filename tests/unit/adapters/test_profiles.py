"""Regression tests for serving.adapters.profiles."""

from __future__ import annotations

import copy

import pytest

from serving.adapters.profiles import (
    ProviderProfile,
    filter_sampling_params,
    function_call_delta_to_tool_calls,
    get_stream_idle_timeout_seconds,
    normalize_messages_for_profile,
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


def test_filter_sampling_params_kimi_drops_top_p() -> None:
    """Kimi 400s any top_p != 0.95; drop it so upstream applies its default."""
    validated = {"temperature": 0.7, "top_p": 1.0, "max_tokens": 100}
    result = filter_sampling_params(ProviderProfile.KIMI, validated)
    assert result == {"temperature": 0.7, "max_tokens": 100}


def test_filter_sampling_params_kimi_no_top_p_passthrough() -> None:
    """Nothing to drop -> same dict returned unchanged."""
    validated = {"temperature": 0.7}
    assert filter_sampling_params(ProviderProfile.KIMI, validated) == validated


@pytest.mark.parametrize("profile", [ProviderProfile.DEFAULT, ProviderProfile.DEEPSEEK])
def test_filter_sampling_params_non_kimi_passthrough(profile) -> None:
    """Other profiles forward top_p unchanged."""
    validated = {"top_p": 1.0}
    assert filter_sampling_params(profile, validated) is validated


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


# ---------------------------------------------------------------------------
# Message normalization: normalize_messages_for_profile (MiniMax 2013 guard)
# ---------------------------------------------------------------------------
#
# MiniMax rejects any history where an assistant tool_calls message is not
# immediately followed by tool messages answering all of its ids:
# '{"type":"error","error":{"type":"bad_request_error","message":"invalid
# params, tool call result does not follow tool call (2013)"}}'. Agent clients
# routinely produce such histories and every other provider accepts them.


def _tool_call(call_id: str, name: str = "f") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_normalize_messages_valid_single_call_sequence_unchanged() -> None:
    """A well-formed call/result pair passes through untouched."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]
    assert normalize_messages_for_profile(ProviderProfile.MINIMAX, messages) == messages


def test_normalize_messages_valid_multi_call_block_unchanged() -> None:
    """One assistant turn with several calls answered by adjacent tool messages."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("a"), _tool_call("b"), _tool_call("c")],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
        {"role": "tool", "tool_call_id": "c", "content": "rc"},
    ]
    assert normalize_messages_for_profile(ProviderProfile.MINIMAX, messages) == messages


def test_normalize_messages_orphan_tool_calls_with_text_keeps_text() -> None:
    """Unanswered tool_calls beside real text: strip the calls, keep the turn."""
    messages = [
        {"role": "assistant", "content": "let me check", "tool_calls": [_tool_call("a")]},
        {"role": "user", "content": "never mind"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "assistant", "content": "let me check"},
        {"role": "user", "content": "never mind"},
    ]


@pytest.mark.parametrize("content", [None, ""])
def test_normalize_messages_orphan_tool_calls_without_content_drops_message(content) -> None:
    """A tool_calls-only assistant turn with no answers has nothing left to send."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": content, "tool_calls": [_tool_call("a")]},
        {"role": "assistant", "content": "answering directly"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "answering directly"},
    ]


def test_normalize_messages_partial_answer_filters_unanswered_call() -> None:
    """Two calls, one answered adjacently: keep the answered pair, drop the orphan."""
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a"), _tool_call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "user", "content": "next"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "user", "content": "next"},
    ]


def test_normalize_messages_orphan_tool_after_user_becomes_user() -> None:
    """A tool result whose governing message is a user turn is downgraded."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "a", "name": "f", "content": "stale result"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "stale result"},
    ]


def test_normalize_messages_orphan_tool_with_mismatched_id_becomes_user() -> None:
    """A tool result the preceding assistant never called is downgraded."""
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "zzz", "content": "stray"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "user", "content": "stray"},
    ]


def test_normalize_messages_non_minimax_profile_passthrough() -> None:
    """Other providers tolerate malformed histories; forward them verbatim."""
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("a")]},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "a", "content": "late result"},
    ]
    for profile in (ProviderProfile.DEFAULT, ProviderProfile.DEEPSEEK, ProviderProfile.KIMI):
        assert normalize_messages_for_profile(profile, messages) is messages


def test_normalize_messages_real_world_deferred_results_shape() -> None:
    """Staging shape: calls answered many turns later, past an assistant text turn.

    The tool_calls-only assistant turn is dropped (its answers are not
    adjacent), and the late tool results are downgraded to user messages
    because their governing assistant has no matching tool_calls.
    """
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("a"), _tool_call("b"), _tool_call("c")],
        },
        {"role": "user", "content": "interruption"},
        {"role": "assistant", "content": "some text"},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
        {"role": "tool", "tool_call_id": "c", "content": "rc"},
        {"role": "user", "content": "continue"},
    ]
    result = normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert result == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "interruption"},
        {"role": "assistant", "content": "some text"},
        {"role": "user", "content": "ra"},
        {"role": "user", "content": "rb"},
        {"role": "user", "content": "rc"},
        {"role": "user", "content": "continue"},
    ]


def test_normalize_messages_does_not_mutate_input() -> None:
    """Sanitizing is copy-on-write: the caller's list and dicts stay intact."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("a"), _tool_call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "zzz", "content": "stray"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("x")]},
    ]
    snapshot = copy.deepcopy(messages)
    normalize_messages_for_profile(ProviderProfile.MINIMAX, messages)
    assert messages == snapshot
