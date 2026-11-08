"""logging.Handler that pushes records onto a bounded asyncio.Queue.

The AlertEngine drains the queue and applies rule-based alerts.
"""

from __future__ import annotations

import asyncio
import logging


class AlertingLogHandler(logging.Handler):
    """Capture log records into a bounded asyncio.Queue (drop-oldest on overflow).

    The queue belongs to the event loop that drains it, and ``asyncio.Queue`` is
    not thread-safe. Records also come from worker threads -- ``send_email`` in
    the threadpool, the host-facts thread -- and a ``put_nowait`` from one of
    those woke the waiting drain with a plain ``call_soon``: an idle loop slept
    on with the record queued, and under asyncio debug the logging call itself
    raised in the worker, which then reported a sent email as failed. A record
    from another thread is now handed to the loop with ``call_soon_threadsafe``
    and queued there.
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        super().__init__(level=logging.DEBUG)
        self.queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=maxsize)
        self.dropped_count = 0
        # The loop that drains the queue: the one running where the handler is
        # made (the gateway's startup), else the first loop to log through it.
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def emit(self, record: logging.LogRecord) -> None:
        """Queue the record on its loop's thread, dropping the oldest on overflow."""
        try:
            try:
                running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            loop = self._loop
            if loop is None and running is not None:
                loop = self._loop = running
            if loop is None or running is loop:
                self._enqueue(record)
                return
            try:
                loop.call_soon_threadsafe(self._enqueue, record)
            except RuntimeError:
                # The loop has closed: nothing will drain this queue again.
                self.dropped_count += 1
        except Exception:
            self.handleError(record)

    def _enqueue(self, record: logging.LogRecord) -> None:
        """Push onto the queue, on the loop's thread, dropping the oldest when full."""
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            # Drop oldest, push newest.
            try:
                self.queue.get_nowait()
                self.dropped_count += 1
                self.queue.put_nowait(record)
            except Exception:
                self.dropped_count += 1


def install_alert_log_handler(handler: AlertingLogHandler) -> None:
    """Attach ``handler`` to the root logger, where every record reaches it once.

    Only there. The engine picks the request log out by ``record.name``; a
    second copy on that logger would see its records twice, and turning the
    logger's propagation off to prevent that also cuts it off from the root's
    console and ``LOG_FILE`` handlers, its only output.
    """
    logging.getLogger().addHandler(handler)


def uninstall_alert_log_handler(handler: AlertingLogHandler) -> None:
    """Detach ``handler`` from where :func:`install_alert_log_handler` put it."""
    logging.getLogger().removeHandler(handler)
