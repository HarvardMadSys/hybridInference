"""``0023_orgs``: organizations, their connections and members, the audit column."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from ops.accounting_currency import migrate_currency
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.storage.accounting_schema import accounting_schema_problems
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

TABLES = ("orgs", "org_sso_connections", "org_memberships")
POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
    )
    return {row["column_name"] for row in rows}


async def _seed(conn) -> None:
    await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
    await conn.execute("INSERT INTO users (id, email) VALUES ('u-2', 'b@example.edu')")
    await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-1', 'Example One')")
    await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-2', 'Example Two')")


async def test_the_tables_arrive_empty_and_existing_rows_are_untouched(scratch):
    await mig.upgrade(scratch, "0022_ledger_currency")
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-old', 'old@example.edu')")
        await conn.execute(
            "INSERT INTO admin_audit_log (admin_ip, action) VALUES ('unknown', 'create_user')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ANY($1)",
                list(TABLES),
            )
        }
        counts = [await conn.fetchval(f"SELECT COUNT(*) FROM {table}") for table in TABLES]
        user = await conn.fetchrow("SELECT email, status FROM users WHERE id = 'u-old'")
        audit = await conn.fetchrow("SELECT action, organization_id FROM admin_audit_log")
        organization_columns = await _columns(conn, "orgs")
        connection_columns = await _columns(conn, "org_sso_connections")
        member_columns = await _columns(conn, "org_memberships")

    assert tables == set(TABLES)
    assert counts == [0, 0, 0]
    assert dict(user) == {"email": "old@example.edu", "status": "active"}
    assert dict(audit) == {"action": "create_user", "organization_id": None}
    assert organization_columns == {
        "id",
        "name",
        "monthly_limit_usd",
        "monthly_limit_amount",
        "default_member_monthly_limit_usd",
        "default_member_monthly_limit_amount",
        "accounting_currency",
        "created_at",
        "updated_at",
    }
    assert connection_columns == {"provider_id", "organization_id", "created_at"}
    assert member_columns == {
        "user_id",
        "organization_id",
        "role",
        "joined_at",
        "removed_at",
        "removed_by",
        "monthly_limit_usd",
        "monthly_limit_amount",
        "accounting_currency",
        "disabled_models",
    }


async def test_an_account_and_a_connection_belong_to_one_organization(scratch):
    await mig.upgrade(scratch)
    member = "INSERT INTO org_memberships (user_id, organization_id) VALUES ($1, $2)"
    connection = "INSERT INTO org_sso_connections (provider_id, organization_id) VALUES ($1, $2)"
    async with mig.connected(scratch) as conn:
        await _seed(conn)
        await conn.execute(member, "u-1", "o-1")
        await conn.execute(connection, "campus", "o-1")

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(member, "u-1", "o-2")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(connection, "campus", "o-2")
        # A second account and a second connection of the same organization are fine.
        await conn.execute(member, "u-2", "o-1")
        await conn.execute(connection, "campus-staff", "o-1")

        role = await conn.fetchval("SELECT role FROM org_memberships WHERE user_id = 'u-1'")

    assert role == "member"


async def test_a_membership_leaves_with_its_account_and_everything_with_its_organization(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _seed(conn)
        for user_id in ("u-1", "u-2"):
            await conn.execute(
                "INSERT INTO org_memberships (user_id, organization_id) VALUES ($1, 'o-1')",
                user_id,
            )
        await conn.execute(
            "INSERT INTO org_sso_connections (provider_id, organization_id) "
            "VALUES ('campus', 'o-1')"
        )

        await conn.execute("DELETE FROM users WHERE id = 'u-1'")
        after_account = await conn.fetchval("SELECT COUNT(*) FROM org_memberships")
        await conn.execute("DELETE FROM orgs WHERE id = 'o-1'")
        members = await conn.fetchval("SELECT COUNT(*) FROM org_memberships")
        connections = await conn.fetchval("SELECT COUNT(*) FROM org_sso_connections")
        accounts = await conn.fetchval("SELECT COUNT(*) FROM users WHERE id = 'u-2'")

    assert after_account == 1
    assert (members, connections) == (0, 0)
    # Deleting an organization deletes no account.
    assert accounts == 1


async def test_a_role_and_a_name_are_constrained(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _seed(conn)

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO org_memberships (user_id, organization_id, role) "
                "VALUES ('u-1', 'o-1', 'owner')"
            )
        # Names differ by more than their case.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-3', 'EXAMPLE ONE')")


async def test_a_limit_written_in_usd_gets_its_native_amount(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO orgs (id, name, monthly_limit_usd) VALUES ('o-1', 'Example One', 1000)"
        )
        created = await conn.fetchrow(
            "SELECT monthly_limit_amount, default_member_monthly_limit_amount, "
            "accounting_currency FROM orgs WHERE id = 'o-1'"
        )
        await conn.execute("UPDATE orgs SET default_member_monthly_limit_usd = 20 WHERE id = 'o-1'")
        updated = await conn.fetchval(
            "SELECT default_member_monthly_limit_amount FROM orgs WHERE id = 'o-1'"
        )
        # The limit an organization sets for one member is a pair on its membership.
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id) VALUES ('u-1', 'o-1')"
        )
        member = "SELECT monthly_limit_usd, monthly_limit_amount, accounting_currency "
        member += "FROM org_memberships WHERE user_id = 'u-1'"
        joined = await conn.fetchrow(member)
        await conn.execute(
            "UPDATE org_memberships SET monthly_limit_usd = 30 WHERE user_id = 'u-1'"
        )
        limited = await conn.fetchrow(member)
        # A write that names neither column leaves the pair as it is.
        await conn.execute("UPDATE org_memberships SET role = 'admin'")
        after_role = await conn.fetchrow(member)
        await conn.execute(
            "UPDATE org_memberships SET monthly_limit_usd = NULL WHERE user_id = 'u-1'"
        )
        cleared = await conn.fetchrow(member)

    # A USD ledger: the native amount is the USD amount, and no limit stays no limit.
    assert created["monthly_limit_amount"] == Decimal(1000)
    assert created["default_member_monthly_limit_amount"] is None
    assert created["accounting_currency"] == "USD"
    assert updated == Decimal(20)
    assert tuple(joined) == (None, None, "USD")
    assert tuple(limited) == (Decimal(30), Decimal(30), "USD")
    assert tuple(after_role) == tuple(limited)
    assert tuple(cleared) == (None, None, "USD")


async def test_the_startup_check_of_the_ledger_covers_the_limits(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        complete = await accounting_schema_problems(conn)
        await conn.execute("DROP TRIGGER orgs_accounting_amounts ON orgs")
        await conn.execute("DROP TRIGGER org_memberships_accounting_amounts ON org_memberships")
        without_triggers = await accounting_schema_problems(conn)

    assert complete == []
    assert without_triggers == [
        "trigger orgs_accounting_amounts is missing",
        "trigger org_memberships_accounting_amounts is missing",
    ]


async def test_the_offline_currency_migration_converts_the_limits_of_an_organization(
    scratch, monkeypatch
):
    """A USD deployment that has organizations moves its ledger to CNY."""

    async def offline(_conn):
        return None

    monkeypatch.setattr("ops.accounting_currency._require_offline", offline)
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO orgs "
            "(id, name, monthly_limit_usd, default_member_monthly_limit_usd) "
            "VALUES ('o-1', 'Example One', 1000, 20)"
        )
        await conn.execute("INSERT INTO orgs (id, name) VALUES ('o-2', 'Example Two')")
        await conn.execute(
            "INSERT INTO spend_counters (scope_id, period, cost_usd, requests) "
            "VALUES ('o:o-1', 'm:2026-11', 12.5, 4)"
        )
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-1', 'a@example.edu')")
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-2', 'b@example.edu')")
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id, monthly_limit_usd) "
            "VALUES ('u-1', 'o-1', 30), ('u-2', 'o-1', NULL)"
        )
        try:
            result = await migrate_currency(conn, POLICY, apply=True)
            one = await conn.fetchrow(
                "SELECT monthly_limit_usd, monthly_limit_amount, "
                "default_member_monthly_limit_usd, default_member_monthly_limit_amount, "
                "accounting_currency FROM orgs WHERE id = 'o-1'"
            )
            two = await conn.fetchrow(
                "SELECT monthly_limit_amount, default_member_monthly_limit_amount, "
                "accounting_currency FROM orgs WHERE id = 'o-2'"
            )
            counter = await conn.fetchrow(
                "SELECT cost_usd, cost_amount, accounting_currency FROM spend_counters "
                "WHERE scope_id = 'o:o-1'"
            )
            members = await conn.fetch(
                "SELECT monthly_limit_usd, monthly_limit_amount, accounting_currency "
                "FROM org_memberships ORDER BY user_id"
            )
            # A converted row takes the writes of the native ledger: a rename,
            # and a limit written in the native unit, whose USD equivalent follows.
            await conn.execute("UPDATE orgs SET name = 'Renamed' WHERE id = 'o-1'")
            await conn.execute("UPDATE orgs SET monthly_limit_amount = 7000 WHERE id = 'o-1'")
            rewritten = await conn.fetchval("SELECT monthly_limit_usd FROM orgs WHERE id = 'o-1'")
        finally:
            bind_accounting_policy(None)

    assert result["applied"] is True
    converted = result["tables"]["orgs"]
    assert Decimal(converted["monthly_limit_amount"]["native_total"]) == Decimal("6718")
    assert Decimal(converted["default_member_monthly_limit_amount"]["native_total"]) == (
        Decimal("134.36")
    )
    # The USD values stay as they were; the native ones are the USD ones at the rate.
    assert (one["monthly_limit_usd"], one["monthly_limit_amount"]) == (
        Decimal("1000"),
        Decimal("6718"),
    )
    assert (
        one["default_member_monthly_limit_usd"],
        one["default_member_monthly_limit_amount"],
    ) == (Decimal("20"), Decimal("134.36"))
    assert one["accounting_currency"] == "CNY"
    # No limit stays no limit, and the row is tagged all the same.
    assert tuple(two) == (None, None, "CNY")
    assert (counter["cost_usd"], counter["cost_amount"], counter["accounting_currency"]) == (
        Decimal("12.5"),
        Decimal("83.975"),
        "CNY",
    )
    assert rewritten == (Decimal(7000) / POLICY.usd_to_currency_rate).quantize(Decimal("0.0001"))
    # The limit of a member converts as the limits of its organization do.
    converted = result["tables"]["org_memberships"]
    assert Decimal(converted["monthly_limit_amount"]["native_total"]) == Decimal("201.54")
    assert [tuple(row) for row in members] == [
        (Decimal("30"), Decimal("201.54"), "CNY"),
        (None, None, "CNY"),
    ]


async def test_the_audit_rows_of_an_organization_are_found_through_their_index(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        indexes = {
            row["indexname"]: row["indexdef"]
            for row in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'admin_audit_log'"
            )
        }

    definition = indexes["idx_admin_audit_log_organization"]
    assert '(organization_id, "timestamp" DESC)' in definition
    assert "WHERE (organization_id IS NOT NULL)" in definition
