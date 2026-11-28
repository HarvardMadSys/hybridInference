"""The scheduled OpenInfra jobs: one replica at a time, in order, never raising."""

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
        return [now]

    async def prune(conn, now, days, *, archived_only):
        taken.append(("prune", now, days, archived_only))
        return {"openinfra_gpu_samples": 0}

    monkeypatch.setattr(store, "convert_six_second_buckets", convert)
    monkeypatch.setattr(store, "refresh_rollups", refresh)
    monkeypatch.setattr(archive, "archive_ended_weeks", archive_weeks)
    monkeypatch.setattr(archive, "prune", prune)
    monkeypatch.setattr(config, "retention_days", lambda: 7)
    monkeypatch.setattr(config, "archive_dir", lambda: None)
    monkeypatch.setattr(config, "delete_unarchived", lambda: False)
    return taken


@pytest.mark.asyncio
async def test_without_an_archive_directory_nothing_is_deleted(steps):
    conn = FakeConn(locked=True)
    assert await maintenance.run_maintenance(FakePool(conn), now=42) is True
    assert steps == [("convert",), ("rollups", 42)]
    assert "pg_try_advisory_lock" in conn.calls[0]
    assert "pg_advisory_unlock" in conn.calls[-1]


@pytest.mark.asyncio
async def test_deleting_unarchived_rows_takes_the_switch(steps, monkeypatch):
    monkeypatch.setattr(config, "delete_unarchived", lambda: True)
    await maintenance.run_maintenance(FakePool(FakeConn(locked=True)), now=42)
    assert steps == [("convert",), ("rollups", 42), ("prune", 42, 7, False)]


@pytest.mark.asyncio
async def test_with_an_archive_directory_only_archived_weeks_are_deleted(steps, monkeypatch):
    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    await maintenance.run_maintenance(FakePool(FakeConn(locked=True)), now=42)
    # Archiving is a job of its own: a long one never holds up the rollups.
    assert steps == [("convert",), ("rollups", 42), ("prune", 42, 7, True)]


@pytest.mark.asyncio
async def test_a_failed_refresh_deletes_nothing(steps, monkeypatch):
    async def failing(conn, now):
        raise RuntimeError("down")

    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
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
async def test_the_archive_run_holds_its_own_lock(steps, monkeypatch):
    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    conn = FakeConn(locked=True)
    assert await maintenance.run_archive(FakePool(conn), now=42) == [42]
    assert steps == [("archive", Path("/archive"), 42)]
    assert conn.calls[0] == archive.TRY_LOCK_SQL
    assert conn.calls[-1] == archive.UNLOCK_SQL
    assert "openinfra-archive" in archive.TRY_LOCK_SQL


@pytest.mark.asyncio
async def test_the_archive_run_skips_without_a_directory_or_the_lock(steps, monkeypatch):
    conn = FakeConn(locked=True)
    assert await maintenance.run_archive(FakePool(conn), now=42) == []
    assert conn.calls == []
    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    assert await maintenance.run_archive(FakePool(FakeConn(locked=False)), now=42) is None
    assert steps == []


@pytest.mark.asyncio
async def test_the_jobs_log_a_failure_and_still_unlock(steps, monkeypatch, caplog):
    async def failing(*args):
        raise OSError("read-only file system")

    monkeypatch.setattr(config, "archive_dir", lambda: Path("/archive"))
    monkeypatch.setattr(store, "refresh_rollups", failing)
    monkeypatch.setattr(archive, "archive_ended_weeks", failing)
    conn = FakeConn(locked=True)
    await maintenance.maintenance_job(FakePool(conn))  # neither raises
    assert conn.calls[-1].startswith("SELECT pg_advisory_unlock")
    await maintenance.archive_job(FakePool(conn))
    assert conn.calls[-1] == archive.UNLOCK_SQL
    assert "openinfra archive to /archive failed" in caplog.text


def test_both_jobs_are_registered():
    class Scheduler:
        def __init__(self):
            self.jobs = {}

        def add_job(self, func, **kwargs):
            self.jobs[kwargs["id"]] = (func, kwargs)

    scheduler = Scheduler()
    maintenance.register_maintenance_job(scheduler, object())
    func, kwargs = scheduler.jobs["openinfra_maintenance"]
    assert func is maintenance.maintenance_job
    assert str(kwargs["trigger"].fields[6]) == "1-59/5"  # minute
    assert kwargs["max_instances"] == 1
    func, kwargs = scheduler.jobs["openinfra_archive"]
    assert func is maintenance.archive_job
    assert str(kwargs["trigger"].fields[6]) == "7"
    assert kwargs["max_instances"] == 1
