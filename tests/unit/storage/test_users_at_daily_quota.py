"""Unit tests for ``PostgresOperationalStore.query_users_at_daily_quota``.

These mock the asyncpg pool, so what they can check is the *shape* of the
query — that it asks the gate's question, binding ``serving.quota``'s constants
rather than copies of them, through the same join
``get_quota_context_for_user`` uses — plus the row mapping and the truncation
warning. See ``test_binds_the_enforcers_constants_rather_than_literals`` for
the limit of that: it ties this query to the cloud-agent *grant* door, and
cannot speak for the API-key door, which hardcodes its own copies of the same
two numbers.

Whether the predicate selects the right rows is a question only a real
database can answer; that test lives in
``tests/integration/storage/test_users_at_daily_quota_postgres.py`` (marked
``dbtest``, and never yet executed in this environment — see its module
docstring). These two are meant to be read together: a change that drifts the
join away from ``get_quota_context_for_user`` fails here without needing a
database, and a change that breaks the boundary fails there.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving import quota
from serving.storage.postgres_operational import PostgresOperationalStore


@pytest.fixture
def pg_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.fixture
def pg_pool(pg_conn: MagicMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield pg_conn

    pool.acquire = _acquire
    return pool


@pytest.fixture
def store(pg_pool: MagicMock) -> PostgresOperationalStore:
    return PostgresOperationalStore(pg_pool)


def _row(user_id: str, role: str, spend: float, quota_usd: float) -> dict:
    return {"user_id": user_id, "role": role, "spend_usd": spend, "quota_usd": quota_usd}


def _normalized_sql(pg_conn: MagicMock) -> str:
    """The executed SQL with runs of whitespace collapsed."""
    return re.sub(r"\s+", " ", pg_conn.fetch.await_args.args[0]).strip()


class TestQueryShape:
    """The query must ask exactly what the quota gate asks."""

    async def test_binds_the_enforcers_constants_rather_than_literals(self, store, pg_conn):
        """The query must bind ``serving.quota``'s constants, not copies of them.

        What this actually protects is narrower than "the alert and the 429
        always agree", so be precise about it: it binds this query to the
        *grant* door, which reads the same two constants through
        ``quota.check``/``quota.resolve_quota``. Retune ``serving/quota.py`` and
        both move together; paste ``0.01`` into the SQL and they stop.

        It cannot protect the API-key door. ``verify_api_key`` in
        ``serving/servers/auth.py`` — the path nearly all traffic takes —
        hardcodes its own ``0.01`` and its own ``1000.0`` inline and never
        imports ``serving.quota`` for the gate. The values are equal today by
        hand, and no test here (or anywhere) would notice if one side were
        tuned alone.
        """
        await store.query_users_at_daily_quota()

        args = pg_conn.fetch.await_args.args
        assert quota.ESTIMATED_REQUEST_COST_USD in args[1:]
        assert quota.DEFAULT_DAILY_QUOTA_USD in args[1:]
        assert "0.01" not in args[0]
        assert "1000" not in args[0]

    async def test_predicate_matches_quota_check(self, store, pg_conn):
        """``spend + estimate > cap`` — the same comparison quota.check makes."""
        await store.query_users_at_daily_quota()

        sql = _normalized_sql(pg_conn)
        assert "udc.cost_usd + $3::float8 > COALESCE(k.quota_daily_cost_usd, $2::float8)" in sql

    async def test_join_matches_canonical_quota_context_join(self, store, pg_conn):
        """Same join get_quota_context_for_user uses to resolve a user's cap."""
        await store.query_users_at_daily_quota()

        sql = _normalized_sql(pg_conn)
        assert "k.user_id = u.id" in sql
        assert "k.status = 'active'" in sql
        assert "(k.expires_at IS NULL OR k.expires_at > NOW())" in sql
        assert "u.status = 'active'" in sql

    async def test_day_key_matches_the_counter_tables_text_day(self, store, pg_conn):
        """``user_daily_cost.day`` is TEXT in UTC; compare it as stored."""
        await store.query_users_at_daily_quota()

        sql = _normalized_sql(pg_conn)
        assert "udc.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')" in sql

    async def test_orders_by_overage_not_raw_spend(self, store, pg_conn):
        """A truncated list must keep the worst offenders, not the biggest caps."""
        await store.query_users_at_daily_quota()

        sql = _normalized_sql(pg_conn)
        assert "ORDER BY (udc.cost_usd - COALESCE(k.quota_daily_cost_usd, $2::float8)) DESC" in sql


class TestResults:
    """Row mapping and truncation."""

    async def test_maps_rows_to_user_role_spend_quota(self, store, pg_conn):
        pg_conn.fetch.return_value = [_row("u1", "pro", 80.05, 80.0)]

        rows = await store.query_users_at_daily_quota()

        assert rows == [("u1", "pro", 80.05, 80.0)]

    async def test_fetches_one_extra_row_to_detect_truncation(self, store, pg_conn):
        await store.query_users_at_daily_quota(limit=10)

        assert pg_conn.fetch.await_args.args[1] == 11

    async def test_truncates_to_limit_and_warns(self, store, pg_conn, caplog):
        pg_conn.fetch.return_value = [_row(f"u{i}", "free", 20.0, 20.0) for i in range(4)]

        with caplog.at_level("WARNING"):
            rows = await store.query_users_at_daily_quota(limit=3)

        assert len(rows) == 3
        # Silently dropping the fourth user is the failure mode this guards:
        # nobody would ever learn the list was cut.
        assert any("truncated" in record.getMessage() for record in caplog.records)

    async def test_full_page_without_overflow_does_not_warn(self, store, pg_conn, caplog):
        pg_conn.fetch.return_value = [_row(f"u{i}", "free", 20.0, 20.0) for i in range(3)]

        with caplog.at_level("WARNING"):
            rows = await store.query_users_at_daily_quota(limit=3)

        assert len(rows) == 3
        assert not [r for r in caplog.records if "truncated" in r.getMessage()]
