"""Regression: future-dated scheduled broadcasts must fire on the event loop.

APScheduler 3.11's AsyncIOExecutor runs plain (non-coroutine) job funcs in a
thread-pool worker thread, where asyncio.create_task raises RuntimeError (no
running loop) — so DateTrigger broadcasts failed silently. The scheduled job
must be a coroutine so it runs ON the loop.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from serving.utils import email_scheduler


def test_run_broadcast_is_coroutine_function() -> None:
    """The scheduled job must be a coroutine so AsyncIOExecutor runs it on the loop."""
    assert inspect.iscoroutinefunction(email_scheduler._run_broadcast)


@pytest.mark.asyncio
async def test_scheduled_job_registers_coroutine_and_fires_once(monkeypatch) -> None:
    """A near-future DateTrigger broadcast fires exactly once, without error."""
    calls: list[str] = []

    async def _recorder(broadcast_id: str) -> None:
        calls.append(broadcast_id)

    monkeypatch.setattr(email_scheduler, "execute_broadcast", _recorder)

    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    monkeypatch.setattr(email_scheduler, "_scheduler", scheduler)
    scheduler.start()
    try:
        run_at = datetime.now(timezone.utc) + timedelta(seconds=0.2)
        email_scheduler._add_scheduler_job("b-1", run_at)

        # The registered job func must be a coroutine function, else the
        # AsyncIOExecutor would run it in a worker thread and crash.
        job = scheduler.get_job("b-1")
        assert inspect.iscoroutinefunction(job.func)

        await asyncio.sleep(0.5)
        assert calls == ["b-1"]
    finally:
        scheduler.shutdown(wait=False)
