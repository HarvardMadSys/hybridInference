"""Effective ``state_changes`` alert policy, applied once at startup.

The ``rules:`` block of ``alerts.yaml`` is read by ``AlertEngine``, which owns
the rules it configures. The ``state_changes:`` block has no such owner: both
alerts it configures are raised from outside the engine — ``circuit_open`` by
the breaker in ``routing/endpoint_health.py``, ``db_disconnect`` by the health
route — and each called ``alert_on_transition`` with a literal ``300`` and no
``enabled`` check. Every knob in that block except
``circuit_open.page_on_usage_limit`` therefore parsed, validated, and did
nothing: a deployment lowering ``cooldown_sec`` to 120 got 300, and one setting
``enabled: false`` kept being paged.

This module is where the loaded block lands so those call sites can read it.
Module-level rather than threaded through constructors because the breakers are
created per endpoint across the routing layer, long after config is loaded, and
the policy is deployment-wide by definition.

Defaults are the pydantic models' own, so a deployment with no ``alerts.yaml``
behaves exactly as it did before.

Applied once at startup and not re-read: a config change takes effect on the
next restart, which is also when the in-process transition trackers reset, so
flipping ``enabled`` cannot strand an incident this process still believes is
open.
"""

from __future__ import annotations

from serving.observability.alert_config import CircuitOpenStateChange, StateChange, StateChanges

_CIRCUIT_OPEN = CircuitOpenStateChange()
_DB_DISCONNECT = StateChange()


def apply_state_change_policy(state_changes: StateChanges) -> None:
    """Install ``state_changes`` as the policy the state alert sites read."""
    global _CIRCUIT_OPEN, _DB_DISCONNECT
    _CIRCUIT_OPEN = state_changes.circuit_open
    _DB_DISCONNECT = state_changes.db_disconnect


def reset_state_change_policy() -> None:
    """Restore the built-in defaults. For tests and repeated app startups."""
    apply_state_change_policy(StateChanges())


def set_usage_limit_paging(enabled: bool) -> None:
    """Set whether subscription usage-limit circuit trips page at all.

    Narrower than :func:`apply_state_change_policy` and kept separate because it
    is the one field with a caller that knows only this much: the bootstrap path
    that runs even when ``ALERTS_ENABLED`` is off, since the breaker pages
    ``alert_slack`` directly and is gated on the webhook env vars instead.
    """
    global _CIRCUIT_OPEN
    _CIRCUIT_OPEN = _CIRCUIT_OPEN.model_copy(update={"page_on_usage_limit": bool(enabled)})


def circuit_open_policy() -> CircuitOpenStateChange:
    """Return the effective ``state_changes.circuit_open`` configuration."""
    return _CIRCUIT_OPEN


def db_disconnect_policy() -> StateChange:
    """Return the effective ``state_changes.db_disconnect`` configuration."""
    return _DB_DISCONNECT
