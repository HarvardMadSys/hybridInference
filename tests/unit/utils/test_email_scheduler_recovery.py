"""Scheduled broadcasts survive a restart and are neither lost nor sent twice.

On startup ``rehydrate_scheduled_broadcasts`` re-reads every broadcast still
'scheduled': one whose time has passed (the process was down) fires now, a
future one goes back on the scheduler. Nothing here had a test.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from serving.utils import email_scheduler as scheduler_mod


class FakePool:
    def __init__(self, rows):
        self.rows = rows

    @asynccontextmanager
    async def acquire(self):
        class Conn:
            async def fetch(_self, sql, *args):
                assert "status = 'scheduled'" in sql
                return self.rows

        yield Conn()


@pytest.fixture
async def scheduler(monkeypatch):
    fired: list[str] = []

    async def record(broadcast_id: str) -> None:
        fired.append(broadcast_id)

    monkeypatch.setattr(scheduler_mod, "execute_broadcast", record)
    scheduler_mod.start_scheduler(FakePool([]))
    try:
        yield scheduler_mod.get_scheduler(), fired
    finally:
        scheduler_mod.stop_scheduler()
        monkeypatch.setattr(scheduler_mod, "_db_pool", None)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_missed_broadcast_fires_now_and_a_future_one_is_rescheduled(scheduler, monkeypatch):
    sched, fired = scheduler
    now = datetime.now(UTC)
    later = now + timedelta(hours=2)
    monkeypatch.setattr(
        scheduler_mod,
        "_db_pool",
        FakePool(
            [
                {"id": "missed", "scheduled_at": now - timedelta(minutes=5)},
                {"id": "unscheduled-time", "scheduled_at": None},
                {"id": "later", "scheduled_at": later},
            ]
        ),
    )

    await scheduler_mod.rehydrate_scheduled_broadcasts()
    await _settle()

    assert sorted(fired) == ["missed", "unscheduled-time"]
    [job] = sched.get_jobs()
    assert (job.id, job.args) == ("later", ("later",))
    assert job.next_run_time == later


async def test_rehydrating_twice_keeps_one_job_per_broadcast(scheduler, monkeypatch):
    """A second startup pass (or a re-run) must not schedule a second send."""
    sched, _fired = scheduler
    later = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(scheduler_mod, "_db_pool", FakePool([{"id": "b1", "scheduled_at": later}]))

    await scheduler_mod.rehydrate_scheduled_broadcasts()
    await scheduler_mod.rehydrate_scheduled_broadcasts()

    assert [job.id for job in sched.get_jobs()] == ["b1"]


async def test_without_a_database_nothing_is_rehydrated(scheduler, monkeypatch):
    sched, fired = scheduler
    monkeypatch.setattr(scheduler_mod, "_db_pool", None)

    await scheduler_mod.rehydrate_scheduled_broadcasts()

    assert fired == [] and sched.get_jobs() == []


async def test_schedule_now_or_later_and_cancel(scheduler):
    sched, fired = scheduler

    scheduler_mod.schedule_broadcast("now", None)
    scheduler_mod.schedule_broadcast("later", datetime.now(UTC) + timedelta(days=1))
    await _settle()

    assert fired == ["now"]
    assert [job.id for job in sched.get_jobs()] == ["later"]

    scheduler_mod.cancel_broadcast_job("later")
    scheduler_mod.cancel_broadcast_job("never-scheduled")  # another replica's: a no-op
    assert sched.get_jobs() == []


async def test_a_future_broadcast_without_a_running_scheduler_is_refused():
    """Silently dropping it would leave it 'scheduled' and never sent."""
    scheduler_mod.stop_scheduler()

    with pytest.raises(RuntimeError, match="Scheduler not started"):
        scheduler_mod.schedule_broadcast("b", datetime.now(UTC) + timedelta(hours=1))
    scheduler_mod.cancel_broadcast_job("b")  # and cancelling is still safe
