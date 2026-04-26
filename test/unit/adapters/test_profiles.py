"""Regression tests for serving.adapters.profiles — function_call handling."""

from __future__ import annotations

from serving.adapters.profiles import (
    ProviderProfile,
    function_call_delta_to_tool_calls,
)

# ---------------------------------------------------------------------------
# Streaming: function_call_delta_to_tool_calls
# ---------------------------------------------------------------------------


def test_delta_returns_none_for_default_profile() -> None:
    """After Llama removal, function_call_delta_to_tool_calls returns None for all profiles."""
    result = function_call_delta_to_tool_calls(
        ProviderProfile.DEFAULT,
        {"name": "get_weather", "arguments": ""},
    )
    assert result is None


def test_delta_returns_none_for_zhipu_profile() -> None:
    """Zhipu profile does not convert function_call deltas."""
    result = function_call_delta_to_tool_calls(
        ProviderProfile.ZHIPU,
        {"arguments": '{"cit'},
    )
    assert result is None


def test_delta_deepseek_profile_returns_none() -> None:
    """DeepSeek profile does not convert function_call deltas."""
    result = function_call_delta_to_tool_calls(
        ProviderProfile.DEEPSEEK,
        {"name": "fn", "arguments": "{}"},
    )
    assert result is None


def test_delta_empty_dict_returns_none() -> None:
    result = function_call_delta_to_tool_calls(ProviderProfile.DEFAULT, {})
    assert result is None
