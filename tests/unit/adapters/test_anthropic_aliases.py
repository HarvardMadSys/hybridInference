"""Unit tests for the Anthropic public-display-id alias map."""

from __future__ import annotations

from serving.adapters.anthropic_aliases import ANTHROPIC_MODEL_ALIASES, resolve_anthropic_alias


def test_known_alias_resolves():
    assert resolve_anthropic_alias("claude-3-5-sonnet-latest") == "claude-sonnet-4.6"
    assert resolve_anthropic_alias("claude-3-5-sonnet-20241022") == "claude-sonnet-4.6"


def test_unknown_returns_input_unchanged():
    assert resolve_anthropic_alias("nonexistent-model") == "nonexistent-model"


def test_already_canonical_passes_through():
    assert resolve_anthropic_alias("claude-opus-4.7") == "claude-opus-4.7"


def test_context_window_marker_is_stripped():
    # Claude Code appends "[1m]" when the 1M-context variant is selected.
    assert resolve_anthropic_alias("claude-opus-4-8[1m]") == "claude-opus-4-8"
    assert resolve_anthropic_alias("claude-sonnet-5[1m]") == "claude-sonnet-5"
    # Marker is stripped before the alias map is consulted.
    assert resolve_anthropic_alias("claude-sonnet-4-6[1m]") == "claude-sonnet-4.6"
    # Only a trailing bracketed size marker is treated as a marker.
    assert resolve_anthropic_alias("model[1m]x") == "model[1m]x"
    assert resolve_anthropic_alias("model[big]") == "model[big]"


def test_alias_map_has_required_entries():
    required_aliases = {
        "claude-3-5-sonnet-latest",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-latest",
        "claude-3-opus-20240229",
    }
    assert required_aliases.issubset(ANTHROPIC_MODEL_ALIASES.keys())
