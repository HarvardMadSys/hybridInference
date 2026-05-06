"""Unit tests for new user-summary / cost-history store methods.

These tests mock the asyncpg pool/connection — no real DB calls. The d1
variant has analogous tests in test_d1_operational.py; this file is the
postgres equivalent for the new methods added in Tasks 2-4.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore


@pytest.fixture
def pg_conn() -> MagicMock:
    """Mock asyncpg connection with fetch/fetchrow/execute coroutines."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 0")
    return conn


@pytest.fixture
def pg_pool(pg_conn: MagicMock) -> MagicMock:
    """Mock asyncpg pool whose acquire() returns the mock connection."""
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield pg_conn

    pool.acquire = _acquire
    return pool


@pytest.fixture
def store(pg_pool: MagicMock) -> PostgresOperationalStore:
    return PostgresOperationalStore(pg_pool)


# ------------------------------------------------------------------
# get_user_cost_history
# ------------------------------------------------------------------


class TestGetUserCostHistory:
    """Tests for get_user_cost_history (single user)."""

    async def test_returns_daily_buckets_ordered_ascending(self, store, pg_conn):
        # ``user_daily_cost.day`` is TEXT (YYYY-MM-DD) in this schema.
        pg_conn.fetch.return_value = [
            {"day": "2025-06-13", "cost_usd": Decimal("1.00"), "requests": 1},
            {"day": "2025-06-14", "cost_usd": Decimal("2.00"), "requests": 2},
            {"day": "2025-06-15", "cost_usd": Decimal("3.00"), "requests": 3},
        ]

        points = await store.get_user_cost_history("u1", days=7)

        assert len(points) == 3
        days = [p["day"] for p in points]
        assert days == sorted(days)
        assert sum(Decimal(str(p["cost_usd"])) for p in points) == Decimal("6.00")
        # day field is ISO string YYYY-MM-DD
        assert points[0]["day"] == "2025-06-13"

    async def test_passes_user_id_and_days_to_query(self, store, pg_conn):
        await store.get_user_cost_history("u-abc", days=30)

        sql = pg_conn.fetch.call_args[0][0]
        assert "user_daily_cost" in sql
        assert "ORDER BY day ASC" in sql
        # Args: user_id, cutoff date string (YYYY-MM-DD)
        assert pg_conn.fetch.call_args[0][1] == "u-abc"
        cutoff = pg_conn.fetch.call_args[0][2]
        assert isinstance(cutoff, str)
        # Should be a valid YYYY-MM-DD date string
        assert len(cutoff) == 10 and cutoff[4] == "-" and cutoff[7] == "-"

    async def test_empty_for_unknown_user(self, store, pg_conn):
        pg_conn.fetch.return_value = []
        points = await store.get_user_cost_history("nope", days=7)
        assert points == []

    async def test_zero_days_returns_empty_without_query(self, store, pg_conn):
        points = await store.get_user_cost_history("u1", days=0)
        assert points == []
        pg_conn.fetch.assert_not_awaited()

    async def test_negative_days_returns_empty(self, store, pg_conn):
        points = await store.get_user_cost_history("u1", days=-3)
        assert points == []
        pg_conn.fetch.assert_not_awaited()


# ------------------------------------------------------------------
# get_bulk_user_cost_history
# ------------------------------------------------------------------


class TestGetBulkUserCostHistory:
    """Tests for get_bulk_user_cost_history (many users in one round-trip)."""

    async def test_groups_by_user(self, store, pg_conn):
        pg_conn.fetch.return_value = [
            {
                "user_id": "u1",
                "day": "2025-06-15",
                "cost_usd": Decimal("1"),
                "requests": 1,
            },
            {
                "user_id": "u2",
                "day": "2025-06-15",
                "cost_usd": Decimal("2"),
                "requests": 2,
            },
        ]

        out = await store.get_bulk_user_cost_history(["u1", "u2", "u3"], days=7)

        assert set(out.keys()) == {"u1", "u2", "u3"}
        assert len(out["u1"]) == 1
        assert len(out["u2"]) == 1
        assert out["u3"] == []
        assert out["u1"][0]["day"] == "2025-06-15"

    async def test_empty_user_ids_skips_query(self, store, pg_conn):
        out = await store.get_bulk_user_cost_history([], days=7)
        assert out == {}
        pg_conn.fetch.assert_not_awaited()

    async def test_zero_days_skips_query(self, store, pg_conn):
        out = await store.get_bulk_user_cost_history(["u1"], days=0)
        assert out == {}
        pg_conn.fetch.assert_not_awaited()

    async def test_uses_array_param(self, store, pg_conn):
        await store.get_bulk_user_cost_history(["u1", "u2"], days=7)
        sql = pg_conn.fetch.call_args[0][0]
        assert "ANY($1::text[])" in sql
        assert pg_conn.fetch.call_args[0][1] == ["u1", "u2"]
        cutoff = pg_conn.fetch.call_args[0][2]
        assert isinstance(cutoff, str) and len(cutoff) == 10


