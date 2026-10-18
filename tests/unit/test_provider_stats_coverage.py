"""Unit tests for the provider-stats rollup's coverage: which hours it fills, and when."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.admin import provider_stats_rollup as rollup


def utc(hour: int, minute: int = 0, *, day: int = 10) -> datetime:
    return datetime(2026, 5, day, hour, minute, tzinfo=timezone.utc)


def freeze(monkeypatch, now: datetime) -> None:
    """Fix the rollup module's clock at *now*."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(rollup, "datetime", FrozenDateTime)


class FakeConnection:
    """Answers the rollup's queries and records every statement it runs.

    ``missing`` is what MISSING_HOURS_SQL reports, cut to the window asked
    about and newest first, as the query orders it; ``covered_any`` is whether
    the coverage table holds a row; rolling up an hour in ``failing`` raises.
    Each call is recorded as ``(sql, args, transaction)``, where
    ``transaction`` numbers the transaction the statement ran in, or is None
    outside one.
    """

    def __init__(
        self,
        *,
        missing: list[datetime],
        covered_any: bool = True,
        lock_free: bool = True,
        failing: tuple[datetime, ...] = (),
    ) -> None:
        self.missing = missing
        self.covered_any = covered_any
        self.lock_free = lock_free
        self.failing = failing
        self.calls: list[tuple[str, tuple[Any, ...], int | None]] = []
        self._transactions = 0
        self._current: int | None = None

    def transaction(self):
        connection = self

        class Transaction:
            async def __aenter__(self):
                connection._transactions += 1
                connection._current = connection._transactions

            async def __aexit__(self, *exc):
                connection._current = None
                return False

        return Transaction()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, datetime]]:
        self.calls.append((sql, args, self._current))
        assert sql == rollup.MISSING_HOURS_SQL
        start, end = args
        hours = sorted((hour for hour in self.missing if start <= hour < end), reverse=True)
        return [{"hour_bucket": hour} for hour in hours]

    async def fetchval(self, sql: str, *args: Any) -> bool:
        self.calls.append((sql, args, self._current))
        if "pg_try_advisory_lock" in sql:
            return self.lock_free
        return self.covered_any

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args, self._current))
        if sql == rollup.ROLLUP_SQL:
            if args[0] in self.failing:
                raise RuntimeError(f"cannot roll up {args[0]:%H:00}")
            return "INSERT 0 2"
        return "OK"

    def statements(self, *sqls: str) -> list[tuple[str, tuple[Any, ...], int | None]]:
        return [call for call in self.calls if call[0] in sqls]


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self):
        connection = self.connection

        class Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc):
                return False

        return Acquire()


@pytest.mark.parametrize(
    ("now", "due_end"),
    [
        (utc(12, 4) + timedelta(seconds=59), utc(11)),
        (utc(12, 5), utc(12)),
        (utc(12, 59), utc(12)),
        (utc(0, 2), utc(23, day=9)),
    ],
)
def test_an_hour_is_due_once_the_rollup_minute_passes(now: datetime, due_end: datetime) -> None:
    assert rollup._due_end(now) == due_end


@pytest.mark.asyncio
async def test_backfill_on_a_complete_table_is_one_query_and_takes_no_lock(monkeypatch) -> None:
    freeze(monkeypatch, utc(12, 30))
    connection = FakeConnection(missing=[])
    lock = AsyncMock(side_effect=AssertionError("took the rollup lock"))
    monkeypatch.setattr(rollup, "_try_lock_run", lock)

    assert await rollup.backfill_missing(FakePool(connection), days=30) == 0

    assert connection.calls == [
        (rollup.MISSING_HOURS_SQL, (utc(12) - timedelta(days=30), utc(12)), None)
    ]
    lock.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_rolls_up_only_the_missing_hours_newest_first(monkeypatch) -> None:
    freeze(monkeypatch, utc(12, 30))
    connection = FakeConnection(missing=[utc(2), utc(3), utc(9)])

    assert await rollup.backfill_missing(FakePool(connection), days=1) == 3

    # Each hour's rows and marker commit together, newest hour first.
    assert connection.statements(rollup.ROLLUP_SQL, rollup.MARK_COVERED_SQL) == [
        (rollup.ROLLUP_SQL, (utc(9), utc(10)), 1),
        (rollup.MARK_COVERED_SQL, (utc(9),), 1),
        (rollup.ROLLUP_SQL, (utc(3), utc(4)), 2),
        (rollup.MARK_COVERED_SQL, (utc(3),), 2),
        (rollup.ROLLUP_SQL, (utc(2), utc(3)), 3),
        (rollup.MARK_COVERED_SQL, (utc(2),), 3),
    ]
    sqls = [sql for sql, _, _ in connection.calls]
    # It reads the gaps again once it holds the lock, since another replica may
    # have filled them meanwhile, and releases the lock when done.
    lock_at = sqls.index("SELECT pg_try_advisory_lock($1)")
    assert sqls[lock_at + 1 :].count(rollup.MISSING_HOURS_SQL) == 1
    assert sqls[-1] == "SELECT pg_advisory_unlock($1)"


