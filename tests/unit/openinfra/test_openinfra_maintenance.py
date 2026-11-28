"""The scheduled OpenInfra rollup refresh: one replica at a time, never raising."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from serving.openinfra import maintenance, store


class FakeConn:
    def __init__(self, locked: bool) -> None:
        self.locked = locked
        self.calls: list[str] = []

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        return self.locked

    async def execute(self, sql, *args):
        self.calls.append(sql)

    @asynccontextmanager
    async def _transaction(self):
        yield

    def transaction(self):
        return self._transaction()


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_a_run_refreshes_and_releases_the_lock(monkeypatch):
    refreshed = []

    async def fake_refresh(conn, now):
        refreshed.append(now)

    monkeypatch.setattr(store, "refresh_rollups", fake_refresh)
    conn = FakeConn(locked=True)
    assert await maintenance.run_refresh(FakePool(conn), now=42) is True
    assert refreshed == [42]
    assert "pg_try_advisory_lock" in conn.calls[0]
    assert "pg_advisory_unlock" in conn.calls[-1]


@pytest.mark.asyncio
async def test_a_replica_that_cannot_lock_skips_its_turn(monkeypatch):
    async def fake_refresh(conn, now):
        raise AssertionError("must not run")

    monkeypatch.setattr(store, "refresh_rollups", fake_refresh)
    conn = FakeConn(locked=False)
    assert await maintenance.run_refresh(FakePool(conn)) is False
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_the_job_logs_a_failure_and_still_unlocks(monkeypatch):
    async def failing(conn, now):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "refresh_rollups", failing)
    conn = FakeConn(locked=True)
    await maintenance.refresh_job(FakePool(conn))  # does not raise
    assert "pg_advisory_unlock" in conn.calls[-1]


def test_the_job_is_registered_every_five_minutes():
    class Scheduler:
        def add_job(self, func, **kwargs):
            self.func, self.kwargs = func, kwargs

    scheduler = Scheduler()
    maintenance.register_refresh_job(scheduler, object())
    assert scheduler.func is maintenance.refresh_job
    assert scheduler.kwargs["id"] == "openinfra_rollups"
    assert str(scheduler.kwargs["trigger"].fields[6]) == "1-59/5"  # minute
    assert scheduler.kwargs["max_instances"] == 1
