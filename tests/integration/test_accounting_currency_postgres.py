"""Native CNY and legacy USD coexist without changing historical evidence.

Every test runs on a scratch database the schema migrations built, exactly as a
deploy builds one (``python -m migrations upgrade``): the native columns, their
triggers and the migration audit table come from ``0022_ledger_currency``,
never from the service or the offline currency migration.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from migrations import LOCK_NOT_AVAILABLE, sqlstate
from ops.accounting_currency import migrate_currency
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.storage.accounting_schema import (
    AccountingSchemaUnavailable,
    accounting_schema_problems,
    require_accounting_schema,
)
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]
POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "test-fixture")


@asynccontextmanager
async def accounting_db(revision: str = "head"):
    """A scratch database migrated to *revision*, and a connection to it."""
    async with mig.scratch_database() as name:
        await mig.upgrade(name, revision)
        conn = await postgres.connect(database=name)
        try:
            yield name, conn
        finally:
            bind_accounting_policy(None)
            await conn.close()


@asynccontextmanager
async def store_pool(name: str):
    pool = await postgres.create_pool(database=name, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


async def _persist_policy(conn, policy: AccountingPolicy = POLICY) -> None:
    """The persisted CNY policy and its completion marker, as the migration leaves them."""
    for key in ("accounting:policy", "accounting:migration"):
        await conn.execute(
            "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
            "VALUES ($1, $2, 'json', NOW(), 'test')",
            key,
            json.dumps(policy.as_dict(), sort_keys=True),
        )


def _offline(monkeypatch) -> None:
    # The scratch database is this test's alone, but its own pool counts as a
    # client; the refusal itself is checked separately below.
    async def offline(_conn):
        return None

    monkeypatch.setattr("ops.accounting_currency._require_offline", offline)


async def _key(conn, prefix: str, **columns) -> None:
    names = ", ".join(["key_hash", "key_prefix", "user_id", "account_id", *columns])
    values = ", ".join(f"${i}" for i in range(1, len(columns) + 5))
    await conn.execute(
        f"INSERT INTO api_keys ({names}) VALUES ({values})",
        prefix,
        prefix,
        prefix,
        prefix,
        *columns.values(),
    )


async def test_migration_preserves_usd_and_unknown_cost_and_is_idempotent(monkeypatch):
    async with accounting_db() as (_name, conn):
        await conn.execute(
            "INSERT INTO api_logs(request_id, model_id, provider, cost_usd, upstream_cost_usd) "
            "VALUES ('r1', 'm', 'p', 1.25, 0.8), ('r2', 'm', 'p', NULL, NULL)"
        )
        await _key(
            conn,
            "k1",
            quota_daily_cost_usd=Decimal(100),
            budget_daily_usd=Decimal(5),
        )
        key_id = await conn.fetchval("SELECT id FROM api_keys WHERE key_prefix = 'k1'")
        await conn.execute(
            "INSERT INTO api_key_daily_cost (key_id, day, user_id, cost_usd, requests) "
            "VALUES ($1, '2026-10-23', 'k1', 0.5, 1)",
            key_id,
        )
        # Under the USD default the trigger keeps every native amount equal to its USD value.
        assert await conn.fetchval("SELECT cost_amount FROM api_logs WHERE request_id='r1'") == (
            Decimal("1.25")
        )
        preview = await migrate_currency(conn, POLICY)
        assert not preview["applied"]
        assert Decimal(preview["tables"]["api_keys"]["budget_daily_amount"]["native_total"]) == (
            Decimal("33.59")
        )
        assert (
            await conn.fetchval("SELECT value FROM site_settings WHERE key='accounting:policy'")
            is None
        )

        _offline(monkeypatch)
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
        assert rows[0]["accounting_currency"] == "CNY"
        assert rows[1]["cost_amount"] is None
        key = await conn.fetchrow("SELECT * FROM api_keys")
        assert key["quota_daily_cost_amount"] == Decimal("671.8")
        assert key["quota_monthly_cost_amount"] is None
        # The key budgets and the per-key counter are ledger amounts too: they
        # convert with everything else, or a CNY ledger would read USD as CNY.
        assert key["budget_daily_usd"] == Decimal(5)
        assert key["budget_daily_amount"] == Decimal("33.59")
        assert key["budget_monthly_amount"] is None
        counter = await conn.fetchrow("SELECT cost_usd, cost_amount FROM api_key_daily_cost")
        assert counter["cost_usd"] == Decimal("0.5")
        assert counter["cost_amount"] == Decimal("3.359")
        again = await migrate_currency(conn, POLICY, apply=True)
        assert again["already_applied"] and not again["applied"]
        assert await conn.fetchval("SELECT count(*) FROM accounting_currency_migrations") == 1
        # Repeating the completed policy re-converts nothing.
        assert await conn.fetchval("SELECT cost_amount FROM api_logs WHERE request_id='r1'") == (
            Decimal("8.3975")
        )


async def test_apply_refuses_a_schema_behind_this_release(monkeypatch):
    async with accounting_db("0002_api_key_limits") as (_name, conn):
        _offline(monkeypatch)
        with pytest.raises(RuntimeError, match="not at this release's head"):
            await migrate_currency(conn, POLICY, apply=True)
        assert await conn.fetchval("SELECT to_regclass('accounting_currency_migrations')") is None


async def test_native_small_cost_and_exact_quota_survive_usd_rounding():
    async with accounting_db() as (_name, conn):
        await _persist_policy(conn)
        await conn.execute(
            "INSERT INTO api_logs(request_id, model_id, provider, cost_amount, "
            "accounting_currency) VALUES ('r', 'm', 'p', 0.00000001, 'CNY')"
        )
        row = await conn.fetchrow("SELECT * FROM api_logs")
        assert row["cost_amount"] == Decimal("0.00000001")
        assert row["cost_usd"] == Decimal("0")
        # Native only: the USD column's default is overridden with NULL, so the
        # trigger derives it rather than comparing it with a default of $1000.
        await _key(
            conn,
            "k",
            quota_daily_cost_usd=None,
            quota_daily_cost_amount=Decimal(10),
            accounting_currency="CNY",
        )
        row = await conn.fetchrow("SELECT * FROM api_keys")
        assert row["quota_daily_cost_amount"] == Decimal("10")
        assert row["quota_daily_cost_usd"] == Decimal("1.4885")
        await conn.execute("UPDATE api_keys SET quota_daily_cost_usd=2")
        assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(
            "13.436"
        )
        # A native budget derives its USD equivalent at the column's scale.
        await conn.execute("UPDATE api_keys SET budget_daily_amount = 50")
        row = await conn.fetchrow("SELECT budget_daily_usd, budget_daily_amount FROM api_keys")
        assert row["budget_daily_amount"] == Decimal(50)
        assert row["budget_daily_usd"] == Decimal("7.4427")
        # A mismatched currency tag is refused outright.
        with pytest.raises(Exception, match="does not match persisted policy"):
            await conn.execute(
                "INSERT INTO api_logs(request_id, model_id, provider, cost_amount, "
                "accounting_currency) VALUES ('usd', 'm', 'p', 1, 'USD')"
            )


async def test_native_counter_increment_keeps_primary_precision():
    async with accounting_db() as (name, conn):
        await _persist_policy(conn)
        bind_accounting_policy(POLICY)
        async with store_pool(name) as pool:
            store = PostgresOperationalStore(pool)
            for _ in range(3):
                await store.increment_user_cost_amount("user", Decimal("0.00000001"))
            assert await store.get_user_cost_today_amount("user") == Decimal("0.00000003")
            await store.increment_user_cost("user", 1)
            assert await store.get_user_cost_today_amount("user") == Decimal("6.71800003")
            assert await conn.fetchval("SELECT requests FROM user_daily_cost") == 4


async def test_per_key_counter_is_native_and_derives_its_usd_equivalent():
    """The key's counter takes the native charge; its rounded USD column never disagrees."""
    async with accounting_db() as (name, conn):
        await _persist_policy(conn)
        bind_accounting_policy(POLICY)
        # A key belongs to its account's Default project, so the account exists.
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status, role) "
            "VALUES ('u', 'u@example.test', 'x', 'active', 'free')"
        )
        async with store_pool(name) as pool:
            store = PostgresOperationalStore(pool)
            key = await store.create_key(key_hash="h", key_prefix="hyi-k", user_id="u")
            # Amounts whose USD equivalents do not fit numeric(12,6) exactly.
            for amount in ("0.0000001", "1.2345678", "0.33"):
                await store.increment_user_cost_amount("u", Decimal(amount), api_key_id=key["id"])
            row = await conn.fetchrow(
                "SELECT cost_usd, cost_amount, accounting_currency, requests "
                "FROM api_key_daily_cost WHERE key_id = $1",
                key["id"],
            )
            assert row["cost_amount"] == Decimal("1.5645679")
            assert row["cost_usd"] == (Decimal("1.5645679") / POLICY.usd_to_currency_rate).quantize(
                Decimal("0.000001")
            )
            assert row["accounting_currency"] == "CNY"
            assert row["requests"] == 3
            assert await store.get_key_spend_amount(key["id"]) == (
                Decimal("1.5645679"),
                Decimal("1.5645679"),
            )
            # The account counter is bumped exactly as without a key.
            assert await store.get_user_cost_today_amount("u") == Decimal("1.5645679")


