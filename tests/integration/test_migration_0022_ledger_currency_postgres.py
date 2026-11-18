"""``0022_ledger_currency`` on synthetic scratch databases.

What the revision promises, and what an operator relies on when a deployment
moves to it or past it:

- a USD database keeps every amount it had; rows the previous release writes
  afterwards get their native amounts from the triggers;
- running the revision again on a database that already has its objects --
  the case of a distribution whose own revisions built them, joined to this
  one by an empty merge -- changes no amount, USD or native (no second
  conversion);
- the compatibility bridges carry exact native quotas on a native ledger and
  behave as they did on a USD one;
- the offline currency migration raises the compatibility floor, so code that
  keeps only a USD ledger refuses the converted database, once;
- a native ledger whose shared rows lack native amounts is refused rather
  than relabelled.

Accounts, keys and amounts are synthetic.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from migrations.compat import Verdict, code_chain, judge, read_state
from ops.accounting_currency import LEDGER_REVISION, migrate_currency
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.storage.accounting_schema import (
    AMOUNT_COLUMNS,
    ROLLUP_AMOUNT_COLUMNS,
    accounting_schema_problems,
)
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0021_sso_identities"
POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")
VERSION_TABLE = "hybridinference_alembic_version"


@asynccontextmanager
async def database(revision: str):
    """A scratch database migrated to *revision*, and a connection to it."""
    async with mig.scratch_database() as name:
        await mig.upgrade(name, revision)
        conn = await postgres.connect(database=name)
        try:
            yield name, conn
        finally:
            bind_accounting_policy(None)
            await conn.close()


async def _seed(conn, *, shared: bool = False) -> None:
    """One synthetic account with a row in every table that holds money.

    With *shared*, a shared spend reservation too (which the offline currency
    migration refuses to relabel).
    """
    await conn.execute(
        "INSERT INTO users (id, email, password_hash, status, role) "
        "VALUES ('u1', 'u1@example.test', 'synthetic', 'active', 'pro')"
    )
    await conn.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, "
        "quota_daily_cost_usd, quota_monthly_cost_usd, budget_daily_usd, budget_monthly_usd) "
        "VALUES ('h1', 'hyi-u1', 'u1', 'u1', 12.3456, 100, 1.5, NULL)"
    )
    key_id = await conn.fetchval("SELECT id FROM api_keys WHERE key_prefix = 'hyi-u1'")
    await conn.execute(
        "INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd) "
        "VALUES ('u1', 12.3456, 100)"
    )
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, cost_usd, "
        "upstream_cost_usd) VALUES ('r1', 'm', 'p', 'u1', 0.00000123, 0.00000123), "
        "('r2', 'm', 'p', 'u1', NULL, NULL)"
    )
    await conn.execute(
        "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) "
        "VALUES ('u1', '2026-11-16', 0.123457, 3)"
    )
    await conn.execute(
        "INSERT INTO api_key_daily_cost (key_id, day, user_id, cost_usd, requests) "
        "VALUES ($1, '2026-11-16', 'u1', 0.123457, 3)",
        key_id,
    )
    await conn.execute(
        "INSERT INTO spend_counters (scope_id, period, cost_usd, requests) "
        "VALUES ('proj_1', 'd:2026-11-16', 0.5, 1)"
    )
    if not shared:
        return
    await conn.execute(
        "INSERT INTO spend_reservations (reservation_id, instance_id, user_id, amount_usd, "
        "check_day, operation, digest) "
        "VALUES ('sr_1', 'i1', 'u1', 0.25, '2026-11-16', 'request', 'd')"
    )


async def _amounts(conn) -> dict[str, list[dict]]:
    """Every USD and native amount, by table, in a stable order."""
    found: dict[str, list[dict]] = {}
    for table, pairs in {**AMOUNT_COLUMNS, **ROLLUP_AMOUNT_COLUMNS}.items():
        columns = sorted({*pairs, *pairs.values()})
        rows = await conn.fetch(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(columns)}"
        )
        found[table] = [dict(row) for row in rows]
    return found


async def _usd_amounts(conn) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for table, pairs in {**AMOUNT_COLUMNS, **ROLLUP_AMOUNT_COLUMNS}.items():
        columns = sorted(pairs)
        rows = await conn.fetch(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(columns)}"
        )
        found[table] = [dict(row) for row in rows]
    return found


async def _rewind(conn) -> None:
    """Record the database as one revision back, as a distribution's own head would be."""
    await conn.execute(f"UPDATE {VERSION_TABLE} SET version_num = $1", PREVIOUS)


def _offline(monkeypatch) -> None:
    async def offline(_conn):
        return None

    monkeypatch.setattr("ops.accounting_currency._require_offline", offline)


async def test_a_usd_database_upgrades_without_changing_an_amount():
    async with database(PREVIOUS) as (name, conn):
        await _seed(conn, shared=True)
        before = await _usd_amounts(conn)
        await mig.upgrade(name)
        assert await accounting_schema_problems(conn) == []
        assert await _usd_amounts(conn) == before
        # Rows older than the revision have no native amount, except the
        # shared rows, which take theirs at the identity rate.
        assert await conn.fetchval("SELECT cost_amount FROM api_logs WHERE request_id='r1'") is None
        reservation = await conn.fetchrow("SELECT amount, amount_usd FROM spend_reservations")
        assert reservation["amount"] == reservation["amount_usd"] == Decimal("0.25")
        # The previous release's own writes, which name only the USD columns,
        # get their native amounts from the triggers.
        await conn.execute(
            "UPDATE user_daily_cost SET cost_usd = cost_usd + 0.000001 WHERE user_id = 'u1'"
        )
        row = await conn.fetchrow(
            "SELECT cost_usd, cost_amount, accounting_currency FROM user_daily_cost"
        )
        assert row["cost_amount"] == row["cost_usd"] == Decimal("0.123458")
        assert row["accounting_currency"] == "USD"
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, cost_usd) "
            "VALUES ('r3', 'm', 'p', 0.00000001)"
        )
        assert await conn.fetchval(
            "SELECT cost_amount FROM api_logs WHERE request_id = 'r3'"
        ) == Decimal("0.00000001")
        # The previous release still starts on it.
        state = await read_state(conn)
        assert state.floor is None
        assert judge(state, code_chain()[: code_chain().index(PREVIOUS) + 1]) is (
            Verdict.NEWER_COMPATIBLE
        )


