"""The ``state_changes`` block of alerts.yaml reaches the two state alert sites.

Before ``state_alert_policy`` existed, both sites passed a literal
``cooldown_sec=300`` and never looked at ``enabled``, so a deployment could set
either knob and be paged exactly as if it had not. These tests pin the wiring:
a policy applied at startup is what the breaker and the health route use.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routing.endpoint_health import _ALERT_TASKS, _CircuitBreaker, _CircuitState
from serving.observability.alert_config import (
    AlertConfig,
    CircuitOpenStateChange,
    StateChange,
    StateChanges,
)
from serving.observability.alerts import reset_transition_state
from serving.observability.state_alert_policy import (
    apply_state_change_policy,
    circuit_open_policy,
    db_disconnect_policy,
    reset_state_change_policy,
)
from serving.servers.routers.health import _ALERT_TASKS as health_alert_tasks, _test_store_health


@pytest.fixture(autouse=True)
def _restore_policy():
    """Leave the module-level policy as found; it outlives any one test."""
    yield
    reset_state_change_policy()


def _trip_env(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()


async def _drain_alert_tasks():
    tasks = list(_ALERT_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_defaults_match_the_config_models():
    reset_state_change_policy()
    assert circuit_open_policy() == CircuitOpenStateChange()
    assert db_disconnect_policy() == StateChange()


def test_apply_installs_the_loaded_block():
    apply_state_change_policy(
        StateChanges(
            circuit_open=CircuitOpenStateChange(cooldown_sec=120, page_on_usage_limit=False),
            db_disconnect=StateChange(enabled=False, cooldown_sec=900),
        )
    )
    assert circuit_open_policy().cooldown_sec == 120
    assert circuit_open_policy().page_on_usage_limit is False
    assert db_disconnect_policy().enabled is False
    assert db_disconnect_policy().cooldown_sec == 900


def test_a_config_with_no_state_changes_block_still_yields_defaults():
    # ``load_alert_config`` on a file with only ``rules:`` returns the default
    # container rather than None, so the apply path cannot install a null policy.
    apply_state_change_policy(AlertConfig().state_changes)
    assert circuit_open_policy().cooldown_sec == 300


async def test_circuit_page_uses_the_configured_cooldown(monkeypatch):
    _trip_env(monkeypatch)
    apply_state_change_policy(StateChanges(circuit_open=CircuitOpenStateChange(cooldown_sec=120)))
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail="boom")
        cb.on_failure(reason="chat_exception", detail="boom")  # CLOSED -> OPEN
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        assert mock_alert.await_args.kwargs["cooldown_sec"] == 120


async def test_circuit_recovery_uses_the_configured_cooldown(monkeypatch):
    # The resolving edge carries the same key, so a mismatched cooldown there
    # would describe the incident differently on the way out than on the way in.
    _trip_env(monkeypatch)
    apply_state_change_policy(StateChanges(circuit_open=CircuitOpenStateChange(cooldown_sec=120)))
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail="boom")
        cb.on_failure(reason="chat_exception", detail="boom")
        cb.state = _CircuitState.HALF_OPEN
        cb.on_success()
        await _drain_alert_tasks()
        resolutions = [
            call for call in mock_alert.await_args_list if call.kwargs.get("breached") is False
        ]
        assert resolutions, "recovery edge never reported"
        assert resolutions[-1].kwargs["cooldown_sec"] == 120


async def test_circuit_disabled_never_pages(monkeypatch):
    _trip_env(monkeypatch)
    apply_state_change_policy(StateChanges(circuit_open=CircuitOpenStateChange(enabled=False)))
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail="boom")
        cb.on_failure(reason="chat_exception", detail="boom")
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        fired = [call for call in mock_alert.await_args_list if call.kwargs.get("breached") is True]
        assert fired == []


async def test_circuit_disabled_is_logged_as_its_own_gate(monkeypatch, caplog):
    # The suppressed record is the only trace left when the page is off, so it
    # has to say *which* knob held it, not blame the plan-usage mute.
    _trip_env(monkeypatch)
    apply_state_change_policy(StateChanges(circuit_open=CircuitOpenStateChange(enabled=False)))
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with (
        caplog.at_level("INFO", logger="routing.endpoint_health"),
        patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()),
    ):
        cb.on_failure(reason="chat_exception", detail="boom")
        cb.on_failure(reason="chat_exception", detail="boom")
        await _drain_alert_tasks()

    gates = [
        record.gate
        for record in caplog.records
        if getattr(record, "event", None) == "circuit_open_alert_suppressed"
    ]
    assert gates == ["circuit_open_disabled"]


def _broken_store():
    store = MagicMock()
    store.health_check = AsyncMock(side_effect=RuntimeError("db unreachable"))
    return store


async def _settle_health_tasks() -> None:
    if health_alert_tasks:
        await asyncio.gather(*list(health_alert_tasks), return_exceptions=True)


async def test_db_disconnect_uses_the_configured_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    reset_transition_state()
    apply_state_change_policy(StateChanges(db_disconnect=StateChange(cooldown_sec=900)))

    with patch("serving.servers.routers.health.alert_on_transition", new=AsyncMock()) as mock_alert:
        result = await _test_store_health(_broken_store(), None)
        await _settle_health_tasks()

    assert result["healthy"] is False
    assert mock_alert.await_args.kwargs["cooldown_sec"] == 900


async def test_db_disconnect_disabled_reports_nothing(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    reset_transition_state()
    apply_state_change_policy(StateChanges(db_disconnect=StateChange(enabled=False)))

    with patch("serving.servers.routers.health.alert_on_transition", new=AsyncMock()) as mock_alert:
        result = await _test_store_health(_broken_store(), None)
        await _settle_health_tasks()

    # The health verdict is unchanged — only the page is off.
    assert result["healthy"] is False
    mock_alert.assert_not_awaited()
