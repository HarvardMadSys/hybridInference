"""``0018_projects_owned_by_accounts``: the account takes its quota and its projects.

The previous release keeps running on the result, so most of this is about
what its writes do afterwards, written here as the SQL that release issues.
"""

from __future__ import annotations

import asyncio

import pytest

from serving.storage.project_backfill import backfill_projects, default_project_id
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0017_revoke_scoped_keys"

#: The lock the previous release's quota writers take, shared, per transaction.
_WRITER_LOCK = (
    "SELECT pg_advisory_xact_lock_shared(hashtextextended('hybridinference:api-keys', 0))"
)


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _personal_account(conn, user_id: str, **org) -> None:
    """Seed an account the way the previous release left it."""
    await conn.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')",
        user_id,
        f"{user_id}@x.test",
    )
    await conn.execute(
        "INSERT INTO organizations "
        "(id, name, kind, status, budget_daily_usd, budget_monthly_usd, max_concurrent_requests) "
        "VALUES ($1, $2, 'personal', $3, $4, $5, $6)",
        f"org_p_{user_id}",
        user_id,
        org.get("status", "active"),
        org.get("daily"),
        org.get("monthly"),
        org.get("cap"),
    )
    await conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'owner')",
        f"org_p_{user_id}",
        user_id,
    )
    await conn.execute(
        "INSERT INTO projects (id, org_id, name, is_default) VALUES ($1, $2, 'Default', TRUE)",
        default_project_id(user_id),
        f"org_p_{user_id}",
    )


async def _key(
    conn, key_hash: str, user_id: str, *, status: str = "active", daily=None, monthly=None
) -> None:
    await conn.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, status, "
        "quota_daily_cost_usd, quota_monthly_cost_usd, project_id, org_id) "
        "VALUES ($1, $1, $2, $2, $3, $4, $5, $6, $7)",
        key_hash,
        user_id,
        status,
        daily,
        monthly,
        default_project_id(user_id),
        f"org_p_{user_id}",
    )


async def _quotas(conn) -> dict[str, tuple]:
    return {
        row["user_id"]: (row["quota_daily_cost_usd"], row["quota_monthly_cost_usd"])
        for row in await conn.fetch(
            "SELECT user_id, quota_daily_cost_usd, quota_monthly_cost_usd FROM account_quotas"
        )
    }


async def _org_budgets(conn) -> dict[str, tuple]:
    return {
        row["id"]: (row["budget_daily_usd"], row["budget_monthly_usd"])
        for row in await conn.fetch(
            "SELECT id, budget_daily_usd, budget_monthly_usd FROM organizations"
        )
    }


async def _key_quotas(conn) -> dict[str, tuple]:
    return {
        row["key_hash"]: (row["quota_daily_cost_usd"], row["quota_monthly_cost_usd"])
        for row in await conn.fetch(
            "SELECT key_hash, quota_daily_cost_usd, quota_monthly_cost_usd FROM api_keys"
        )
    }