@pytest.mark.asyncio
async def test_backfill_skips_while_another_replica_holds_the_lock(monkeypatch) -> None:
    freeze(monkeypatch, utc(12, 30))
    connection = FakeConnection(missing=[utc(3)], lock_free=False)

    assert await rollup.backfill_missing(FakePool(connection), days=1) == 0

    assert connection.statements(rollup.ROLLUP_SQL, rollup.MARK_COVERED_SQL) == []


@pytest.mark.asyncio
async def test_first_repair_marks_the_hours_that_already_have_rows() -> None:
    connection = FakeConnection(missing=[utc(3)], covered_any=False)

    await rollup._repair_missing(connection, start=utc(0), end=utc(12))

    sqls = [sql for sql, _, _ in connection.calls]
    # Marked before the gaps are read, so an hour with rows is not among them.
    assert sqls.index(rollup.SEED_COVERAGE_SQL) < sqls.index(rollup.MISSING_HOURS_SQL)


@pytest.mark.asyncio
async def test_later_repairs_do_not_mark_hours_from_their_rows() -> None:
    connection = FakeConnection(missing=[utc(3)], covered_any=True)

    await rollup._repair_missing(connection, start=utc(0), end=utc(12))

    assert connection.statements(rollup.SEED_COVERAGE_SQL) == []


@pytest.mark.asyncio
async def test_hourly_job_purges_where_its_window_starts(monkeypatch) -> None:
    freeze(monkeypatch, utc(12, 5))
    connection = FakeConnection(missing=[utc(8), utc(11)])

    await rollup.hourly_job(FakePool(connection), retention_days=30)

    start = utc(12) - timedelta(days=30)
    assert [args for _, args, _ in connection.statements(rollup.MISSING_HOURS_SQL)] == [
        (start, utc(12))
    ]
    assert [args for _, args, _ in connection.statements(rollup.ROLLUP_SQL)] == [
        (utc(11), utc(12)),
        (utc(8), utc(9)),
    ]
    purges = connection.statements(rollup.PURGE_SQL, rollup.PURGE_COVERAGE_SQL)
    assert [(sql, args) for sql, args, _ in purges] == [
        (rollup.PURGE_SQL, (start,)),
        (rollup.PURGE_COVERAGE_SQL, (start,)),
    ]
    # An hour's rows and its marker are purged together.
    assert purges[0][2] is not None
    assert purges[0][2] == purges[1][2]


@pytest.mark.asyncio
async def test_an_hour_that_keeps_failing_holds_up_neither_the_previous_hour_nor_the_purge(
    monkeypatch,
) -> None:
    freeze(monkeypatch, utc(12, 5))
    connection = FakeConnection(missing=[utc(8), utc(11)], failing=(utc(8),))

    await rollup.hourly_job(FakePool(connection), retention_days=30)

    assert connection.statements(rollup.PURGE_SQL)
    assert [args for _, args, _ in connection.statements(rollup.MARK_COVERED_SQL)] == [(utc(11),)]
    assert connection.calls[-1][0] == "SELECT pg_advisory_unlock($1)"


def test_registers_hourly_job_at_the_rollup_minute() -> None:
    scheduler = MagicMock()
    pool = object()

    rollup.register_rollup_job(scheduler, pool)

    scheduler.add_job.assert_called_once()
    args, kwargs = scheduler.add_job.call_args
    assert args[0] is rollup.hourly_job
    assert kwargs["args"] == [pool]
    assert kwargs["id"] == "rollup_provider_stats"
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1
    assert str(kwargs["trigger"].fields[6]) == str(rollup.ROLLUP_MINUTE)
