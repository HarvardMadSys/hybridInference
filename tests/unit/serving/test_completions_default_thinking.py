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


def _cfg(default_thinking=None, thinking_disable_by_omission=False):
    return SimpleNamespace(
        default_thinking=default_thinking,
        thinking_disable_by_omission=thinking_disable_by_omission,
    )


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
def test_disable_by_omission_translates_explicit_disable_to_none():
    # Presence-enables provider (MiniMax M2.x): a client `{type: disabled}` must
    # become "send nothing" — forwarding it verbatim would keep reasoning ON.
    payload = _payload(thinking={"type": "disabled"})
    cfg = _cfg(thinking_disable_by_omission=True)
    assert _resolve_thinking_param(payload, cfg) is None


@pytest.mark.unit
def test_disable_by_omission_still_forwards_explicit_enable():
    # Opt-in must still work: an enable passes through untouched.
    payload = _payload(thinking={"type": "enabled", "budget_tokens": 1024})
    cfg = _cfg(thinking_disable_by_omission=True)
    assert _resolve_thinking_param(payload, cfg) == {
        "type": "enabled",
        "budget_tokens": 1024,
    }


@pytest.mark.unit
def test_disable_by_omission_defaults_to_none_when_silent():
    # No client param and no default -> nothing sent -> clean answer.
    assert _resolve_thinking_param(_payload(), _cfg(thinking_disable_by_omission=True)) is None


@pytest.mark.unit
def test_disable_by_omission_neutralizes_disable_shaped_default():
    # Even a mis-configured default_thinking={type: disabled} is omitted (not
    # forwarded) for presence-enables models, so it can't turn reasoning on.
    cfg = _cfg(default_thinking={"type": "disabled"}, thinking_disable_by_omission=True)
    assert _resolve_thinking_param(_payload(), cfg) is None


@pytest.mark.unit
def test_disable_without_flag_is_forwarded_unchanged():
    # Regression guard: the flag must not change behavior for other models —
    # a normal model still forwards an explicit `{type: disabled}` verbatim.
    payload = _payload(thinking={"type": "disabled"})
    assert _resolve_thinking_param(payload, _cfg()) == {"type": "disabled"}


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
