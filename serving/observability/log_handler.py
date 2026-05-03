"""logging.Handler that pushes records onto a bounded asyncio.Queue.

The AlertEngine drains the queue and applies rule-based alerts.
"""

from __future__ import annotations

import asyncio
import logging


class AlertingLogHandler(logging.Handler):
    """Capture log records into a bounded asyncio.Queue (drop-oldest on overflow)."""

    def __init__(self, maxsize: int = 10_000) -> None:
        super().__init__(level=logging.DEBUG)
        self.queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=maxsize)
        self.dropped_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Push the record onto the bounded queue, dropping the oldest on overflow."""
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
