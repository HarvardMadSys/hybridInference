"""Work a request leaves running after its response, counted until it ends.

A request can start work that outlives it: a rejection's request-log row, an
interrupted Responses object being saved, an alert being sent. Nothing awaits
that work, so a process that stops while it runs loses it. Each such task is
registered here, by a short name, for as long as it runs, so that whoever has
to know whether the process has finished everything it started -- the release
handoff's seal (:mod:`serving.servers.handoff`) -- can count it.

Registering keeps the task referenced, which is what stops the event loop from
collecting it half-way; callers need no set of their own for that.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

_PENDING: dict[asyncio.Task[Any], str] = {}


def detach(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    """Run *coro* in a task of its own, counted under *name* until it ends."""
    return track(asyncio.ensure_future(coro), name=name)


def track(task: asyncio.Task[Any], *, name: str) -> asyncio.Task[Any]:
    """Count an already started *task* under *name* until it ends; return it."""
    if task.done():
        return task
    _PENDING[task] = name
    task.add_done_callback(_forget)
    return task


def _forget(task: asyncio.Task[Any]) -> None:
    _PENDING.pop(task, None)


def pending() -> dict[str, int]:
    """How many registered tasks are still running, by name."""
    return dict(Counter(name for task, name in _PENDING.items() if not task.done()))


def rejection_log(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Write a refused request's request-log row in a task of its own, counted as such."""
    return detach(coro, name="rejection_log")
