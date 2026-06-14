"""Regression tests for serving.adapters.profiles."""

from __future__ import annotations

import pytest

from serving.adapters.profiles import (
    ProviderProfile,
    function_call_delta_to_tool_calls,
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
    assert info.to_dict()["cached_tokens"] == 913


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