async def test_the_account_quota_is_what_the_previous_release_enforced(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        # An organization budget overrides what the keys carry ...
        await _personal_account(conn, "u-budget", daily=3, monthly=30)
        await _key(conn, "h-live", "u-budget", daily=5)
        await _key(conn, "h-revoked", "u-budget", status="revoked", daily=5)
        # ... and may exist before the account has a key.
        await _personal_account(conn, "u-keyless", daily=1, monthly=2)
        await _personal_account(conn, "u-plain")
        await _key(conn, "h-plain", "u-plain", daily=7)
        await _personal_account(conn, "u-nothing")

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        quotas = await _quotas(conn)
        keys = await _key_quotas(conn)
        orgs = await _org_budgets(conn)
        owners = {
            row["id"]: row["user_id"]
            for row in await conn.fetch("SELECT id, user_id FROM projects")
        }

    assert quotas == {"u-budget": (3, 30), "u-keyless": (1, 2), "u-plain": (7, None)}
    # Every copy the previous release enforces says the same as the record:
    # the live keys, and the organization, which keeps a budget set for an
    # account without keys enforceable after a rollback. A revoked key is
    # left as it was.
    assert keys == {"h-live": (3, 30), "h-revoked": (5, None), "h-plain": (7, None)}
    assert orgs == {
        "org_p_u-budget": (3, 30),
        "org_p_u-keyless": (1, 2),
        "org_p_u-plain": (7, None),
        "org_p_u-nothing": (None, None),
    }
    assert owners == {
        default_project_id(user): user for user in ("u-budget", "u-keyless", "u-plain", "u-nothing")
    }


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ({"cap": 2}, "concurrency cap"),
        ({"status": "suspended"}, "suspended"),
        ({"daily": 1_000_000}, "budget of 1000000 or more"),
    ],
)
async def test_a_setting_no_account_can_take_over_stops_the_migration(scratch, setting, reason):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-odd", **setting)

    with pytest.raises(Exception, match=rf"org_p_u-odd \({reason}\)"):
        await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        # Nothing ran: the previous release goes on enforcing what it did.
        assert not await conn.fetchval("SELECT to_regclass('account_quotas') IS NOT NULL")


async def test_the_migration_waits_for_a_quota_write_in_flight(scratch):
    """A quota the running release is writing is copied, not overwritten."""
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-edit", daily=100)
        await _key(conn, "h-edit", "u-edit", daily=100)

    async with mig.connected(scratch) as writer:
        edit = writer.transaction()
        await edit.start()
        await writer.execute(_WRITER_LOCK)
        await writer.execute(
            "UPDATE api_keys SET quota_daily_cost_usd = 2 WHERE user_id = 'u-edit'"
        )
        migration = asyncio.create_task(mig.upgrade(scratch))
        await asyncio.sleep(0.5)
        assert not migration.done()
        await writer.execute(
            "UPDATE organizations SET budget_daily_usd = 2 WHERE id = 'org_p_u-edit'"
        )
        await edit.commit()
        await migration

    async with mig.connected(scratch) as conn:
        assert (await _quotas(conn))["u-edit"] == (2, None)
        assert (await _key_quotas(conn))["h-edit"] == (2, None)
        assert (await _org_budgets(conn))["org_p_u-edit"] == (2, None)


