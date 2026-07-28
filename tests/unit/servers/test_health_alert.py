"""Test that an unhealthy DB triggers a Slack alert via _test_store_health."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.observability.alerts import reset_dedupe_state, reset_transition_state
from serving.servers.routers.health import _ALERT_TASKS, _test_store_health


@pytest.fixture(autouse=True)
def _clean_transition_state():
    """The breach tracker is shared, so an open store would leak between tests."""
    reset_transition_state()
    yield
    reset_transition_state()


async def _settle() -> None:
    """Wait for the detached alert deliveries this endpoint starts."""
    if _ALERT_TASKS:
        await asyncio.gather(*list(_ALERT_TASKS), return_exceptions=True)


async def test_db_disconnect_fires_critical_alert(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    reset_dedupe_state()

    # Operational store that raises on health_check
    op_store = MagicMock()
    op_store.health_check = AsyncMock(side_effect=RuntimeError("db unreachable"))

    with patch(
        "serving.observability.alerts.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        result = await _test_store_health(op_store, None)
        await _settle()
        assert result["healthy"] is False
        # At least one alert with CRITICAL severity and the right dedupe key prefix
        assert mock_alert.await_count == 1
        args, kwargs = mock_alert.call_args
        # severity is positional [0]
        assert args[0].value == "critical"
        assert "Database disconnected" in args[1]
        assert kwargs["dedupe_key"].startswith("db_disconnect:")


async def test_the_health_response_does_not_wait_for_alert_delivery(monkeypatch):
    """The container probe allows this endpoint five seconds, then restarts it.

    A send with both sinks unreachable spends their timeouts in sequence, so
    awaiting delivery here would let an alerting outage restart a healthy
    gateway.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    reset_dedupe_state()
    op_store = MagicMock()
    op_store.health_check = AsyncMock(side_effect=RuntimeError("db unreachable"))
    release = asyncio.Event()

    async def hanging_send(*_args, **_kwargs):
        await release.wait()
        return True

    with patch("serving.observability.alerts.alert_slack", new=hanging_send):
        result = await asyncio.wait_for(_test_store_health(op_store, None), timeout=1.0)
        assert result["healthy"] is False
        release.set()
        await _settle()
