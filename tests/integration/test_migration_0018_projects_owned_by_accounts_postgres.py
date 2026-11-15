"""``0018_projects_owned_by_accounts``: projects move to accounts, organizations empty out."""

from __future__ import annotations

import pytest

from serving.storage.project_backfill import backfill_projects, default_project_id
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0017_revoke_scoped_keys"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _personal_account(conn, user_id: str, **org) -> None:
    """Seed an account the way the previous release left it."""
    await conn.execute(
        "INSERT INTO users (id, email, password_hash, max_concurrent_requests) "
        "VALUES ($1, $2, 'h', $3)",
        user_id,
        f"{user_id}@x.test",
        org.pop("user_cap", None),
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


async def _key(conn, key_hash: str, user_id: str, *, status: str = "active", daily=None) -> None:
    await conn.execute(
        "INSERT INTO api_keys "
        "(key_hash, key_prefix, user_id, account_id, status, quota_daily_cost_usd, project_id) "
        "VALUES ($1, $1, $2, $2, $3, $4, $5)",
        key_hash,
        user_id,
        status,
        daily,
        default_project_id(user_id),
    )


async def _quotas(conn) -> dict[str, tuple]:
    return {
        row["user_id"]: (row["quota_daily_cost_usd"], row["quota_monthly_cost_usd"])
        for row in await conn.fetch(
            "SELECT user_id, quota_daily_cost_usd, quota_monthly_cost_usd FROM account_quotas"
        )
    }


async def test_organization_settings_move_onto_the_account(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-budget", daily=3, monthly=30)
        await _key(conn, "h-live", "u-budget", daily=5)
        await _key(conn, "h-revoked", "u-budget", status="revoked", daily=5)
        # A budget set for an account before it had a key.
        await _personal_account(conn, "u-keyless", daily=1, monthly=2)
        await _personal_account(conn, "u-suspended", status="suspended")
        await _personal_account(conn, "u-plain")
        await _key(conn, "h-plain", "u-plain", daily=7)
        await _personal_account(conn, "u-nothing")

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        keys = {
            row["key_hash"]: (row["quota_daily_cost_usd"], row["quota_monthly_cost_usd"])
            for row in await conn.fetch(
                "SELECT key_hash, quota_daily_cost_usd, quota_monthly_cost_usd FROM api_keys"
            )
        }
        quotas = await _quotas(conn)
        statuses = {
            row["id"]: row["status"] for row in await conn.fetch("SELECT id, status FROM users")
        }
        owners = {
            row["id"]: row["user_id"]
            for row in await conn.fetch("SELECT id, user_id FROM projects")
        }
        leftover = await conn.fetchval(
            "SELECT COUNT(*) FROM organizations WHERE budget_daily_usd IS NOT NULL "
            "OR budget_monthly_usd IS NOT NULL OR status <> 'active'"
        )

    # The account quota has a home of its own, keys or no keys.
    assert quotas == {"u-budget": (3, 30), "u-keyless": (1, 2), "u-plain": (7, None)}
    # The keys carry the organization's budget too, revoked ones included.
    assert keys["h-live"] == (3, 30)
    assert keys["h-revoked"] == (3, 30)
    assert keys["h-plain"] == (7, None)
    assert statuses["u-suspended"] == "suspended"
    assert statuses["u-plain"] == "active"
    assert owners == {default_project_id(user): user for user in statuses}
    # Nothing is left for the previous release to enforce on top.
    assert leftover == 0


async def test_an_organization_concurrency_cap_stops_the_migration(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-capped", cap=2)

    with pytest.raises(Exception, match="org_p_u-capped"):
        await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        # Nothing ran: the cap still applies under the previous release.
        assert (
            await conn.fetchval(
                "SELECT max_concurrent_requests FROM organizations WHERE id = 'org_p_u-capped'"
            )
            == 2
        )
        assert not await conn.fetchval("SELECT to_regclass('account_quotas') IS NOT NULL")


async def test_the_previous_release_keeps_working_after_it_runs(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await _personal_account(conn, "u-old")
        await _key(conn, "h-old", "u-old", daily=7)
    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        # How the previous release provisions an account and a project ...
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u-new', 'new@x.test', 'h')"
        )
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u-new', 'n', 'personal')"
        )
        await conn.execute(
            "INSERT INTO projects (id, org_id, name, is_default) "
            "VALUES ('proj_d_u-new', 'org_p_u-new', 'Default', TRUE)"
        )
        await conn.execute(
            "INSERT INTO projects (id, org_id, name) VALUES ('proj_old', 'org_p_u-old', 'CI')"
        )
        # ... and how it finds them: by organization id, which this release still writes.
        found = await conn.fetchval(
            "SELECT COUNT(*) FROM projects WHERE org_id = 'org_p_u-old' AND status = 'active'"
        )
        # This release finds them by account.
        owners = {
            row["id"]: row["user_id"]
            for row in await conn.fetch(
                "SELECT id, user_id FROM projects WHERE id IN ('proj_d_u-new', 'proj_old')"
            )
        }
        # An account quota the previous release writes reaches the account; the
        # organization keeps none, so that release enforces it from the keys.
        await conn.execute("UPDATE organizations SET budget_daily_usd = 2 WHERE id = 'org_p_u-old'")
        await conn.execute(
            "UPDATE organizations SET budget_monthly_usd = 9 WHERE id = 'org_p_u-new'"
        )
        quotas = await _quotas(conn)
        org_budgets = await conn.fetchval(
            "SELECT COUNT(*) FROM organizations "
            "WHERE budget_daily_usd IS NOT NULL OR budget_monthly_usd IS NOT NULL"
        )
        await conn.execute("UPDATE organizations SET status = 'suspended' WHERE id = 'org_p_u-new'")
        suspended = await conn.fetchval("SELECT status FROM users WHERE id = 'u-new'")
        with pytest.raises(Exception, match="concurrency caps"):
            await conn.execute(
                "UPDATE organizations SET max_concurrent_requests = 3 WHERE id = 'org_p_u-old'"
            )

    assert found == 2
    assert owners == {"proj_d_u-new": "u-new", "proj_old": "u-old"}
    assert quotas["u-old"] == (2, None)
    assert quotas["u-new"] == (None, 9)
    assert org_budgets == 0
    assert suspended == "suspended"


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
        key_project = await conn.fetchval(
            "SELECT project_id FROM api_keys WHERE key_hash = 'h-bare'"
        )
        placeholder = await conn.fetchrow(
            "SELECT budget_daily_usd, budget_monthly_usd, max_concurrent_requests, status "
            "FROM organizations WHERE id = 'org_p_u-bare'"
        )

    assert first == 1
    assert second == 0
    assert projects == {
        "proj_d_u-bare": ("u-bare", "Default", True),
        "proj_d_u-old": ("u-old", "Default", True),
    }
    assert key_project == "proj_d_u-bare"
    # The row the previous release needs to find the project holds nothing.
    assert dict(placeholder) == {
        "budget_daily_usd": None,
        "budget_monthly_usd": None,
        "max_concurrent_requests": None,
        "status": "active",
    }
