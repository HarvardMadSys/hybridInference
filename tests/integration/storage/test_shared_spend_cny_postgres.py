"""Two-instance accounting on a native (CNY) ledger, with synthetic isolated databases."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import pytest

from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.shared_spend.ledger import SharedSpendLedger
from serving.spend_ledger import SpendCheck
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils import context as req_ctx
from tests.fixtures import migrations, postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]
POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")


async def policy(conn):
    for key in ("accounting:policy", "accounting:migration"):
        await conn.execute(
            "INSERT INTO site_settings (key, value, value_type, updated_by) "
            "VALUES ($1, $2, 'json', 'synthetic-test')",
            key,
            json.dumps(POLICY.as_dict()),
        )


async def test_two_native_instances_admit_and_settle_exactly_once():
    async with migrations.scratch_database() as name:
        await migrations.upgrade(name)
        async with migrations.connected(name) as conn:
            await policy(conn)
        pool = await postgres.create_pool(database=name, min_size=1, max_size=8)
        bind_accounting_policy(POLICY)
        a = SharedSpendLedger(
            PostgresOperationalStore(pool).shared_spend_backend(), instance_id="cny-a"
        )
        b = SharedSpendLedger(
            PostgresOperationalStore(pool).shared_spend_backend(), instance_id="cny-b"
        )
        budget = Decimal("1.234567890123")
        asked = Decimal("0.312345678901")
        try:

            async def admit(index):
                ledger = a if index % 2 else b
                check = SpendCheck(
                    ledger=ledger,
                    user_id="synthetic",
                    key_id=None,
                    account_quota_usd=budget,
                    account_spent_usd=Decimal(0),
                    read_at=time.monotonic(),
                )
                return ledger, await ledger.admit(check, asked)

            admitted = await asyncio.gather(*(admit(i) for i in range(12)))
            reservations = [(ledger, pair[0]) for ledger, pair in admitted if pair[0] is not None]
            assert len(reservations) == 4
            assert sum((r.amount for _, r in reservations), Decimal(0)) == budget
            async with pool.acquire() as conn:
                held = await conn.fetchval("SELECT SUM(amount) FROM spend_reservations")
                assert held == budget
                assert await conn.fetchval(
                    "SELECT bool_and(accounting_currency='CNY') FROM spend_reservations"
                )
                # USD compatibility is genuinely USD, never the CNY scalar.
                assert await conn.fetchval("SELECT SUM(amount_usd) FROM spend_reservations") < held
            cost = Decimal("0.000000123456")
            for ledger, reservation in reservations:
                with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
                    ledger.record("synthetic", cost)
                reservation.release()
            await a.flush(timeout=5)
            await b.flush(timeout=5)
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval("SELECT SUM(cost_amount) FROM user_daily_cost") == cost * 4
                )
                assert await conn.fetchval("SELECT COUNT(*) FROM spend_reservations") == 0
                assert await conn.fetchval("SELECT COUNT(*) FROM spend_settlement_members") == 4
        finally:
            await a.flush(timeout=1)
            await b.flush(timeout=1)
            bind_accounting_policy(None)
            await pool.close()


@pytest.mark.parametrize("currency", ["USD", "CNY"])
async def test_nonzero_crash_reconciliation_keeps_native_and_usd_units(currency):
    from serving.shared_spend.store import (
        AdmissionRequest,
        CostLine,
        Member,
        Reconciliation,
        SharedSpendStore,
    )
    from serving.spend_ledger import utc_day

    async with migrations.scratch_database() as name:
        await migrations.upgrade(name)
        async with migrations.connected(name) as conn:
            if currency == "CNY":
                await policy(conn)
        selected = POLICY if currency == "CNY" else AccountingPolicy()
        bind_accounting_policy(selected)
        pool = await postgres.create_pool(database=name, min_size=1, max_size=3)
        try:
            backend = SharedSpendStore(pool)
            request = AdmissionRequest(
                "synthetic-reservation",
                "gone-owner",
                "synthetic-user",
                None,
                None,
                Decimal("0.3"),
                utc_day(),
                "request",
            )
            async with pool.acquire() as conn:
                await backend.admit_on(
                    conn, request, lambda _: Decimal("0.3"), budget_ms=1000, on_xid=lambda _: None
                )
                created_at = await conn.fetchval(
                    "SELECT created_at FROM spend_reservations WHERE reservation_id=$1",
                    request.reservation_id,
                )
            cost = Decimal("0.000000123456") if currency == "CNY" else Decimal("0.123456")
            member = Member(
                reservation_id=request.reservation_id,
                user_id=request.user_id,
                kind="settle",
                costs=(CostLine(request.user_id, utc_day(), None, cost, 1, None),),
            )
            entry = Reconciliation(
                request.reservation_id, Decimal("0.3"), "request", created_at, member
            )
            result, _ = await backend.reconcile(
                "gone-owner", [entry], operator="synthetic-operator", reason="isolated test"
            )
            assert result.members["settled"] == [request.reservation_id]
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT cost_amount,cost_usd,accounting_currency FROM spend_reconciliations"
                )
                assert row["cost_amount"] == cost and row["accounting_currency"] == currency
                assert row["cost_usd"] == selected.to_usd(cost).quantize(Decimal("0.000001"))
                assert await conn.fetchval("SELECT cost_amount FROM user_daily_cost") == cost
        finally:
            bind_accounting_policy(None)
            await pool.close()


async def test_cny_role_quota_update_preserves_unrounded_monthly_authority():
    async with migrations.scratch_database() as name:
        await migrations.upgrade(name)
        async with migrations.connected(name) as conn:
            await policy(conn)
            await conn.execute(
                "INSERT INTO users (id,email,password_hash,status,role) "
                "VALUES ('native-role','role@example.test','synthetic','active','pro')"
            )
            await conn.execute(
                "INSERT INTO api_keys (key_hash,key_prefix,user_id,account_id,quota_daily_cost_usd, "
                "quota_daily_cost_amount,quota_monthly_cost_amount) "
                "VALUES ('role','role','native-role','native-role',NULL,1,2.345678901234)"
            )
        pool = await postgres.create_pool(database=name, min_size=1, max_size=2)
        bind_accounting_policy(POLICY)
        try:
            assert await PostgresOperationalStore(pool).apply_role_quota("pro", Decimal("0.3")) == 1
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT quota_daily_cost_amount,quota_monthly_cost_amount FROM account_quotas"
                )
                assert row["quota_daily_cost_amount"] == POLICY.from_usd(Decimal("0.3"))
                assert row["quota_monthly_cost_amount"] == Decimal("2.345678901234")
        finally:
            bind_accounting_policy(None)
            await pool.close()


async def test_currency_activation_refuses_existing_shared_liabilities_without_relabeling():
    from ops.accounting_currency import migrate_currency

    async with migrations.scratch_database() as name:
        await migrations.upgrade(name)
        async with migrations.connected(name) as conn:
            await conn.execute(
                "INSERT INTO spend_reservations (reservation_id,instance_id,user_id,amount_usd,check_day,operation,digest) "
                "VALUES ('USD-left','gone','synthetic',0.3,'2026-11-17','request','synthetic-digest')"
            )
            with pytest.raises(ValueError, match="existing shared accounting evidence"):
                await migrate_currency(conn, POLICY, apply=True)
            assert await conn.fetchval("SELECT amount FROM spend_reservations") == Decimal("0.3")
            assert (
                await conn.fetchval("SELECT accounting_currency FROM spend_reservations") == "USD"
            )
            assert await conn.fetchval("SELECT count(*) FROM accounting_currency_migrations") == 0
