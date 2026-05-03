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
            logger.info("m%d" % i)

        # Queue holds latest 2, dropped count = 3
        msgs = []
        while not handler.queue.empty():
            rec = handler.queue.get_nowait()
            msgs.append(rec.getMessage())
        assert len(msgs) == 2
        assert handler.dropped_count == 3
    finally:
        logger.removeHandler(handler)
