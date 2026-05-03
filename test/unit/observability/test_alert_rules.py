"""Tests for AlertEngine and individual rule classes."""

from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine
from serving.observability.log_handler import AlertingLogHandler


async def test_engine_starts_and_stops_cleanly():
    handler = AlertingLogHandler(maxsize=10)
    cfg = AlertConfig()
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    await engine.start()
    assert engine.is_running()
    await engine.stop()
    assert not engine.is_running()
