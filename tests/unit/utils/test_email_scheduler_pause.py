"""A release handoff pauses this process's scheduled jobs and counts the running ones."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from serving.utils import email_scheduler


@pytest.fixture
async def scheduler(monkeypatch):
    scheduler = AsyncIOScheduler(timezone=UTC)
    monkeypatch.setattr(email_scheduler, "_scheduler", scheduler)
    monkeypatch.setattr(email_scheduler, "_paused", False)
    monkeypatch.setattr(email_scheduler, "_db_pool", None)
    scheduler.start()
    yield scheduler
    scheduler.shutdown(wait=False)


async def test_a_paused_scheduler_runs_nothing_until_resumed(scheduler):
    calls: list[str] = []

    async def job() -> None:
        calls.append("ran")

    email_scheduler.pause_jobs()
    scheduler.add_job(
        job, "date", run_date=datetime.now(UTC) + timedelta(seconds=0.1), misfire_grace_time=60
    )
    await asyncio.sleep(0.3)
    assert calls == []

    email_scheduler.resume_jobs()
    await asyncio.sleep(0.3)
    assert calls == ["ran"]


async def test_running_jobs_counts_a_job_until_it_ends(scheduler):
    gate = asyncio.Event()

    async def job() -> None:
        await gate.wait()

    scheduler.add_job(job, "date", run_date=datetime.now(UTC))
    for _ in range(50):
        if email_scheduler.running_jobs():
            break
        await asyncio.sleep(0.02)
    assert email_scheduler.running_jobs() == 1

    gate.set()
    for _ in range(50):
        if not email_scheduler.running_jobs():
            break
        await asyncio.sleep(0.02)
    assert email_scheduler.running_jobs() == 0


async def test_an_immediate_broadcast_counts_while_it_sends(scheduler, monkeypatch):
    gate = asyncio.Event()

    async def send(broadcast_id: str) -> None:
        await gate.wait()

    monkeypatch.setattr(email_scheduler, "execute_broadcast", send)
    email_scheduler.schedule_broadcast("b-1", None)
    await asyncio.sleep(0)
    assert email_scheduler.running_jobs() == 1

    gate.set()
    await asyncio.sleep(0.01)
    assert email_scheduler.running_jobs() == 0


def test_pausing_without_a_scheduler_does_nothing(monkeypatch):
    monkeypatch.setattr(email_scheduler, "_scheduler", None)
    monkeypatch.setattr(email_scheduler, "_paused", False)
    email_scheduler.pause_jobs()
    email_scheduler.resume_jobs()
    assert email_scheduler.running_jobs() == 0
