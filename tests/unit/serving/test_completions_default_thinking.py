"""Unit tests for completions `_resolve_thinking_param` precedence.

The gateway applies a model's ``default_thinking`` (e.g. disable reasoning)
only when the client sent no explicit reasoning control, so reason-by-default
providers don't silently spend thinking tokens. An explicit client choice —
either ``thinking`` or ``reasoning_effort`` — always suppresses the default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.servers.routers.completions import _resolve_thinking_param


def _payload(thinking=None, reasoning_effort=None):
    return SimpleNamespace(thinking=thinking, reasoning_effort=reasoning_effort)


def _cfg(default_thinking=None):
    return SimpleNamespace(default_thinking=default_thinking)


@pytest.mark.unit
def test_explicit_thinking_wins_over_default():
    payload = _payload(thinking={"type": "enabled", "budget_tokens": 1024})
    cfg = _cfg(default_thinking={"type": "disabled"})
    assert _resolve_thinking_param(payload, cfg) == {
        "type": "enabled",
        "budget_tokens": 1024,
    }


@pytest.mark.unit
def test_reasoning_effort_suppresses_default_thinking():
    # The client chose reasoning_effort; injecting a thinking default too could
    # double-signal or 400 on providers that reject both (e.g. Kimi).
    payload = _payload(reasoning_effort="low")
    cfg = _cfg(default_thinking={"type": "disabled"})
    assert _resolve_thinking_param(payload, cfg) is None


@pytest.mark.unit
def test_default_applied_when_client_silent():
    payload = _payload()
    cfg = _cfg(default_thinking={"type": "disabled"})
    assert _resolve_thinking_param(payload, cfg) == {"type": "disabled"}


@pytest.mark.unit
def test_no_default_configured_yields_none():
    payload = _payload()
    assert _resolve_thinking_param(payload, _cfg(default_thinking=None)) is None


@pytest.mark.unit
def test_missing_config_yields_none():
    assert _resolve_thinking_param(_payload(), None) is None


@pytest.mark.unit
def test_returned_default_is_copied_not_shared():
    default = {"type": "disabled"}
    cfg = _cfg(default_thinking=default)
    result = _resolve_thinking_param(_payload(), cfg)
    # Mutating the outgoing request must never leak into the shared config dict.
    assert result == default
    assert result is not default
    result["type"] = "enabled"
    assert default == {"type": "disabled"}
