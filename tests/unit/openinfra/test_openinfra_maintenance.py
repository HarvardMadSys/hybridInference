"""The scheduled OpenInfra maintenance: one replica at a time, in order, never raising."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from serving.openinfra import archive, config, maintenance, store


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


@pytest.fixture
def steps(monkeypatch):
    """Record each step a run takes, in order; the settings as given."""
    taken: list[tuple] = []

    async def convert(conn):
        taken.append(("convert",))
        return 0

    async def refresh(conn, now):
        taken.append(("rollups", now))

    async def archive_weeks(conn, directory, now):
        taken.append(("archive", directory, now))
        return []

    async def prune(conn, now, days, *, archived_only):
        taken.append(("prune", now, days, archived_only))
        return {"openinfra_gpu_samples": 0}

    monkeypatch.setattr(store, "convert_six_second_buckets", convert)
    monkeypatch.setattr(store, "refresh_rollups", refresh)
    monkeypatch.setattr(archive, "archive_ended_weeks", archive_weeks)
    monkeypatch.setattr(archive, "prune", prune)
    monkeypatch.setattr(config, "retention_days", lambda: 7)
    monkeypatch.setattr(config, "archive_dir", lambda: None)
    return taken


@pytest.mark.asyncio
async def test_a_run_refreshes_prunes_and_releases_the_lock(steps):
    conn = FakeConn(locked=True)
    assert await maintenance.run_maintenance(FakePool(conn), now=42) is True
    # Without an archive directory: no archive, and old rows go unarchived.
    assert steps == [("convert",), ("rollups", 42), ("prune", 42, 7, False)]
    assert "pg_try_advisory_lock" in conn.calls[0]
    assert "pg_advisory_unlock" in conn.calls[-1]


@pytest.mark.asyncio
async def test_with_an_archive_directory_only_archived_weeks_are_pruned(steps, monkeypatch):
    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    await maintenance.run_maintenance(FakePool(FakeConn(locked=True)), now=42)
    assert steps == [
        ("convert",),
        ("rollups", 42),
        ("archive", Path("/archive"), 42),
        ("prune", 42, 7, True),
    ]


@pytest.mark.asyncio
async def test_a_failed_archive_is_logged_and_the_run_goes_on(steps, monkeypatch, caplog):
    async def failing(conn, directory, now):
        raise OSError("read-only file system")

    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    monkeypatch.setattr(archive, "archive_ended_weeks", failing)
    conn = FakeConn(locked=True)
    assert await maintenance.run_maintenance(FakePool(conn), now=42) is True
    assert steps == [("convert",), ("rollups", 42), ("prune", 42, 7, True)]
    assert "openinfra archive to /archive failed" in caplog.text
    assert "pg_advisory_unlock" in conn.calls[-1]


@pytest.mark.asyncio
async def test_a_failed_refresh_deletes_nothing(steps, monkeypatch):
    async def failing(conn, now):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "refresh_rollups", failing)
    conn = FakeConn(locked=True)
    with pytest.raises(RuntimeError):
        await maintenance.run_maintenance(FakePool(conn), now=42)
    assert steps == [("convert",)]
    assert "pg_advisory_unlock" in conn.calls[-1]


@pytest.mark.asyncio
async def test_a_replica_that_cannot_lock_skips_its_turn(steps):
    conn = FakeConn(locked=False)
    assert await maintenance.run_maintenance(FakePool(conn)) is False
    assert steps == []
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_the_job_logs_a_failure_and_still_unlocks(steps, monkeypatch):
    async def failing(conn, now):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "refresh_rollups", failing)
    conn = FakeConn(locked=True)
    await maintenance.maintenance_job(FakePool(conn))  # does not raise
    assert "pg_advisory_unlock" in conn.calls[-1]


def test_the_job_is_registered_every_five_minutes():
    class Scheduler:
        def add_job(self, func, **kwargs):
            self.func, self.kwargs = func, kwargs

    scheduler = Scheduler()
    maintenance.register_maintenance_job(scheduler, object())
    assert scheduler.func is maintenance.maintenance_job
    assert scheduler.kwargs["id"] == "openinfra_maintenance"
    assert str(scheduler.kwargs["trigger"].fields[6]) == "1-59/5"  # minute
    assert scheduler.kwargs["max_instances"] == 1
