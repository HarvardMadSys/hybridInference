"""The usage rollup's fill goes past a day it cannot count, and backs off from it.

``tests/integration/test_user_usage_rollup_postgres.py`` runs it against
PostgreSQL; these cover the loop's bookkeeping without one.
"""

from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest

from serving.storage import user_usage_rollup

pytestmark = pytest.mark.asyncio

DAYS = [date(2026, 11, 7), date(2026, 11, 6), date(2026, 11, 5)]


class _Conn:
    """Runs each day's statements; the INSERT of a day in *failing* times out."""

    def __init__(self, failing: set[date] | None = None, *, closed: bool = False) -> None:
        self.failing = failing or set()
        self.closed = closed
        self.statements: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    @contextlib.asynccontextmanager
    async def _transaction(self) -> Any:
        yield

    def transaction(self) -> Any:
        return self._transaction()

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> str:
        self.statements.append((sql, args, kwargs))
        if sql is user_usage_rollup._FILL_DAY_SQL and args[0] in self.failing:
            raise TimeoutError
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> bool:
        return True  # every day due

    def is_closed(self) -> bool:
        return self.closed


@pytest.fixture(autouse=True)
def _no_failures(monkeypatch):
    monkeypatch.setattr(user_usage_rollup, "_failed_days", {})


async def test_a_failing_day_is_skipped_and_the_rest_are_filled(caplog):
    conn = _Conn(failing={DAYS[1]})

    assert await user_usage_rollup.fill_days(conn, DAYS) == 2

    filled = [
        args[0] for sql, args, _ in conn.statements if sql is user_usage_rollup._MARK_FILLED_SQL
    ]
    assert filled == [DAYS[0], DAYS[2]]
    assert f"day {DAYS[1]} not filled (TimeoutError, 1 in a row)" in caplog.text


async def test_a_failed_day_is_retried_after_a_back_off_that_doubles(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(user_usage_rollup.time, "monotonic", lambda: clock[0])
    conn = _Conn(failing={DAYS[1]})

    await user_usage_rollup.fill_days(conn, [DAYS[1]])
    assert user_usage_rollup._failed_days[DAYS[1]] == (
        1,
        1000.0 + user_usage_rollup.FILL_RETRY_FIRST_SEC,
    )

    tried = len(conn.statements)
    await user_usage_rollup.fill_days(conn, [DAYS[1]])
    assert len(conn.statements) == tried  # backed off: not tried

    clock[0] += user_usage_rollup.FILL_RETRY_FIRST_SEC
    await user_usage_rollup.fill_days(conn, [DAYS[1]])
    failures, retry_at = user_usage_rollup._failed_days[DAYS[1]]
    assert (failures, retry_at - clock[0]) == (2, 2 * user_usage_rollup.FILL_RETRY_FIRST_SEC)

    # Never more than a day apart, and forgotten once it is filled.
    user_usage_rollup._failed_days[DAYS[1]] = (30, 0.0)
    await user_usage_rollup.fill_days(conn, [DAYS[1]])
    assert user_usage_rollup._failed_days[DAYS[1]][1] - clock[0] == (
        user_usage_rollup.FILL_RETRY_MAX_SEC
    )
    conn.failing.clear()
    user_usage_rollup._failed_days[DAYS[1]] = (31, 0.0)
    assert await user_usage_rollup.fill_days(conn, [DAYS[1]]) == 1
    assert user_usage_rollup._failed_days == {}


async def test_a_lost_connection_ends_the_run():
    conn = _Conn(failing={DAYS[0]}, closed=True)

    with pytest.raises(TimeoutError):
        await user_usage_rollup.fill_days(conn, DAYS)
    assert user_usage_rollup._failed_days == {}


async def test_a_days_statements_may_run_past_the_pools_minute():
    conn = _Conn()

    await user_usage_rollup.fill_days(conn, [DAYS[0]])

    by_sql = {sql: kwargs.get("timeout") for sql, _, kwargs in conn.statements}
    assert user_usage_rollup.FILL_DAY_TIMEOUT_SEC > 60
    for sql in (
        "SELECT pg_advisory_xact_lock($1)",
        "DELETE FROM user_usage_daily WHERE day = $1",
        user_usage_rollup._FILL_DAY_SQL,
    ):
        assert by_sql[sql] == user_usage_rollup.FILL_DAY_TIMEOUT_SEC
