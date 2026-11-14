"""``0018_shared_spend``: expand-only, ``local`` by default, inert for older code."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_shared_tables_arrive_in_the_local_mode_and_old_writes_are_unchanged(scratch):
    await mig.upgrade(scratch, "0017_revoke_scoped_keys")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) "
            "VALUES ('u-1', '2026-11-14', 1.5, 2)"
        )

    await mig.upgrade(scratch, "0018_shared_spend")

    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT mode FROM spend_accounting") == "local"
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'spend_%'"
            )
        }
        assert {
            "spend_accounting",
            "spend_reservations",
            "spend_settlements",
            "spend_settlement_members",
            "spend_unidentified_writes",
        } <= tables
        # What the previous release writes, exactly as before: an upsert on the
        # counters, which the trigger only watches -- and, locally, ignores.
        await conn.execute(
            "INSERT INTO user_daily_cost AS c (user_id, day, cost_usd, requests) "
            "VALUES ('u-1', '2026-11-14', 0.5, 1) ON CONFLICT (user_id, day) DO UPDATE SET "
            "cost_usd = c.cost_usd + EXCLUDED.cost_usd, requests = c.requests + EXCLUDED.requests"
        )
        assert (
            str(await conn.fetchval("SELECT cost_usd FROM user_daily_cost WHERE user_id = 'u-1'"))
            == "2.000000"
        )
        assert await conn.fetchval("SELECT count(*) FROM spend_unidentified_writes") == 0
        # One retirement per reservation.
        await conn.execute(
            "INSERT INTO spend_settlement_members (reservation_id, batch_id, outcome) "
            "VALUES ('sr_1', 'b1', 'settled')"
        )
        with pytest.raises(Exception, match="duplicate key"):
            await conn.execute(
                "INSERT INTO spend_settlement_members (reservation_id, batch_id, outcome) "
                "VALUES ('sr_1', 'b2', 'released')"
            )


async def test_a_second_upgrade_changes_nothing(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("UPDATE spend_accounting SET mode = 'shared' WHERE id")
    # Re-running the revision's statements keeps the stored mode.
    from importlib import import_module

    revision = import_module("migrations.versions.0018_shared_spend")
    async with mig.connected(scratch) as conn:
        for statement in (*revision.STATEMENTS, *revision.TRIGGER_STATEMENTS):
            await conn.execute(statement)
        assert await conn.fetchval("SELECT mode FROM spend_accounting") == "shared"
