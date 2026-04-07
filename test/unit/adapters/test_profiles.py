"""Regression tests for serving.adapters.profiles — function_call handling."""

from __future__ import annotations

from serving.adapters.profiles import (
    ProviderProfile,
    _function_call_to_tool_calls,
    function_call_delta_to_tool_calls,
)

# ---------------------------------------------------------------------------
# Non-streaming: _function_call_to_tool_calls
# ---------------------------------------------------------------------------


def test_complete_function_call_converts_to_tool_calls() -> None:
    result = _function_call_to_tool_calls({"name": "get_weather", "arguments": '{"city": "NYC"}'})
    assert result is not None
    assert len(result) == 1
    assert result[0]["function"]["name"] == "get_weather"
    assert result[0]["function"]["arguments"] == '{"city": "NYC"}'
    assert result[0]["type"] == "function"
    assert result[0]["id"].startswith("call_")


def test_function_call_without_name_returns_none() -> None:
    assert _function_call_to_tool_calls({"arguments": '{"x":1}'}) is None


def test_function_call_none_returns_none() -> None:
    assert _function_call_to_tool_calls(None) is None


# ---------------------------------------------------------------------------
# Streaming: function_call_delta_to_tool_calls
# ---------------------------------------------------------------------------


def test_delta_first_chunk_with_name_emits_full_tool_call() -> None:
    """First streaming delta carries function name — should emit id + type."""
    result = function_call_delta_to_tool_calls(
        ProviderProfile.LLAMA,
        {"name": "get_weather", "arguments": ""},
    )
    assert result is not None
    assert len(result) == 1
    assert result[0]["function"]["name"] == "get_weather"
    assert "id" in result[0]
    assert result[0]["type"] == "function"


def test_delta_continuation_with_only_arguments_emits_delta() -> None:
    """Subsequent streaming deltas carry only arguments — must NOT be dropped.

    This is the regression case: before the fix, argument-only deltas
    returned None because _function_call_to_tool_calls required a name.
    """
    result = function_call_delta_to_tool_calls(
        ProviderProfile.LLAMA,
        {"arguments": '{"cit'},
    )
    assert result is not None
    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["function"]["arguments"] == '{"cit'
    # Continuation deltas should NOT carry id or type
    assert "id" not in result[0]
    assert "type" not in result[0]


def test_delta_split_reassembly_sequence() -> None:
    """Simulate a full split-delta sequence: name chunk + N argument chunks."""
    deltas = [
        {"name": "search", "arguments": ""},
        {"arguments": '{"q'},
        {"arguments": 'uery":'},
        {"arguments": ' "hello"}'},
    ]
    fragments: list[str] = []
    name = None
    for i, fc in enumerate(deltas):
        result = function_call_delta_to_tool_calls(ProviderProfile.LLAMA, fc)
        assert result is not None, f"delta {i} was dropped: {fc}"
        if "name" in result[0].get("function", {}):
            name = result[0]["function"]["name"]
        fragments.append(result[0]["function"]["arguments"])

    assert name == "search"
    reassembled = "".join(fragments)
    assert reassembled == '{"query": "hello"}'


def test_tool_call_ids_are_unique() -> None:
    """Concurrent calls must not produce colliding IDs."""
    ids = {
        _function_call_to_tool_calls({"name": "fn", "arguments": "{}"})[0]["id"]  # type: ignore[index]
        for _ in range(100)
    }
    assert len(ids) == 100


def test_delta_non_llama_profile_returns_none() -> None:
    result = function_call_delta_to_tool_calls(
        ProviderProfile.DEFAULT,
        {"name": "fn", "arguments": "{}"},
    )
    assert result is None


def test_delta_empty_dict_returns_none() -> None:
    result = function_call_delta_to_tool_calls(ProviderProfile.LLAMA, {})
    assert result is None