# ------------------------------------------------------------------
# get_users_summary (Task 3)
# ------------------------------------------------------------------


class TestGetUsersSummary:
    """Tests for get_users_summary."""

    async def test_anomaly_threshold_filters_correctly(self, store, pg_conn):
        # pending count + top
        pg_conn.fetchrow.return_value = {"c": 0}
        pg_conn.fetch.side_effect = [
            [],  # pending users
            # usage rows: user_a is anomalous (10x avg of 1, days >= 3),
            # user_b is below the $1 floor, user_c has < 3 days history.
            [
                {
                    "id": "user_a",
                    "email": "a@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("10.00"),
                    "prior_7d_total": Decimal("7.00"),
                    "days_with_history": 7,
                    "quota_daily_cost_usd": None,
                },
                {
                    "id": "user_b",
                    "email": "b@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("0.50"),
                    "prior_7d_total": Decimal("0.07"),
                    "days_with_history": 7,
                    "quota_daily_cost_usd": None,
                },
                {
                    "id": "user_c",
                    "email": "c@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("10.00"),
                    "prior_7d_total": Decimal("0.10"),
                    "days_with_history": 1,
                    "quota_daily_cost_usd": None,
                },
            ],
        ]

        summary = await store.get_users_summary()

        anomaly_ids = {u["id"] for u in summary["anomalies"]["top"]}
        assert "user_a" in anomaly_ids
        assert "user_b" not in anomaly_ids  # below $1 floor
        assert "user_c" not in anomaly_ids  # < 3 days history
        assert summary["anomalies"]["count"] >= 1

    async def test_pending_count_and_top_returned(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"c": 3}
        pg_conn.fetch.side_effect = [
            [
                {
                    "id": "p1",
                    "email": "p1@x.com",
                    "user_name": None,
                    "role": "free",
                    "created_at": None,
                },
                {
                    "id": "p2",
                    "email": "p2@x.com",
                    "user_name": None,
                    "role": "free",
                    "created_at": None,
                },
            ],
            [],  # usage rows
        ]

        summary = await store.get_users_summary()

        assert summary["pending"]["count"] == 3
        ids = {u["id"] for u in summary["pending"]["top"]}
        assert ids == {"p1", "p2"}

    async def test_top_spenders_sorted_desc(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"c": 0}
        pg_conn.fetch.side_effect = [
            [],
            [
                {
                    "id": "small",
                    "email": "s@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("1.00"),
                    "prior_7d_total": Decimal("0"),
                    "days_with_history": 0,
                    "quota_daily_cost_usd": None,
                },
                {
                    "id": "big",
                    "email": "b@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("100.00"),
                    "prior_7d_total": Decimal("0"),
                    "days_with_history": 0,
                    "quota_daily_cost_usd": None,
                },
            ],
        ]

        summary = await store.get_users_summary()

        top_ids = [u["id"] for u in summary["top_spenders_today"]["top"]]
        assert top_ids[0] == "big"
        assert summary["top_spenders_today"]["count"] == 2

    async def test_near_quota_threshold(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"c": 0}
        pg_conn.fetch.side_effect = [
            [],
            [
                {
                    "id": "near_q",
                    "email": "n@x.com",
                    "user_name": None,
                    "role": "free",
                    # 80% of $10 quota -> just at near threshold
                    "today_cost": Decimal("8.00"),
                    "prior_7d_total": Decimal("0"),
                    "days_with_history": 0,
                    "quota_daily_cost_usd": Decimal("10.00"),
                },
                {
                    "id": "far_q",
                    "email": "f@x.com",
                    "user_name": None,
                    "role": "free",
                    "today_cost": Decimal("1.00"),
                    "prior_7d_total": Decimal("0"),
                    "days_with_history": 0,
                    "quota_daily_cost_usd": Decimal("10.00"),
                },
            ],
        ]

        summary = await store.get_users_summary()

        ids = {u["id"] for u in summary["near_quota"]["top"]}
        assert "near_q" in ids
        assert "far_q" not in ids