async def test_key_budgets_and_spend_reach_the_gate_lookup_natively():
    async with accounting_db() as (name, conn):
        await _persist_policy(conn)
        bind_accounting_policy(POLICY)
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status, role) "
            "VALUES ('u', 'u@example.test', 'x', 'active', 'free')"
        )
        async with store_pool(name) as pool:
            store = PostgresOperationalStore(pool)
            key = await store.create_key(
                key_hash="h",
                key_prefix="hyi-k",
                user_id="u",
                quota_daily_cost_usd=POLICY.to_usd(100),
                quota_daily_cost_amount=Decimal(100),
                quota_monthly_cost_usd=POLICY.to_usd(1000),
                quota_monthly_cost_amount=Decimal(1000),
            )
            changed = await store.update_key_settings(
                "u",
                "hyi-k",
                {
                    "budget_daily_usd": POLICY.to_usd(5).quantize(Decimal("0.0001")),
                    "budget_daily_amount": Decimal(5),
                },
            )
            assert changed is not None
            before, after = changed
            assert before["budget_daily_amount"] is None
            assert after["budget_daily_amount"] == Decimal(5)
            assert after["budget_daily_usd"] == Decimal("0.7443")
            await store.increment_user_cost_amount("u", Decimal("4.95"), api_key_id=key["id"])

            joined = await store.get_auth_context_with_spend("h")
            assert joined["quota_daily_cost_amount"] == Decimal(100)
            assert joined["quota_monthly_cost_amount"] == Decimal(1000)
            assert joined["budget_daily_amount"] == Decimal(5)
            assert joined["key_spent_today_amount"] == Decimal("4.95")
            assert joined["key_spent_month_amount"] == Decimal("4.95")
            assert joined["account_spent_month_amount"] == Decimal("4.95")
            assert joined["cost_amount"] == Decimal("4.95")
            listed = (await store.list_account_keys("u"))[0]
            assert listed["budget_daily_amount"] == Decimal(5)
            assert listed["spent_today_amount"] == Decimal("4.95")

            # A second key takes the account's native quota exactly.
            await store.create_key(
                key_hash="h2", key_prefix="hyi-k2", user_id="u", inherit_account_quota=True
            )
            second = await store.get_auth_context_by_key_hash("h2")
            assert second["quota_daily_cost_amount"] == Decimal(100)
            assert second["quota_monthly_cost_amount"] == Decimal(1000)
            # An account-wide quota change reaches every key with its native amount.
            await store.update_key("u", quota_daily_cost_usd=POLICY.to_usd(7))
            amounts = await conn.fetch("SELECT quota_daily_cost_amount FROM api_keys ORDER BY id")
            assert [row["quota_daily_cost_amount"] for row in amounts] == [Decimal(7)] * 2
            # A key created with a new account quota gives it to the account's
            # other keys exactly, not through its four-place USD equivalent.
            exact = Decimal("7.123456789012")
            await store.create_key(
                key_hash="h3",
                key_prefix="hyi-k3",
                user_id="u",
                quota_daily_cost_usd=POLICY.to_usd(exact),
                quota_daily_cost_amount=exact,
            )
            amounts = await conn.fetch(
                "SELECT quota_daily_cost_amount, quota_daily_cost_usd FROM api_keys ORDER BY id"
            )
            assert [row["quota_daily_cost_amount"] for row in amounts] == [exact] * 3
            assert {row["quota_daily_cost_usd"] for row in amounts} == {Decimal("1.0604")}


