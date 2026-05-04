"""Tracked fire-and-forget task scheduler.

Wraps asyncio.ensure_future to emit a structured ``tracked_task_completed``
log event on success or failure. Consumed by TrackedTaskFailureRateRule
in serving.observability.alert_rules.

Use this for any background work where the caller doesn't await the
result (DB writes, telemetry, dual-write shadows). The naked
``asyncio.ensure_future`` / ``asyncio.create_task`` patterns are
permitted only for tasks whose completion is otherwise observable
(e.g., the AlertEngine drain task).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

log = logging.getLogger(__name__)

_TRACKED_TASKS: set[asyncio.Task[None]] = set()


def tracked_task(coro: Awaitable[None], *, name: str) -> asyncio.Task[None]:
    """Schedule ``coro`` and emit a tracked_task_completed log event when done.

    The ``name`` becomes the dimension key for the alert rule —
    use a short, stable identifier (e.g., "request_log", "cost_increment",
    "dual_write_shadow"). Returns the wrapping task; callers normally
    discard the return value.
    """

    async def _runner() -> None:
        start = time.monotonic()
        try:
            await coro
            with contextlib.suppress(Exception):
                # Never let logging itself escape.
                log.info(
                    "tracked_task_completed",
                    extra={
                        "event": "tracked_task_completed",
                        "task_name": name,
                        "success": True,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
        except Exception as e:
            with contextlib.suppress(Exception):
                # Never let logging itself escape.
                log.warning(
                    "tracked_task_completed",
                    extra={
                        "event": "tracked_task_completed",
                        "task_name": name,
                        "success": False,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                        "error": str(e)[:200],
                        "error_type": type(e).__name__,
                    },
                    exc_info=False,
                )

    task = asyncio.ensure_future(_runner())
    _TRACKED_TASKS.add(task)
    task.add_done_callback(_TRACKED_TASKS.discard)
    return task
