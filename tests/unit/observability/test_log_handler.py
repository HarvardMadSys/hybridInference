"""Tests for AlertingLogHandler."""

import asyncio
import logging

from serving.observability.log_handler import AlertingLogHandler


async def test_handler_pushes_records_to_queue():
    handler = AlertingLogHandler(maxsize=10)
    logger = logging.getLogger("test.alerts.handler.1")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("hello", extra={"foo": "bar"})

        rec = await asyncio.wait_for(handler.queue.get(), timeout=1.0)
        assert rec.getMessage() == "hello"
        assert getattr(rec, "foo", None) == "bar"
    finally:
        logger.removeHandler(handler)


async def test_handler_drops_oldest_on_overflow():
    handler = AlertingLogHandler(maxsize=2)
    logger = logging.getLogger("test.alerts.handler.2")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for i in range(5):
            logger.info("m%d", i)

        # Queue holds latest 2, dropped count = 3
        msgs = []
        while not handler.queue.empty():
            rec = handler.queue.get_nowait()
            msgs.append(rec.getMessage())
        assert len(msgs) == 2
        assert handler.dropped_count == 3
    finally:
        logger.removeHandler(handler)


async def test_the_installed_handler_leaves_the_request_log_on_the_console():
    """The access log reaches stdout and LOG_FILE only through the root logger's handlers."""
    from serving.observability.log_handler import (
        install_alert_log_handler,
        uninstall_alert_log_handler,
    )

    console: list[logging.LogRecord] = []

    class _Console(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            console.append(record)

    root = logging.getLogger()
    sink = _Console(level=logging.INFO)
    handler = AlertingLogHandler(maxsize=10)
    request_log = logging.getLogger("serving.servers.middleware.request_log")
    previous_level = request_log.level
    root.addHandler(sink)
    request_log.setLevel(logging.INFO)
    install_alert_log_handler(handler)
    try:
        request_log.info("http_request")

        assert [record.getMessage() for record in console] == ["http_request"]
        assert handler.queue.qsize() == 1
    finally:
        uninstall_alert_log_handler(handler)
        root.removeHandler(sink)
        request_log.setLevel(previous_level)
