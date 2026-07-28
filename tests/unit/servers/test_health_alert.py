"""Test that an unhealthy DB triggers a Slack alert via _test_store_health."""

from unittest.mock import AsyncMock, MagicMock, patch

from serving.servers.routers.health import _test_store_health


async def test_db_disconnect_fires_critical_alert(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    # Operational store that raises on health_check
    op_store = MagicMock()
    op_store.health_check = AsyncMock(side_effect=RuntimeError("db unreachable"))

    with patch(
        "serving.observability.alerts.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        result = await _test_store_health(op_store, None)
        assert result["healthy"] is False
        # At least one alert with CRITICAL severity and the right dedupe key prefix
        assert mock_alert.await_count == 1
        args, kwargs = mock_alert.call_args
        # severity is positional [0]
        assert args[0].value == "critical"
        assert "Database disconnected" in args[1]
        assert kwargs["dedupe_key"].startswith("db_disconnect:")