async def test_apply_refuses_connected_database_clients():
    from ops.accounting_currency import _require_offline

    class Connection:
        async def fetchval(self, sql):
            return 1

    with pytest.raises(RuntimeError, match="other database clients remain"):
        await _require_offline(Connection())


async def test_the_revision_waits_for_a_reader_then_finishes_on_the_rerun():
    """``0022`` under a held table lock fails without recording itself; the rerun completes."""
    async with accounting_db("0021_sso_identities") as (name, conn):
        reader = await postgres.connect(database=name)
        try:
            async with reader.transaction():
                await reader.execute("LOCK TABLE api_keys IN ACCESS SHARE MODE")
                with pytest.raises(Exception) as caught:
                    await mig.upgrade(name, "0022_ledger_currency")
                assert sqlstate(caught.value) == LOCK_NOT_AVAILABLE
                assert (
                    await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version")
                    == "0021_sso_identities"
                )
        finally:
            await reader.close()
        await mig.upgrade(name)
        assert await accounting_schema_problems(conn) == []


async def test_startup_refuses_a_ledger_without_its_native_schema():
    async with accounting_db() as (_name, conn):
        await require_accounting_schema(conn)
        await conn.execute(
            "ALTER TABLE api_keys DISABLE TRIGGER api_keys_accounting_budget_amounts"
        )
        await conn.execute(
            "DROP TRIGGER api_key_daily_cost_accounting_amounts ON api_key_daily_cost"
        )
        await conn.execute(
            "CREATE OR REPLACE FUNCTION accounting_sync_amounts() RETURNS trigger AS $$ "
            "BEGIN RETURN NEW; END $$ LANGUAGE plpgsql"
        )
        with pytest.raises(AccountingSchemaUnavailable) as refused:
            await require_accounting_schema(conn)
        message = str(refused.value)
        assert "accounting_sync_amounts is not the reviewed one" in message
        assert "trigger api_keys_accounting_budget_amounts is disabled" in message
        assert "trigger api_key_daily_cost_accounting_amounts is missing" in message
        assert "python -m migrations upgrade" in message


