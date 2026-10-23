"""Native CNY and legacy USD coexist without changing historical evidence."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from ops.accounting_currency import migrate_currency
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.storage.accounting_schema import initialize_accounting_schema
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]
POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "test-fixture")


@asynccontextmanager
async def accounting_db():
    schema = f"accounting_{uuid.uuid4().hex}"
    conn = await postgres.connect()
    pool = None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(
            "CREATE TABLE site_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "value_type TEXT NOT NULL, updated_at TIMESTAMPTZ, updated_by TEXT); "
            "CREATE TABLE api_logs (id SERIAL PRIMARY KEY, cost_usd NUMERIC(12,8), upstream_cost_usd NUMERIC(12,8)); "
            "CREATE TABLE api_keys (id SERIAL PRIMARY KEY, quota_daily_cost_usd NUMERIC(10,4), quota_monthly_cost_usd NUMERIC(10,4)); "
            "CREATE TABLE user_daily_cost (user_id TEXT, day TEXT, cost_usd NUMERIC(12,6), "
            "requests INT, last_request_at TIMESTAMPTZ, PRIMARY KEY(user_id,day))"
        )
        yield conn, schema
    finally:
        bind_accounting_policy(None)
        if pool is not None:
            await pool.close()
        await conn.execute("SET search_path TO public")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


@pytest.mark.asyncio
async def test_migration_preserves_usd_and_unknown_cost_and_is_idempotent(monkeypatch):
    async with accounting_db() as (conn, _):
        await conn.execute(
            "INSERT INTO api_logs(cost_usd,upstream_cost_usd) VALUES (1.25,0.8),(NULL,NULL)"
        )
        await conn.execute(
            "INSERT INTO api_keys(quota_daily_cost_usd,quota_monthly_cost_usd) VALUES (100,NULL)"
        )
        preview = await migrate_currency(conn, POLICY)
        assert not preview["applied"]
        assert (
            await conn.fetchval("SELECT value FROM site_settings WHERE key='accounting:policy'")
            is None
        )

        # Other xdist tests may use this worker's database; this isolated schema
        # exercises the transaction while the separate test checks the gate.
        async def offline(_conn):
            return None

        monkeypatch.setattr("ops.accounting_currency._require_offline", offline)
        result = await migrate_currency(conn, POLICY, apply=True)
        assert result["applied"] and result["migration_complete"]
        assert (
            json.loads(
                await conn.fetchval(
                    "SELECT value FROM site_settings WHERE key='accounting:migration'"
                )
            )
            == POLICY.as_dict()
        )
        rows = await conn.fetch("SELECT * FROM api_logs ORDER BY id")
        assert rows[0]["cost_usd"] == Decimal("1.25")
        assert rows[0]["upstream_cost_usd"] == Decimal("0.8")
        assert rows[0]["cost_amount"] == Decimal("8.3975")
        assert rows[1]["cost_amount"] is None
        key = await conn.fetchrow("SELECT * FROM api_keys")
        assert key["quota_daily_cost_amount"] == Decimal("671.8")
        assert key["quota_monthly_cost_amount"] is None
        again = await migrate_currency(conn, POLICY, apply=True)
        assert again["already_applied"] and not again["applied"]
        assert await conn.fetchval("SELECT count(*) FROM accounting_currency_migrations") == 1


@pytest.mark.asyncio
async def test_native_small_cost_and_exact_quota_survive_usd_rounding():
    async with accounting_db() as (conn, _):
        await initialize_accounting_schema(conn)
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        await conn.execute(
            "INSERT INTO api_logs(cost_amount,accounting_currency) VALUES (0.00000001,'CNY')"
        )
        row = await conn.fetchrow("SELECT * FROM api_logs")
        assert row["cost_amount"] == Decimal("0.00000001")
        assert row["cost_usd"] == Decimal("0")
        await conn.execute(
            "INSERT INTO api_keys(quota_daily_cost_amount,accounting_currency) VALUES (10,'CNY')"
        )
        row = await conn.fetchrow("SELECT * FROM api_keys")
        assert row["quota_daily_cost_amount"] == Decimal("10")
        assert row["quota_daily_cost_usd"] == Decimal("1.4885")
        await conn.execute("UPDATE api_keys SET quota_daily_cost_usd=2")
        assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(
            "13.436"
        )


@pytest.mark.asyncio
async def test_native_counter_increment_keeps_primary_precision():
    async with accounting_db() as (conn, schema):
        await initialize_accounting_schema(conn)
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        bind_accounting_policy(POLICY)
        pool = await postgres.create_pool(
            min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        try:
            store = PostgresOperationalStore(pool)
            for _ in range(3):
                await store.increment_user_cost_amount("user", Decimal("0.00000001"))
            assert await store.get_user_cost_today_amount("user") == Decimal("0.00000003")
            await store.increment_user_cost("user", 1)
            assert await store.get_user_cost_today_amount("user") == Decimal("6.71800003")
            assert await conn.fetchval("SELECT requests FROM user_daily_cost") == 4
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_apply_refuses_connected_database_clients():
    from ops.accounting_currency import _require_offline

    class Connection:
        async def fetchval(self, sql):
            return 1

    with pytest.raises(RuntimeError, match="other database clients remain"):
        await _require_offline(Connection())


@pytest.mark.asyncio
async def test_currency_schema_cannot_be_deferred_behind_a_reader(monkeypatch):
    from serving.storage import log_schema
    from serving.storage.accounting_schema import AccountingSchemaUnavailable

    monkeypatch.setattr(log_schema, "_DDL_LOCK_TIMEOUT", "100ms")
    async with accounting_db() as (conn, schema):
        reader = await postgres.connect(server_settings={"search_path": schema})
        try:
            async with reader.transaction():
                await reader.execute("LOCK TABLE api_keys IN ACCESS SHARE MODE")
                with pytest.raises(AccountingSchemaUnavailable):
                    await initialize_accounting_schema(conn, tables=("api_keys",))
            await initialize_accounting_schema(conn, tables=("api_keys",))
        finally:
            await reader.close()


@pytest.mark.asyncio
async def test_rollup_keeps_the_sum_of_legacy_usd_rows():
    async with accounting_db() as (conn, _):
        await conn.execute(
            "CREATE TABLE provider_hourly_stats(id INT, total_cost_usd NUMERIC(14,8))"
        )
        await initialize_accounting_schema(conn)
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        # Three separately rounded request costs do not necessarily equal the
        # converted native sum. Preserve both sums for their respective APIs.
        await conn.execute(
            "INSERT INTO provider_hourly_stats(id,total_cost_usd,total_cost_amount) VALUES(1,0,0.00000003)"
        )
        row = await conn.fetchrow("SELECT * FROM provider_hourly_stats")
        assert row["total_cost_usd"] == Decimal("0")
        assert row["total_cost_amount"] == Decimal("0.00000003")


@pytest.mark.asyncio
async def test_role_quota_one_cny_and_batch_native_usage_keep_precision():
    from serving.storage.postgres_log import PostgresLogStore

    async with accounting_db() as (conn, schema):
        await conn.execute(
            "CREATE TABLE users(id TEXT, role TEXT); "
            "ALTER TABLE api_keys ADD COLUMN user_id TEXT, ADD COLUMN status TEXT; "
            "ALTER TABLE api_logs ADD COLUMN user_id TEXT, ADD COLUMN timestamp TIMESTAMPTZ DEFAULT NOW(); "
            "INSERT INTO users VALUES ('user','free'); "
            "INSERT INTO api_keys(user_id,status,quota_daily_cost_usd) VALUES('user','active',100)"
        )
        await initialize_accounting_schema(conn)
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        bind_accounting_policy(POLICY)
        pool = await postgres.create_pool(
            min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        try:
            store = PostgresOperationalStore(pool)
            assert await store.apply_role_quota("free", POLICY.to_usd(1)) == 1
            assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(1)
            assert await conn.fetchval("SELECT quota_daily_cost_usd FROM api_keys") == Decimal(
                "0.1489"
            )
            await store.update_key("user", quota_daily_cost_usd=POLICY.to_usd(2))
            assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(2)
            for _ in range(3):
                await store.increment_user_cost_amount("user", Decimal("0.00000001"))
                await conn.execute(
                    "INSERT INTO api_logs(user_id,cost_amount) VALUES ('user',0.00000001)"
                )
            logs = PostgresLogStore(pool)
            for period in ("today", "month"):
                assert await store.get_batch_usage_amount(["user"], period) == {
                    "user": Decimal("0.00000003")
                }
                assert await logs.get_batch_usage_amount(["user"], period) == {
                    "user": Decimal("0.00000003")
                }
                assert await logs.get_batch_usage(["user"], period) == {"user": 0.0}
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_partial_manual_policy_cannot_appear_as_completed_migration(monkeypatch):
    async with accounting_db() as (conn, _):
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        preview = await migrate_currency(conn, POLICY)
        assert not preview["already_applied"]
        assert not preview["migration_complete"]

        async def offline(_conn):
            return None

        monkeypatch.setattr("ops.accounting_currency._require_offline", offline)
        with pytest.raises(ValueError, match="no matching completed migration"):
            await migrate_currency(conn, POLICY, apply=True)


@pytest.mark.asyncio
async def test_native_user_filters_sort_and_summary_keep_sub_usd_costs():
    from serving.storage.log_schema import ensure_api_logs_schema

    async with accounting_db() as (conn, schema):
        await conn.execute(
            "CREATE TABLE users (LIKE public.users INCLUDING ALL); "
            "DROP TABLE api_keys; CREATE TABLE api_keys (LIKE public.api_keys INCLUDING ALL); "
            "DROP TABLE api_logs"
        )
        await ensure_api_logs_schema(conn)
        await initialize_accounting_schema(conn)
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        await conn.execute(
            "INSERT INTO users(id,email,password_hash,status,role,created_at) VALUES "
            "('high','high@example.test','fixture','active','free','2026-01-01'), "
            "('low','low@example.test','fixture','active','free','2026-01-02'), "
            "('zero','zero@example.test','fixture','active','free','2026-01-03')"
        )
        bind_accounting_policy(POLICY)
        pool = await postgres.create_pool(
            min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        try:
            store = PostgresOperationalStore(pool)
            for uid, amount, quota in (
                ("high", "0.00000002", "0.00000002"),
                ("low", "0.00000001", "1"),
            ):
                await store.create_key(
                    key_hash=uid,
                    key_prefix=uid,
                    user_id=uid,
                    quota_daily_cost_usd=POLICY.to_usd(quota),
                    quota_daily_cost_amount=Decimal(quota),
                )
                await store.increment_user_cost_amount(uid, Decimal(amount))
                await conn.execute(
                    "INSERT INTO api_logs(request_id,model_id,provider,user_id,cost_amount) "
                    "VALUES ($1,'fixture','fixture',$1,$2)",
                    uid,
                    Decimal(amount),
                )
            for sort_by in ("cost_today", "cost_month", "cost_alltime"):
                total, rows, _ = await store.list_users(
                    sort_by=sort_by, limit=1, include_usage_totals=False
                )
                assert total == 3
                assert [row["id"] for row in rows] == ["high"]
                assert rows[0]["usage_today_amount"] == Decimal("0.00000002")
                assert rows[0]["usage_today"] == 0
            for field in ("min_cost_today", "min_cost_month"):
                total, rows, _ = await store.list_users(
                    **{field: POLICY.to_usd("0.000000015")}, include_usage_totals=False
                )
                assert total == 1
                assert [row["id"] for row in rows] == ["high"]
            for quota_state in ("near", "over"):
                total, rows, _ = await store.list_users(
                    quota_state=quota_state, include_usage_totals=False
                )
                assert total == 1
                assert [row["id"] for row in rows] == ["high"]
            summary = await store.get_users_summary()
            assert summary["top_spenders_today"]["count"] == 2
            assert summary["top_spenders_today"]["top"][0]["id"] == "high"
            assert summary["near_quota"]["count"] == 1
            assert summary["near_quota"]["top"][0]["today_cost_amount"] == Decimal("0.00000002")
            # Alert selection must agree at the native optimistic-charge boundary.
            await store.update_key("high", quota_daily_cost_amount=Decimal("1"))
            await conn.execute(
                "UPDATE user_daily_cost SET cost_amount=0.93282 WHERE user_id='high'"
            )
            assert await store.query_users_at_daily_quota() == []
            await store.increment_user_cost_amount("high", Decimal("0.00000001"))
            exhausted = await store.query_users_at_daily_quota()
            assert [row[0] for row in exhausted] == ["high"]
            assert exhausted[0][3] == 0.1489  # Legacy return value remains USD.

        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_migration_preview_matches_each_stored_row_and_repeated_native_totals(monkeypatch):
    async with accounting_db() as (conn, _):
        await conn.execute(
            "CREATE TABLE routewise_probe_samples(id SERIAL, cost_usd DOUBLE PRECISION); "
            "INSERT INTO routewise_probe_samples(cost_usd) VALUES (0.0000000000001),(0.0000000000001)"
        )
        preview = await migrate_currency(conn, POLICY)
        expected = Decimal(
            preview["tables"]["routewise_probe_samples"]["cost_amount"]["native_total"]
        )
        assert expected == Decimal("0.000000000002")

        async def offline(_conn):
            return None

        monkeypatch.setattr("ops.accounting_currency._require_offline", offline)
        await migrate_currency(conn, POLICY, apply=True)
        assert (
            await conn.fetchval("SELECT SUM(cost_amount) FROM routewise_probe_samples") == expected
        )
        assert (
            await conn.fetchval("SELECT SUM(cost_usd) FROM routewise_probe_samples")
            == 0.0000000000002
        )
        await conn.execute("INSERT INTO api_logs(cost_amount) VALUES(0.00000001)")
        repeated = await migrate_currency(conn, POLICY)
        assert Decimal(repeated["tables"]["api_logs"]["cost_amount"]["native_total"]) == Decimal(
            "0.00000001"
        )
        assert Decimal(repeated["tables"]["api_logs"]["cost_amount"]["original_usd_total"]) == 0
