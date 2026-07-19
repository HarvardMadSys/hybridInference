"""Unit tests for persistent geo-demand rollups and coverage repair."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.admin import geo_demand_rollup
from serving.admin.geo_demand_rollup import (
    _chunk_missing_hours,
    _repair_missing,
    ensure_geo_rollup_schema,
    register_rollup_job,
    rollup_window,
)
from serving.utils.geo_resolver import GeoResolver


class AsyncRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.rows:
            raise StopAsyncIteration
        return self.rows.pop(0)


class FakeReader:
    def get(self, ip: str) -> dict[str, Any] | None:
        records = {
            "8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
            "1.1.1.1": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
        }
        return records.get(ip)

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.transaction_entered = False
        self.execute_calls: list[tuple[Any, ...]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def transaction(self):
        connection = self

        class Transaction:
            async def __aenter__(self):
                connection.transaction_entered = True

            async def __aexit__(self, exc_type, exc, tb):
                connection.transaction_entered = False
                return False

        return Transaction()

    def cursor(self, *_args, **_kwargs):
        assert self.transaction_entered
        return AsyncRows(self.rows)

    async def execute(self, *args):
        assert self.transaction_entered
        self.execute_calls.append(args)
        return "DELETE 0"

    async def executemany(self, sql, args):
        assert self.transaction_entered
        self.executemany_calls.append((sql, list(args)))


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def utc(hour: int) -> datetime:
    return datetime(2026, 7, 1, hour, tzinfo=timezone.utc)


def resolver() -> GeoResolver:
    return GeoResolver(country_reader=FakeReader(), country_provider="dbip-lite")


@pytest.mark.asyncio
async def test_schema_is_idempotent_and_stores_no_raw_identifiers() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="OK")

    await ensure_geo_rollup_schema(connection)
    await ensure_geo_rollup_schema(connection)

    assert connection.execute.await_count == 4
    statements = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    assert "CREATE TABLE IF NOT EXISTS geo_hourly_coverage" in statements
    assert "CREATE TABLE IF NOT EXISTS geo_hourly_demand" in statements
    assert "hour_bucket" in statements
    assert "country_code" in statements
    assert "metadata" not in statements
    assert "user_id" not in statements
    assert " ip " not in statements.lower()


@pytest.mark.asyncio
async def test_rollup_atomically_writes_country_buckets_and_empty_hour_coverage() -> None:
    connection = FakeConnection(
        [
            {
                "hour": utc(0),
                "ip": "8.8.8.8",
                "request_count": 4,
                "completion_tokens": 20,
            },
            {
                "hour": utc(0),
                "ip": "1.1.1.1",
                "request_count": 2,
                "completion_tokens": 8,
            },
            {
                "hour": utc(0),
                "ip": None,
                "request_count": 1,
                "completion_tokens": 0,
            },
        ]
    )

    written = await rollup_window(
        connection,
        start=utc(0),
        end=utc(2),
        resolver=resolver(),
    )

    assert written == 2
    assert len(connection.execute_calls) == 1
    coverage_args = connection.executemany_calls[0][1]
    assert coverage_args == [(utc(0), 7, 6), (utc(1), 0, 0)]
    demand_args = connection.executemany_calls[1][1]
    assert set(demand_args) == {
        (utc(0), "USA", "NA", 6, 28),
        (utc(0), "?", "?", 1, 0),
    }
    assert "8.8.8.8" not in repr(connection.executemany_calls)
    assert connection.transaction_entered is False


def test_missing_hours_are_grouped_into_bounded_contiguous_chunks() -> None:
    hours = [utc(0), utc(1), utc(2), utc(4), utc(5)]

    assert _chunk_missing_hours(hours, chunk_hours=2) == [
        (utc(0), utc(2)),
        (utc(2), utc(3)),
        (utc(4), utc(6)),
    ]


@pytest.mark.asyncio
async def test_repair_resumes_only_gaps_and_forced_previous_hour(monkeypatch) -> None:
    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"hour_bucket": utc(0)}, {"hour_bucket": utc(2)}])
    rollup = AsyncMock(return_value=3)
    monkeypatch.setattr(geo_demand_rollup, "rollup_window", rollup)

    hours, rows = await _repair_missing(
        connection,
        start=utc(0),
        end=utc(4),
        resolver_factory=resolver,
        force_hours=(utc(2),),
    )

    assert hours == 3
    assert rows == 3
    assert rollup.await_count == 1
    call = rollup.await_args
    assert call.kwargs["start"] == utc(1)
    assert call.kwargs["end"] == utc(4)


def test_registers_hourly_coroutine_with_stable_scheduler_options() -> None:
    scheduler = MagicMock()
    pool = object()

    register_rollup_job(scheduler, pool)

    scheduler.add_job.assert_called_once()
    args, kwargs = scheduler.add_job.call_args
    assert args[0] is geo_demand_rollup.hourly_job
    assert kwargs["args"] == [pool]
    assert kwargs["id"] == "rollup_geo_demand"
    assert kwargs["replace_existing"] is True
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1
    assert kwargs["trigger"].fields[6].__str__() == "10"


@pytest.mark.asyncio
async def test_advisory_lock_skips_other_replica_and_unlocks_after_failure() -> None:
    connection = MagicMock()
    connection.fetchval = AsyncMock(side_effect=[False, True])
    connection.execute = AsyncMock(return_value="SELECT 1")

    class Pool:
        def acquire(self):
            return AsyncContext(connection)

    work = AsyncMock(side_effect=RuntimeError("failed rollup"))
    assert await geo_demand_rollup._try_lock_run(Pool(), work) is False
    work.assert_not_awaited()

    with pytest.raises(RuntimeError, match="failed rollup"):
        await geo_demand_rollup._try_lock_run(Pool(), work)
    connection.execute.assert_awaited_once_with(
        "SELECT pg_advisory_unlock($1)", geo_demand_rollup.ADVISORY_LOCK_KEY
    )


@pytest.mark.asyncio
async def test_hourly_job_checks_full_supported_window_and_forces_previous_hour(
    monkeypatch,
) -> None:
    repair = AsyncMock(return_value=(2, 5))
    monkeypatch.setattr(geo_demand_rollup, "_repair_missing", repair)

    async def run_work(_pool, work):
        connection = MagicMock()
        connection.execute = AsyncMock(return_value="DELETE 0")
        await work(connection)
        return True

    monkeypatch.setattr(geo_demand_rollup, "_try_lock_run", run_work)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 10, 12, 45, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(geo_demand_rollup, "datetime", FrozenDateTime)

    await geo_demand_rollup.hourly_job(object())

    call = repair.await_args
    assert call.kwargs["start"] == datetime(2026, 7, 10, 12, tzinfo=timezone.utc) - timedelta(
        days=90
    )
    assert call.kwargs["end"] == datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
    assert call.kwargs["force_hours"] == (datetime(2026, 7, 10, 11, tzinfo=timezone.utc),)
