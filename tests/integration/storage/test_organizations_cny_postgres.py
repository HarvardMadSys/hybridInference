"""Organizations on a native (CNY) ledger, on a synthetic scratch database.

A limit is money. On a ledger that is not in USD it is kept exactly in its
native column, with the USD equivalent beside it, and it is compared in the
native unit: the limit of the organization, the default of a member and the
quota of a member's account. The counter of the organization holds native
amounts too.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from serving import organizations as organization_rules, quota
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.audit import AuditActor
from serving.storage import organizations as orgs
from serving.storage.base import CostIncrement
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import migrations, postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")
RATE = POLICY.usd_to_currency_rate
PLATFORM = AuditActor.admin("u-root", "192.0.2.10")
ORG_ADMIN = AuditActor.user("u-lead", "192.0.2.20")
FOUR_PLACES = Decimal("0.0001")


@asynccontextmanager
async def cny_store():
    """A store on a scratch database whose ledger is in CNY, with its pool."""
    async with migrations.scratch_database() as name:
        await migrations.upgrade(name)
        async with migrations.connected(name) as conn:
            for key in ("accounting:policy", "accounting:migration"):
                await conn.execute(
                    "INSERT INTO site_settings (key, value, value_type, updated_by) "
                    "VALUES ($1, $2, 'json', 'synthetic-test')",
                    key,
                    json.dumps(POLICY.as_dict()),
                )
        pool = await postgres.create_pool(database=name, min_size=1, max_size=4)
        bind_accounting_policy(POLICY)
        try:
            yield PostgresOperationalStore(pool), pool
        finally:
            bind_accounting_policy(None)
            await pool.close()


async def _accounts(store, *names: str) -> None:
    for name in names:
        await store.create_user(
            user_id=name,
            email=f"{name}@example.edu",
            password_hash=None,
            user_name=name,
            email_verified=True,
            status="active",
        )


async def test_the_limits_of_an_organization_are_kept_exactly_in_the_native_column():
    exact = Decimal("700.123456789012")
    async with cny_store() as (store, pool):
        await _accounts(store, "u1", "u2")

        created = await store.create_organization(
            organization_id="o-1",
            name="Example University",
            monthly_limit_usd=None,
            monthly_limit_amount=exact,
            connections=[("campus", ["u1", "u2"])],
            actor=PLATFORM,
        )
        by_usd = await store.set_organization_limit(
            "o-1",
            "default_member_monthly_limit",
            usd=Decimal("20"),
            amount=None,
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )
        above = await store.set_organization_limit(
            "o-1",
            "default_member_monthly_limit",
            usd=None,
            amount=exact + Decimal("0.000000000001"),
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )
        # Every update of the row runs the trigger, a rename too.
        renamed = await store.rename_organization("o-1", "Example University Two", actor=PLATFORM)
        row = await pool.fetchrow(
            "SELECT name, monthly_limit_usd, monthly_limit_amount, "
            "default_member_monthly_limit_usd, default_member_monthly_limit_amount, "
            "accounting_currency FROM sso_organizations WHERE id = 'o-1'"
        )

    assert created == (orgs.CREATED, 2)
    # A USD value is taken at the fixed rate; the comparison is in the native unit.
    assert by_usd == (orgs.UPDATED, None, Decimal("134.36"))
    assert above[0] == orgs.ABOVE_LIMIT
    assert renamed == orgs.UPDATED
    assert row["name"] == "Example University Two"
    assert row["monthly_limit_amount"] == exact
    assert row["monthly_limit_usd"] == (exact / RATE).quantize(FOUR_PLACES)
    assert row["default_member_monthly_limit_amount"] == Decimal("134.36")
    assert row["default_member_monthly_limit_usd"] == Decimal("20")
    assert row["accounting_currency"] == "CNY"


async def test_the_limit_of_a_member_is_kept_exactly_on_its_record_and_its_keys():
    exact = Decimal("100.000000000001")
    async with cny_store() as (store, pool):
        await _accounts(store, "u1")
        await store.create_key(key_hash="h-u1", key_prefix="hyi-u1", user_id="u1", account_id="u1")
        await store.create_organization(
            organization_id="o-1",
            name="Example University",
            monthly_limit_usd=None,
            monthly_limit_amount=Decimal("700"),
            connections=[("campus", ["u1"])],
            actor=PLATFORM,
        )

        async def stored() -> tuple:
            record = await pool.fetchrow(
                "SELECT quota_monthly_cost_amount, quota_monthly_cost_usd, accounting_currency "
                "FROM account_quotas WHERE user_id = 'u1'"
            )
            key = await pool.fetchrow(
                "SELECT quota_monthly_cost_amount, quota_monthly_cost_usd, accounting_currency "
                "FROM api_keys WHERE user_id = 'u1'"
            )
            return tuple(record), tuple(key)

        # As the route sends it: the native amount, and its USD equivalent to four places.
        saved = await store.set_organization_member_limit(
            "o-1",
            "u1",
            usd=(exact / RATE).quantize(FOUR_PLACES),
            amount=exact,
            actor=ORG_ADMIN,
        )
        after_native = await stored()
        above = await store.set_organization_member_limit(
            "o-1", "u1", usd=None, amount=Decimal("700.000000000001"), actor=ORG_ADMIN
        )
        by_usd = await store.set_organization_member_limit(
            "o-1", "u1", usd=Decimal("10"), amount=None, actor=ORG_ADMIN
        )
        after_usd = await stored()
        looked_up = await store.get_auth_context_with_spend("h-u1")
        cleared = await store.set_organization_member_limit(
            "o-1", "u1", usd=None, amount=None, actor=ORG_ADMIN
        )
        after_clear = await stored()

    usd = (exact / RATE).quantize(FOUR_PLACES)
    assert saved == (orgs.UPDATED, None, exact)
    assert after_native == ((exact, usd, "CNY"), (exact, usd, "CNY"))
    assert above == (orgs.ABOVE_LIMIT, exact, None)
    assert by_usd == (orgs.UPDATED, exact, Decimal("67.18"))
    assert after_usd == (
        (Decimal("67.18"), Decimal("10"), "CNY"),
        (Decimal("67.18"), Decimal("10"), "CNY"),
    )
    assert quota.native_limit(
        looked_up, "quota_monthly_cost_amount", "quota_monthly_cost_usd"
    ) == Decimal("67.18")
    assert cleared == (orgs.UPDATED, Decimal("67.18"), None)
    assert after_clear == ((None, None, "CNY"), (None, None, "CNY"))


async def test_the_counter_and_the_lookups_of_an_organization_hold_native_amounts():
    limit = Decimal("700.123456789012")
    cost = Decimal("0.000000123456")
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    async with cny_store() as (store, pool):
        await _accounts(store, "u1", "u2")
        key = await store.create_key(
            key_hash="h-u1", key_prefix="hyi-u1", user_id="u1", account_id="u1"
        )
        await store.create_organization(
            organization_id="o-1",
            name="Example University",
            monthly_limit_usd=None,
            monthly_limit_amount=limit,
            connections=[("campus", ["u1", "u2"])],
            actor=PLATFORM,
        )
        await store.set_organization_limit(
            "o-1",
            "default_member_monthly_limit",
            usd=None,
            amount=Decimal("50.5"),
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )

        # An exact native charge, and a USD-only one that the store takes at the rate.
        await store.increment_user_costs(
            [
                CostIncrement(
                    user_id="u1",
                    day=day,
                    api_key_id=key["id"],
                    cost_usd=cost / RATE,
                    cost_amount=cost,
                ),
                CostIncrement(user_id="u2", day=day, api_key_id=None, cost_usd=Decimal("1")),
            ]
        )
        counters = await pool.fetch(
            "SELECT period, cost_amount, accounting_currency, requests FROM spend_counters "
            "WHERE scope_id = 'o:o-1' ORDER BY period"
        )
        by_key = await store.get_auth_context_with_spend("h-u1")
        by_hash = await store.get_auth_context_by_key_hash("h-u1")
        [by_user] = await store.get_quota_context_for_user("u1")
        keyless = await store.get_organization_spend_scope("u2")
        month = await store.get_organization_month_spend("o-1")
        fresh = await organization_rules.check_fields(store, by_hash, native=True)

    spent = cost + RATE
    assert [(row["period"][:2], row["cost_amount"], row["requests"]) for row in counters] == [
        ("m:", spent, 2),
    ]
    assert {row["accounting_currency"] for row in counters} == {"CNY"}
    assert month["cost_amount"] == spent
    expected = {
        "organization_id": "o-1",
        "organization_name": "Example University",
        "organization_limit_usd": limit,
        "organization_spent_month_usd": spent,
    }
    # The identity lookup carries no spend; the gate reads the counter for it.
    assert "organization_spent_month_amount" not in by_hash
    assert fresh == expected
    for lookup in (by_key, by_user, keyless):
        assert quota.organization_fields(lookup, native=True) == expected
    for lookup in (by_key, by_hash, by_user, keyless):
        # No quota of its own: the default of the organization, in the native unit.
        assert quota.native_limit(
            lookup, "quota_monthly_cost_amount", "quota_monthly_cost_usd"
        ) == Decimal("50.5")