async def test_rollup_keeps_the_sum_of_legacy_usd_rows():
    async with accounting_db() as (_name, conn):
        await _persist_policy(conn)
        # Three separately rounded request costs do not necessarily equal the
        # converted native sum. Preserve both sums for their respective APIs.
        await conn.execute(
            "INSERT INTO provider_hourly_stats (provider, model_id, hour_bucket, request_count, "
            "error_count, stream_count, total_completion_tokens, total_cost_usd, "
            "total_cost_amount) VALUES ('p', 'm', NOW(), 0, 0, 0, 0, 0, 0.00000003)"
        )
        row = await conn.fetchrow("SELECT * FROM provider_hourly_stats")
        assert row["total_cost_usd"] == Decimal("0")
        assert row["total_cost_amount"] == Decimal("0.00000003")


async def test_role_quota_one_cny_and_batch_native_usage_keep_precision():
    async with accounting_db() as (name, conn):
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status, role) "
            "VALUES ('user', 'user@example.test', 'x', 'active', 'free')"
        )
        # Rows on a CNY ledger are written after its policy; the trigger refuses
        # to update a row still tagged with another currency.
        await _persist_policy(conn)
        await _key(conn, "user", quota_daily_cost_usd=Decimal(100))
        bind_accounting_policy(POLICY)
        async with store_pool(name) as pool:
            store = PostgresOperationalStore(pool)
            assert await store.apply_role_quota("free", POLICY.to_usd(1)) == 1
            assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(1)
            assert await conn.fetchval("SELECT quota_daily_cost_usd FROM api_keys") == Decimal(
                "0.1489"
            )
            await store.update_key("user", quota_daily_cost_usd=POLICY.to_usd(2))
            assert await conn.fetchval("SELECT quota_daily_cost_amount FROM api_keys") == Decimal(2)
            for index in range(3):
                await store.increment_user_cost_amount("user", Decimal("0.00000001"))
                await conn.execute(
                    "INSERT INTO api_logs (request_id, model_id, provider, user_id, cost_amount) "
                    "VALUES ($1, 'm', 'p', 'user', 0.00000001)",
                    f"r{index}",
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
            # The dashboard's windowed usage carries the exact native amount.
            detail = await logs.get_user_usage_detail("user")
            assert detail["today"]["cost_amount"] == Decimal("0.00000003")
            assert detail["alltime"]["cost_amount"] == Decimal("0.00000003")
            assert detail["today"]["cost_usd"] == 0.0


async def test_partial_manual_policy_cannot_appear_as_completed_migration(monkeypatch):
    async with accounting_db() as (_name, conn):
        await conn.execute(
            "INSERT INTO site_settings VALUES ('accounting:policy',$1,'json',NOW(),'test')",
            json.dumps(POLICY.as_dict()),
        )
        preview = await migrate_currency(conn, POLICY)
        assert not preview["already_applied"]
        assert not preview["migration_complete"]

        _offline(monkeypatch)
        with pytest.raises(ValueError, match="no matching completed migration"):
            await migrate_currency(conn, POLICY, apply=True)


async def test_native_user_filters_sort_and_summary_keep_sub_usd_costs():
    async with accounting_db() as (name, conn):
        await _persist_policy(conn)
        await conn.execute(
            "INSERT INTO users(id,email,password_hash,status,role,created_at) VALUES "
            "('high','high@example.test','fixture','active','free','2026-01-01'), "
            "('low','low@example.test','fixture','active','free','2026-01-02'), "
            "('zero','zero@example.test','fixture','active','free','2026-01-03')"
        )
        bind_accounting_policy(POLICY)
        async with store_pool(name) as pool:
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
                # The auth hot path must join the native counter and quota;
                # the legacy USD values have already rounded to zero here.
                joined = await store.get_auth_context_with_spend(uid)
                assert joined is not None
                assert joined["accounting_currency"] == "CNY"
                assert joined["quota_daily_cost_amount"] == Decimal(quota)
                assert joined["cost_amount"] == Decimal(amount)
                assert joined["cost_usd"] == Decimal("0")
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
            assert summary["near_quota"]["top"][0]["quota_daily_amount"] == Decimal("0.00000002")
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


async def test_migration_preview_matches_each_stored_row_and_repeated_native_totals(monkeypatch):
    async with accounting_db() as (_name, conn):
        await conn.execute(
            "INSERT INTO routewise_probe_samples (endpoint_id, model_id, ok, cost_usd) "
            "VALUES ('e', 'm', TRUE, 0.0000000000001), ('e', 'm', TRUE, 0.0000000000001)"
        )
        preview = await migrate_currency(conn, POLICY)
        expected = Decimal(
            preview["tables"]["routewise_probe_samples"]["cost_amount"]["native_total"]
        )
        assert expected == Decimal("0.000000000002")

        _offline(monkeypatch)
        await migrate_currency(conn, POLICY, apply=True)
        assert (
            await conn.fetchval("SELECT SUM(cost_amount) FROM routewise_probe_samples") == expected
        )
        assert (
            await conn.fetchval("SELECT SUM(cost_usd) FROM routewise_probe_samples")
            == 0.0000000000002
        )
        await conn.execute(
            "INSERT INTO api_logs(request_id, model_id, provider, cost_amount) "
            "VALUES ('r', 'm', 'p', 0.00000001)"
        )
        repeated = await migrate_currency(conn, POLICY)
        assert Decimal(repeated["tables"]["api_logs"]["cost_amount"]["native_total"]) == Decimal(
            "0.00000001"
        )
        assert Decimal(repeated["tables"]["api_logs"]["cost_amount"]["original_usd_total"]) == 0


async def test_a_usd_request_cost_rounds_in_the_log_exactly_as_before():
    """``1.5e-8`` (binary, just under the half) is stored as 1e-8 by ``DECIMAL(12, 8)``.

    The log store hands PostgreSQL the computed float, as it always has; the
    trigger copies the stored dollars to the native column.
    """
    async with accounting_db() as (name, conn), store_pool(name) as pool:
        store = PostgresLogStore(pool, store_full_prompts=False)
        await store.log_request(
            request_id="half",
            model_id="m",
            provider="p",
            prompt=[{"role": "user", "content": "hi"}],
            response={"message": "ok"},
            usage={"prompt_tokens": 1, "completion_tokens": 0},
            latency_ms=1,
            status_code=200,
            metadata={},
            pricing={"prompt": "0.015", "completion": "0"},
        )
        row = await conn.fetchrow(
            "SELECT cost_usd, cost_amount, accounting_currency FROM api_logs "
            "WHERE request_id = 'half'"
        )
        assert row["cost_usd"] == Decimal("0.00000001")
        assert row["cost_amount"] == Decimal("0.00000001")
        assert row["accounting_currency"] == "USD"
