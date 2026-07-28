"""Integration test: drive synthetic log records through the full chain
and assert that rule-based alerts fire as expected.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine
from serving.observability.log_handler import AlertingLogHandler


async def test_full_chain_logs_to_slack(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    handler = AlertingLogHandler(maxsize=1000)
    request_log = logging.getLogger("serving.servers.middleware.request_log")
    request_log.addHandler(handler)
    request_log.setLevel(logging.INFO)

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.window_sec = 60
    cfg.rules.failed_request_rate.threshold_pct = 5.0
    cfg.rules.failed_request_rate.min_samples = 10
    cfg.rules.failed_request_rate.cooldown_sec = 1
    # Disable other rules
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.auth_failure_spike.enabled = False

    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alerts.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            for _ in range(10):
                request_log.info(
                    "http_request",
                    extra={"status_code": 200, "provider": "openai", "duration_ms": 100},
                )
            for _ in range(2):
                request_log.info(
                    "http_request",
                    extra={"status_code": 500, "provider": "openai", "duration_ms": 100},
                )
            for _ in range(50):
                if mock_alert.await_count > 0:
                    break
                await asyncio.sleep(0.01)
            assert mock_alert.await_count >= 1
        finally:
            await engine.stop()
            request_log.removeHandler(handler)