# ------------------------------------------------------------------
# list_users new filters (Task 4)
# ------------------------------------------------------------------


class TestListUsersNewFilters:
    """Tests for the new keyword-only filters on list_users."""

    async def test_accepts_min_cost_today_kwarg(self, store, pg_conn):
        # count + status counts + main rows
        pg_conn.fetchrow.return_value = {"total": 0}
        pg_conn.fetch.side_effect = [[], []]

        # Should not raise
        _total, rows, _ = await store.list_users(min_cost_today=Decimal("10"))
        assert rows == []

    async def test_accepts_quota_state_kwarg(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"total": 0}
        pg_conn.fetch.side_effect = [[], []]

        _total, rows, _ = await store.list_users(quota_state="custom")
        assert rows == []

    async def test_accepts_provider_kwarg(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"total": 0}
        pg_conn.fetch.side_effect = [[], []]

        _total, rows, _ = await store.list_users(provider="anthropic")
        assert rows == []

    async def test_accepts_active_within_hours_kwarg(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"total": 0}
        pg_conn.fetch.side_effect = [[], []]

        _total, rows, _ = await store.list_users(active_within_hours=24)
        assert rows == []

    async def test_search_now_includes_id_and_key_prefix(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {"total": 0}
        pg_conn.fetch.side_effect = [[], []]

        await store.list_users(search="sk-test-1")

        # Find the count query and verify it contains key_prefix and id::text
        all_sqls = [c.args[0] for c in pg_conn.fetch.call_args_list] + [
            c.args[0] for c in pg_conn.fetchrow.call_args_list
        ]
        joined = " ".join(all_sqls)
        assert "key_prefix" in joined
        assert "u.id::text" in joined

    async def test_min_cost_today_filtered_in_sql(self, store, pg_conn):
        # With min_cost_today applied in SQL via a correlated subquery on
        # api_logs, the row query already returns only matching rows. We
        # verify the SQL contains the cost filter and that ``total`` reflects
        # the filtered count.
        pg_conn.fetchrow.return_value = {"total": 1}
        pg_conn.fetch.side_effect = [
            [{"status": "active", "cnt": 2}],  # status counts
            [
                {
                    "id": "expensive",
                    "email": "e@x.com",
                    "user_name": None,
                    "role": "free",
                    "status": "active",
                    "email_verified": True,
                    "approval_note": None,
                    "reviewed_at": None,
                    "reviewed_by": None,
                    "created_at": None,
                    "last_login_at": None,
                    "key_prefix": "hyi-e",
                    "key_status": "active",
                    "usage_today": Decimal("100.00"),
                },
            ],
            # month's costs lookup (post-fetch enrichment)
            [],
            # alltime costs lookup (post-fetch enrichment)
            [],
        ]

        total, rows, _ = await store.list_users(min_cost_today=Decimal("10"))

        ids = {r["id"] for r in rows}
        assert ids == {"expensive"}
        assert total == 1
        # Verify the SQL includes the today cost threshold check.
        all_sqls = [c.args[0] for c in pg_conn.fetch.call_args_list] + [
            c.args[0] for c in pg_conn.fetchrow.call_args_list
        ]
        joined = " ".join(all_sqls)
        assert "date_trunc('day'" in joined
        assert "SUM(cost_usd)" in joined

    async def test_alltime_usage_enriched_from_api_logs_without_cost_sort(self, store, pg_conn):
        # Regression: default sorting does not include the all-time CTE, but
        # the admin dashboard still needs historical usage from api_logs.
        pg_conn.fetchrow.return_value = {"total": 1}
        pg_conn.fetch.side_effect = [
            [{"status": "active", "cnt": 1}],  # status counts
            [
                {
                    "id": "historical",
                    "email": "h@x.com",
                    "user_name": None,
                    "role": "free",
                    "status": "active",
                    "email_verified": True,
                    "approval_note": None,
                    "reviewed_at": None,
                    "reviewed_by": None,
                    "created_at": None,
                    "last_login_at": None,
                    "key_prefix": None,
                    "key_status": None,
                },
            ],
            [],  # today's costs lookup
            [],  # month's costs lookup
            [{"user_id": "historical", "cost": Decimal("12.34")}],
        ]

        _total, rows, _ = await store.list_users()

        assert rows[0]["usage_alltime"] == Decimal("12.34")
        alltime_sql = pg_conn.fetch.call_args_list[-1].args[0]
        assert "FROM api_logs" in alltime_sql
        assert "FROM user_daily_cost" not in alltime_sql