@pytest.mark.parametrize("ledger", ["USD", "CNY"])
async def test_running_the_revision_again_on_its_own_objects_converts_nothing(ledger, monkeypatch):
    """The upgrade a distribution takes when its own revisions already built all of this."""
    async with database("head") as (name, conn):
        await _seed(conn)
        if ledger == "CNY":
            _offline(monkeypatch)
            assert (await migrate_currency(conn, POLICY, apply=True))["applied"]
        before = await _amounts(conn)
        function = await conn.fetchval(
            "SELECT p.prosrc FROM pg_proc p WHERE p.proname = 'accounting_sync_amounts'"
        )
        for _ in range(2):
            await _rewind(conn)
            await mig.upgrade(name)
            assert await _amounts(conn) == before
        assert await accounting_schema_problems(conn) == []
        assert (
            await conn.fetchval(
                "SELECT p.prosrc FROM pg_proc p WHERE p.proname = 'accounting_sync_amounts'"
            )
            == function
        )
        if ledger == "CNY":
            # And the completed policy, applied again, converts nothing either.
            again = await migrate_currency(conn, POLICY, apply=True)
            assert again["already_applied"] and not again["applied"]
            assert await _amounts(conn) == before
            assert await conn.fetchval("SELECT count(*) FROM accounting_currency_migrations") == 1


async def test_the_bridges_carry_exact_native_quotas_on_a_native_ledger(monkeypatch):
    async with database("head") as (_name, conn):
        await _seed(conn)
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u1', 'Personal', 'personal')"
        )
        _offline(monkeypatch)
        await migrate_currency(conn, POLICY, apply=True)
        exact = Decimal("1.234567890123")
        await conn.execute(
            "UPDATE account_quotas SET quota_daily_cost_amount = $1, accounting_currency = 'CNY' "
            "WHERE user_id = 'u1'",
            exact,
        )
        org = await conn.fetchrow(
            "SELECT budget_daily_amount, budget_daily_usd FROM organizations WHERE id = 'org_p_u1'"
        )
        # The native amount as it was written, never a rounded USD mirror read back.
        assert org["budget_daily_amount"] == exact
        assert org["budget_daily_usd"] == (exact / POLICY.usd_to_currency_rate).quantize(
            Decimal("0.0001")
        )


async def test_the_bridges_behave_as_before_on_a_usd_ledger():
    async with database("head") as (_name, conn):
        await _seed(conn)
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u1', 'Personal', 'personal')"
        )
        await conn.execute(
            "UPDATE account_quotas SET quota_daily_cost_usd = 7.5 WHERE user_id = 'u1'"
        )
        org = await conn.fetchrow(
            "SELECT budget_daily_usd, budget_monthly_usd FROM organizations WHERE id = 'org_p_u1'"
        )
        assert org["budget_daily_usd"] == Decimal("7.5")
        assert org["budget_monthly_usd"] == Decimal("100")
        # A write the previous release makes on the organization reaches the account.
        await conn.execute("UPDATE organizations SET budget_daily_usd = 8 WHERE id = 'org_p_u1'")
        assert await conn.fetchval(
            "SELECT quota_daily_cost_usd FROM account_quotas WHERE user_id = 'u1'"
        ) == Decimal("8")


async def test_an_applied_migration_shuts_out_code_that_keeps_only_usd(monkeypatch):
    async with database("head") as (_name, conn):
        await _seed(conn)
        _offline(monkeypatch)
        result = await migrate_currency(conn, POLICY, apply=True)
        assert result["compatibility_floor_raised"] is True
        state = await read_state(conn)
        assert state.floor == LEDGER_REVISION
        chain = code_chain()
        assert judge(state, chain) is Verdict.CURRENT
        # Code whose chain ends before the revision meets a database it does
        # not know, whose floor it does not know either: it refuses to start.
        older = chain[: chain.index(LEDGER_REVISION)]
        assert judge(state, older) is Verdict.TOO_NEW
        # Applied again, the completed policy leaves the floor as it was.
        again = await migrate_currency(conn, POLICY, apply=True)
        assert again["already_applied"] and "compatibility_floor_raised" not in again
        assert (await read_state(conn)).floor == LEDGER_REVISION


async def test_a_native_ledger_with_usd_only_shared_rows_is_refused():
    async with database(PREVIOUS) as (name, conn):
        await _seed(conn, shared=True)
        for key in ("accounting:policy", "accounting:migration"):
            await conn.execute(
                "INSERT INTO site_settings (key, value, value_type, updated_by) "
                "VALUES ($1, $2, 'json', 'synthetic')",
                key,
                json.dumps(POLICY.as_dict()),
            )
        with pytest.raises(Exception, match="shared spend rows without native amounts"):
            await mig.upgrade(name)
        assert await conn.fetchval(f"SELECT version_num FROM {VERSION_TABLE}") == PREVIOUS
        assert await conn.fetchval("SELECT amount_usd FROM spend_reservations") == Decimal("0.25")