async def test_the_previous_release_keeps_working_after_it_runs(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-old")
        await _key(conn, "h-old", "u-old", daily=7)
    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        # It provisions an account, a project and a key its own way ...
        await _personal_account(conn, "u-new")
        await conn.execute(
            "INSERT INTO projects (id, org_id, name) VALUES ('proj_old', 'org_p_u-old', 'CI')"
        )
        await _key(conn, "h-new", "u-new", daily=5)
        # ... and finds projects by organization id, which this release still writes.
        found = await conn.fetchval(
            "SELECT COUNT(*) FROM projects WHERE org_id = 'org_p_u-old' AND status = 'active'"
        )
        owners = {
            row["id"]: row["user_id"]
            for row in await conn.fetch(
                "SELECT id, user_id FROM projects WHERE id IN ('proj_d_u-new', 'proj_old')"
            )
        }
        assert found == 2
        # This release finds them by account.
        assert owners == {"proj_d_u-new": "u-new", "proj_old": "u-old"}

        # It edits one account's monthly quota only. The account has no record
        # yet: the daily quota its key carries must not be recorded as unset.
        await conn.execute(
            "UPDATE organizations SET budget_monthly_usd = 9 WHERE id = 'org_p_u-new'"
        )
        assert (await _quotas(conn))["u-new"] == (5, 9)
        assert (await _org_budgets(conn))["org_p_u-new"] == (5, 9)

        # It lowers a daily quota, sets a monthly cap, and removes the cap again.
        await conn.execute("UPDATE organizations SET budget_daily_usd = 2 WHERE id = 'org_p_u-old'")
        assert (await _quotas(conn))["u-old"] == (2, None)
        await conn.execute(
            "UPDATE organizations SET budget_monthly_usd = 20 WHERE id = 'org_p_u-old'"
        )
        assert (await _quotas(conn))["u-old"] == (2, 20)
        await conn.execute(
            "UPDATE organizations SET budget_monthly_usd = NULL WHERE id = 'org_p_u-old'"
        )
        assert (await _quotas(conn))["u-old"] == (2, None)

        # Resuming an organization, none of which can be suspended, changes nothing.
        await conn.execute("UPDATE organizations SET status = 'active' WHERE id = 'org_p_u-old'")
        # What no account setting replaces is refused, and says so.
        for statement, message in (
            ("SET status = 'suspended'", "Suspend the account instead"),
            ("SET max_concurrent_requests = 3", "concurrency cap"),
            ("SET budget_daily_usd = 1000000", "under 1000000"),
        ):
            with pytest.raises(Exception, match=message) as refused:
                await conn.execute(f"UPDATE organizations {statement} WHERE id = 'org_p_u-old'")
            assert message in f"{refused.value} {getattr(refused.value, 'hint', '')}"
        assert (await _quotas(conn))["u-old"] == (2, None)
        assert await conn.fetchval("SELECT status FROM users WHERE id = 'u-old'") == "active"


async def test_an_account_quota_write_reaches_the_organization(scratch):
    """What this release records is what a rolled-back release enforces."""
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-keyless")
    await mig.upgrade(scratch)

    upsert = (
        "INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd) "
        "VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET "
        "quota_daily_cost_usd = EXCLUDED.quota_daily_cost_usd, "
        "quota_monthly_cost_usd = EXCLUDED.quota_monthly_cost_usd"
    )
    async with mig.connected(scratch) as conn:
        await conn.execute(upsert, "u-keyless", 2, 5)
        assert (await _org_budgets(conn))["org_p_u-keyless"] == (2, 5)
        await conn.execute(upsert, "u-keyless", 2, None)
        assert (await _org_budgets(conn))["org_p_u-keyless"] == (2, None)
        assert (await _quotas(conn))["u-keyless"] == (2, None)

        # An organization row created after the record starts out showing it.
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u-late', 'late@x.test', 'h')"
        )
        await conn.execute(upsert, "u-late", 4, 40)
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u-late', 'l', 'personal')"
        )
        assert (await _org_budgets(conn))["org_p_u-late"] == (4, 40)


async def test_backfill_gives_every_account_a_default_project_and_adopts_old_ones(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES "
            "('u-bare', 'bare@x.test', 'h'), ('u-old', 'old@x.test', 'h')"
        )
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-bare', 'hyi-bare', 'u-bare', 'u-bare')"
        )
        # A project the previous release created while the migration ran.
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u-old', 'Old', 'personal')"
        )
        await conn.execute(
            "INSERT INTO projects (id, org_id, name, is_default) "
            "VALUES ('proj_d_u-old', 'org_p_u-old', 'Default', TRUE)"
        )

        first = await backfill_projects(conn)
        second = await backfill_projects(conn)

        projects = {
            row["id"]: (row["user_id"], row["name"], row["is_default"])
            for row in await conn.fetch("SELECT id, user_id, name, is_default FROM projects")
        }
        key = await conn.fetchrow(
            "SELECT project_id, org_id FROM api_keys WHERE key_hash = 'h-bare'"
        )
        member = await conn.fetchval(
            "SELECT role FROM org_members WHERE org_id = 'org_p_u-bare' AND user_id = 'u-bare'"
        )

    assert first == 1
    assert second == 0
    assert projects == {
        "proj_d_u-bare": ("u-bare", "Default", True),
        "proj_d_u-old": ("u-old", "Default", True),
    }
    # What the previous release's own backfill looks for is all there, so a
    # rolled-back release leaves the account alone.
    assert dict(key) == {"project_id": "proj_d_u-bare", "org_id": "org_p_u-bare"}
    assert member == "owner"
