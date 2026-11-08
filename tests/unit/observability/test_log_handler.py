"""Tests for AlertingLogHandler."""

import asyncio
import logging
import math
import threading
import time

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


# --- records logged from worker threads -------------------------------------------


def _log_from_thread(logger: logging.Logger, message: str) -> BaseException | None:
    """Log ``message`` from a fresh thread, as ``send_email`` does from the threadpool."""
    raised: list[BaseException] = []

    def worker() -> None:
        try:
            logger.warning(message)
        except BaseException as exc:  # the point is to see it
            raised.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    return raised[0] if raised else None


def _drain_a_record_logged_from_a_thread(*, debug: bool) -> tuple[float, list[str], str | None]:
    """Log from a worker thread while the draining loop sits idle in ``select``.

    Returns how long the drain took to see the record (inf if not within five
    seconds), what the logging call raised in the worker, and the message the
    drain got. The loop runs in a thread of its own: a drain wedged by a wakeup
    that was never scheduled would hang the cancellation that ends a loop, and
    the test run with it, so this waits for the answer and not for that loop.
    """
    outcome: list[tuple[float, list[str], str | None]] = []
    answered = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_debug(debug)
        handler = AlertingLogHandler(maxsize=10)
        logger = logging.getLogger(f"test.alerts.handler.thread.{debug}")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        raised: list[str] = []
        logged_at: list[float] = []

        def worker() -> None:
            time.sleep(0.2)  # the loop is parked in select by now
            logged_at.append(loop.time())
            try:
                logger.warning("Email sent")
            except BaseException as exc:  # the point is to see it
                raised.append(repr(exc))

        drain = asyncio.create_task(handler.queue.get())
        thread = threading.Thread(target=worker)
        thread.start()
        # ``wait`` does not cancel, so a wedged drain cannot hang this await.
        done, _ = await asyncio.wait({drain}, timeout=5.0)
        seen_at = loop.time()
        thread.join(5)
        logger.removeHandler(handler)
        if drain in done:
            outcome.append((seen_at - logged_at[0], raised, drain.result().getMessage()))
        else:
            outcome.append((math.inf, raised, None))
        answered.set()

    threading.Thread(target=asyncio.run, args=(scenario(),), daemon=True).start()
    assert answered.wait(timeout=15), "the drain's loop never answered"
    return outcome[0]


async def test_a_record_from_a_worker_thread_wakes_an_idle_drain():
    """The drain gets the record at once, not whenever the loop next wakes for something else."""
    waited, raised, message = _drain_a_record_logged_from_a_thread(debug=False)
    assert raised == []
    assert message == "Email sent"
    # Nothing else is scheduled on that loop: an unwoken drain sleeps out the wait.
    assert waited < 1.0


async def test_logging_from_a_thread_never_raises_under_asyncio_debug():
    """Debug mode refuses ``call_soon`` from another thread; the logging call must not see it."""
    waited, raised, message = _drain_a_record_logged_from_a_thread(debug=True)
    assert raised == []
    assert message == "Email sent"
    assert waited < 1.0


async def test_a_full_queue_still_drops_the_oldest_for_a_threaded_record():
    handler = AlertingLogHandler(maxsize=2)
    logger = logging.getLogger("test.alerts.handler.thread-overflow")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("first")
        logger.warning("second")
        assert _log_from_thread(logger, "third") is None
        for _ in range(100):  # handed to the loop; queued on its next turn
            if handler.dropped_count:
                break
            await asyncio.sleep(0.01)
        assert [handler.queue.get_nowait().getMessage() for _ in range(2)] == ["second", "third"]
        assert handler.dropped_count == 1
    finally:
        logger.removeHandler(handler)


def test_a_record_after_its_loop_has_closed_is_counted_not_raised():
    loop = asyncio.new_event_loop()

    async def make() -> AlertingLogHandler:
        return AlertingLogHandler(maxsize=2)

    handler = loop.run_until_complete(make())
    loop.close()
    logger = logging.getLogger("test.alerts.handler.closed")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        assert _log_from_thread(logger, "late") is None
        assert handler.dropped_count == 1
    finally:
        logger.removeHandler(handler)
