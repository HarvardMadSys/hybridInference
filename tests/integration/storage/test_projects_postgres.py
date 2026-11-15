"""Project storage against real Postgres: ownership, archiving and spend counters."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from serving.exceptions import ProjectNotFoundError
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.storage.project_backfill import (
    default_project_id,
    ensure_default_project,
    legacy_org_id,
)
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_USER = "u-projects-test"
_FOREIGN_USER = "u-projects-foreign"
_PROJECT_IDS = ("proj_eval_test", "proj_archive_test", "proj_auth_test")


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    operational = PostgresOperationalStore(pool)
    await operational.initialize()

    async def _wipe(conn) -> None:
        await conn.execute(
            "DELETE FROM spend_counters WHERE scope_id = ANY($1::text[])",
            [default_project_id(_USER), *_PROJECT_IDS],
        )
        await conn.execute("DELETE FROM api_keys WHERE user_id = $1", _USER)
        await conn.execute(
            "DELETE FROM projects WHERE user_id = ANY($1::text[])", [_USER, _FOREIGN_USER]
        )
        await conn.execute("DELETE FROM organizations WHERE id = $1", legacy_org_id(_USER))
        await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])", [_USER, _FOREIGN_USER])

    async with pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')",
            _USER,
            f"{_USER}@x.test",
        )
        await ensure_default_project(conn, user_id=_USER)

    try:
        yield operational
    finally:
        async with pool.acquire() as conn:
            await _wipe(conn)
        await pool.close()


async def test_a_new_key_lands_in_the_default_project(store):
    await store.create_key(
        key_hash="h-projects-default",
        key_prefix="hyi-def",
        user_id=_USER,
    )

    key = await store.get_auth_context_with_spend("h-projects-default")
    default = await store.get_default_project(_USER)
    assert key is not None
    assert key["project_id"] == default_project_id(_USER)
    assert default is not None
    assert default["id"] == default_project_id(_USER)
    async with store._pool.acquire() as conn:
        # The row a rolled-back release needs: no settings of its own, and
        # the account's quota, which that release enforces from it.
        placeholder = await conn.fetchrow(
            "SELECT budget_daily_usd, budget_monthly_usd, max_concurrent_requests, status "
            "FROM organizations WHERE id = $1",
            legacy_org_id(_USER),
        )
    assert dict(placeholder) == {
        "budget_daily_usd": key["quota_daily_cost_usd"],
        "budget_monthly_usd": None,
        "max_concurrent_requests": None,
        "status": "active",
    }


async def test_a_named_project_can_be_created_updated_and_lists_its_spend(store):
    project_id = "proj_eval_test"
    created = await store.create_project(
        project_id=project_id,
        user_id=_USER,
        name="Eval",
        budget_daily_usd=5,
        allowed_models=["m-1"],
    )

    assert created["name"] == "Eval"
    assert created["allowed_models"] == ["m-1"]

    updated = await store.update_project(_USER, project_id, {"budget_daily_usd": 10})
    assert float(updated["budget_daily_usd"]) == 10.0

    await store.increment_user_cost(_USER, 1.25, project_id=project_id)
    today, month = await store.get_project_spend(project_id)
    assert today == pytest.approx(1.25)
    assert month == pytest.approx(1.25)

    listed = await store.list_projects(_USER)
    assert {row["id"] for row in listed} == {default_project_id(_USER), project_id}


async def test_archiving_a_project_revokes_its_keys_and_not_the_default(store):
    project_id = "proj_archive_test"
    await store.create_project(project_id=project_id, user_id=_USER, name="Archive me")
    await store.create_key(
        key_hash="h-projects-archive",
        key_prefix="hyi-arc",
        user_id=_USER,
        project_id=project_id,
    )
    await store.create_key(key_hash="h-projects-keep", key_prefix="hyi-kep", user_id=_USER)

    revoked = await store.archive_project(_USER, project_id)

    assert revoked == 1
    archived = await store.get_auth_context_with_spend("h-projects-archive")
    kept = await store.get_auth_context_with_spend("h-projects-keep")
    assert archived is None
    assert kept is not None
    assert await store.get_project(_USER, project_id) is None

    with pytest.raises(ValueError):
        await store.archive_project(_USER, default_project_id(_USER))


async def test_the_account_budget_is_the_key_quota(store):
    await store.create_key(
        key_hash="h-projects-legacy",
        key_prefix="hyi-leg",
        user_id=_USER,
        quota_daily_cost_usd=42,
        quota_monthly_cost_usd=420,
    )

    daily, monthly = await store.get_account_budgets(_USER)

    assert daily == pytest.approx(42)
    assert monthly == pytest.approx(420)


async def test_the_rest_of_the_auth_identity_carries_the_project(store):
    project_id = "proj_auth_test"
    await store.create_project(
        project_id=project_id,
        user_id=_USER,
        name="Auth",
        budget_daily_usd=3,
        allowed_models=["m-1"],
    )
    await store.create_key(
        key_hash="h-projects-auth",
        key_prefix="hyi-aut",
        user_id=_USER,
        project_id=project_id,
    )

    identity = await store.get_auth_context_with_spend("h-projects-auth")

    assert identity is not None
    assert identity["project_id"] == project_id
    assert identity["project_name"] == "Auth"
    assert float(identity["project_budget_daily_usd"]) == 3.0
    assert identity["project_allowed_models"] == ["m-1"]
    assert "org_id" not in identity


async def test_api_logs_carry_the_project_and_key(store):
    from serving.storage.postgres_log import PostgresLogStore

    log_store = PostgresLogStore(store._pool, store_full_prompts=False)
    await log_store.log_request(
        request_id="req-projects-test",
        model_id="m-1",
        provider="local",
        prompt=[],
        response=None,
        usage=None,
        latency_ms=1,
        status_code=200,
        metadata={"user_id": _USER, "api_key_id": 7, "project_id": "proj_eval_test"},
    )
    async with store._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key_id, project_id FROM api_logs WHERE request_id = 'req-projects-test'"
        )
        await conn.execute("DELETE FROM api_logs WHERE request_id = 'req-projects-test'")
    assert row["key_id"] == 7
    assert row["project_id"] == "proj_eval_test"


async def test_hard_delete_erases_personal_projects_and_their_counters(store):
    await store.create_key(key_hash="h-projects-erase", key_prefix="hyi-era", user_id=_USER)
    await store.increment_user_cost(_USER, 1, project_id=default_project_id(_USER))
    async with store._pool.acquire() as conn:
        # An older release named the account's organization after the account.
        await conn.execute(
            "UPDATE organizations SET name = 'Project Tester' WHERE id = $1",
            legacy_org_id(_USER),
        )
    await store.delete_user(_USER)
    claim = await store.begin_hard_delete_user(_USER)

    await store.hard_delete_user(_USER, claim_token=claim)

    async with store._pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT 1 FROM organizations WHERE id = $1", legacy_org_id(_USER)
        )
        assert not await conn.fetchval("SELECT 1 FROM projects WHERE user_id = $1", _USER)
        assert not await conn.fetchval(
            "SELECT 1 FROM spend_counters WHERE scope_id = $1", default_project_id(_USER)
        )


async def test_grant_context_uses_default_project_even_with_a_named_project_key(store):
    await store.create_project(project_id="proj_auth_test", user_id=_USER, name="Named")
    await store.create_key(
        key_hash="h-projects-grant",
        key_prefix="hyi-grant",
        user_id=_USER,
        project_id="proj_auth_test",
    )
    default_id = default_project_id(_USER)
    await store.update_project(
        _USER,
        default_id,
        {
            "allowed_models": ["m-1"],
            "budget_daily_usd": 2,
        },
    )
    await store.increment_user_cost(_USER, 0.5, project_id=default_id)
    context = (await store.get_quota_context_for_user(_USER))[0]
    assert context["project_id"] == default_id
    assert context["project_allowed_models"] == ["m-1"]
    assert float(context["project_spent_today_usd"]) == pytest.approx(0.5)
    assert float(context["project_budget_daily_usd"]) == 2
    key = await store.get_auth_context_with_spend("h-projects-grant")
    assert key["project_id"] == "proj_auth_test"


async def test_default_is_provisioned_before_startup_backfill(store):
    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM projects WHERE user_id = $1", _USER)
    project = await store.get_default_project(_USER)
    assert project["id"] == default_project_id(_USER)
    await store.create_key(key_hash="h-projects-lazy", key_prefix="hyi-lazy", user_id=_USER)
    key = await store.get_auth_context_with_spend("h-projects-lazy")
    assert key["project_id"] == project["id"]
    assert await store.get_default_project(_USER) == project


@pytest.mark.parametrize("state", ["foreign", "archived", "missing"])
async def test_key_insertion_revalidates_project_ownership_and_status(store, state):
    project_id = "proj_auth_test"
    if state != "missing":
        await store.create_project(project_id=project_id, user_id=_USER, name="Bound")
        async with store._pool.acquire() as conn:
            if state == "archived":
                await conn.execute(
                    "UPDATE projects SET status = 'archived' WHERE id = $1", project_id
                )
            else:
                await conn.execute(
                    "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')",
                    _FOREIGN_USER,
                    f"{_FOREIGN_USER}@x.test",
                )
                await conn.execute(
                    "UPDATE projects SET user_id = $2 WHERE id = $1", project_id, _FOREIGN_USER
                )
    with pytest.raises(ProjectNotFoundError):
        await store.create_key(
            key_hash="h-projects-invalid",
            key_prefix="hyi-invalid",
            user_id=_USER,
            project_id=project_id,
        )
    assert await store.count_active_keys(_USER) == 0


async def test_account_quota_edits_reach_the_key_and_grant_identities(store):
    await store.create_key(key_hash="h-quota-edit", key_prefix="hyi-qed", user_id=_USER)
    await store.update_key(_USER, quota_daily_cost_usd=2, quota_monthly_cost_usd=20)
    identity = await store.get_auth_context_with_spend("h-quota-edit")
    grant_identity = (await store.get_quota_context_for_user(_USER))[0]
    for row in (identity, grant_identity):
        assert float(row["quota_daily_cost_usd"]) == 2
        assert float(row["quota_monthly_cost_usd"]) == 20


async def test_archive_revokes_suspended_keys_and_auth_rejects_archived_projects(store):
    project_id = "proj_archive_test"
    await store.create_project(project_id=project_id, user_id=_USER, name="Archive suspended")
    await store.create_key(
        key_hash="h-suspended-archive", key_prefix="hyi-sar", user_id=_USER, project_id=project_id
    )
    async with store._pool.acquire() as conn:
        await conn.execute("UPDATE api_keys SET status = 'suspended' WHERE user_id = $1", _USER)
    assert await store.archive_project(_USER, project_id) == 1
    await store.update_key(_USER, status="active")
    assert await store.get_auth_context_with_spend("h-suspended-archive") is None
    async with store._pool.acquire() as conn:
        await conn.execute("UPDATE api_keys SET status = 'active' WHERE user_id = $1", _USER)
    assert await store.get_auth_context_with_spend("h-suspended-archive") is None
    assert await store.get_auth_context_lightweight("h-suspended-archive") is None


@pytest.mark.parametrize("fenced", [False, True])
async def test_coalesced_cost_write_attributes_project_and_honors_erasure(store, fenced):
    from serving.storage.base import CostIncrement
    from serving.storage.erasure_fence import fence_account_digest

    secret = "project-cost-fence-test" if fenced else None
    writer = PostgresOperationalStore(store._pool, fence_secret=secret)
    project_id = default_project_id(_USER)
    increment = CostIncrement(_USER, "2026-11-08", None, 0.25, 1, project_id)
    await writer.increment_user_costs([increment, increment])
    async with store._pool.acquire() as conn:
        assert (
            float(
                await conn.fetchval(
                    "SELECT cost_usd FROM spend_counters WHERE scope_id = $1 AND period = 'm:2026-11'",
                    project_id,
                )
            )
            == 0.5
        )
        if fenced:
            digest = fence_account_digest(_USER, secret)
            await conn.execute(
                "INSERT INTO erasure_fence (account_digest) VALUES ($1) ON CONFLICT DO NOTHING",
                digest,
            )
    if fenced:
        try:
            await writer.increment_user_costs([increment])
            async with store._pool.acquire() as conn:
                assert (
                    float(
                        await conn.fetchval(
                            "SELECT cost_usd FROM spend_counters WHERE scope_id = $1 AND period = 'm:2026-11'",
                            project_id,
                        )
                    )
                    == 0.5
                )
        finally:
            async with store._pool.acquire() as conn:
                await conn.execute("DELETE FROM erasure_fence WHERE account_digest = $1", digest)


async def test_an_admin_key_quota_becomes_the_account_quota(store):
    await store.create_key(
        key_hash="h-projects-admin-quota",
        key_prefix="hyi-adm",
        user_id=_USER,
        quota_daily_cost_usd=2,
        quota_monthly_cost_usd=None,
    )

    key = await store.get_auth_context_with_spend("h-projects-admin-quota")
    daily, monthly = await store.get_account_budgets(_USER)
    assert key is not None
    assert key["quota_daily_cost_usd"] == 2
    assert (daily, monthly) == (2.0, None)


async def test_a_revoked_keys_quota_stays_the_account_quota(store):
    await store.create_key(
        key_hash="h-projects-revoked-quota",
        key_prefix="hyi-rvq",
        user_id=_USER,
        quota_daily_cost_usd=3,
    )
    await store.revoke_key(_USER)

    daily, _ = await store.get_account_budgets(_USER)
    assert daily == 3.0


async def test_an_active_project_name_is_unique_per_account(store):
    import asyncpg

    from serving.servers.routers.projects import _is_duplicate_name

    await store.create_project(project_id="proj_eval_test", user_id=_USER, name="Eval")
    with pytest.raises(asyncpg.UniqueViolationError) as caught:
        await store.create_project(project_id="proj_archive_test", user_id=_USER, name="eval")
    assert _is_duplicate_name(caught.value)


async def test_revoking_keys_never_brings_back_a_quota_an_admin_replaced(store):
    """A newer revoked key still carries the old quota; the account's own record wins."""
    await store.create_key(key_hash="h-q-old", key_prefix="hyi-qol", user_id=_USER)
    await store.create_key(
        key_hash="h-q-new", key_prefix="hyi-qnw", user_id=_USER, inherit_account_quota=True
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET status = 'revoked', revoked_at = NOW() WHERE key_hash = 'h-q-new'"
        )
    await store.update_key(_USER, quota_daily_cost_usd=1, quota_monthly_cost_usd=2)
    await store.revoke_key(_USER)

    await store.create_key(
        key_hash="h-q-next", key_prefix="hyi-qnx", user_id=_USER, inherit_account_quota=True
    )

    key = await store.get_auth_context_with_spend("h-q-next")
    assert (float(key["quota_daily_cost_usd"]), float(key["quota_monthly_cost_usd"])) == (1, 2)
    assert await store.get_account_budgets(_USER) == (1.0, 2.0)


async def test_a_role_quota_reaches_an_account_without_keys(store):
    async with store._pool.acquire() as conn:
        role = await conn.fetchval("SELECT role FROM users WHERE id = $1", _USER)

    await store.apply_role_quota(role, Decimal(4))

    daily, _ = await store.get_account_budgets(_USER)
    assert daily == 4.0


async def test_every_key_keeps_the_organization_id_a_rolled_back_release_needs(store):
    """The previous release's backfill moves a key with no organization id to Default."""
    await store.create_project(project_id="proj_auth_test", user_id=_USER, name="Named")
    await store.create_key(
        key_hash="h-legacy-org", key_prefix="hyi-lgo", user_id=_USER, project_id="proj_auth_test"
    )
    async with store._pool.acquire() as conn:
        key = await conn.fetchrow(
            "SELECT org_id, project_id FROM api_keys WHERE key_hash = 'h-legacy-org'"
        )
        member = await conn.fetchval(
            "SELECT role FROM org_members WHERE org_id = $1 AND user_id = $2",
            legacy_org_id(_USER),
            _USER,
        )
    assert dict(key) == {"org_id": legacy_org_id(_USER), "project_id": "proj_auth_test"}
    assert member == "owner"


async def test_a_budget_set_before_the_first_key_applies_to_it(store):
    await store.update_key(_USER, quota_daily_cost_usd=2, quota_monthly_cost_usd=5)

    await store.create_key(
        key_hash="h-keyless-budget",
        key_prefix="hyi-klb",
        user_id=_USER,
        quota_daily_cost_usd=100,
        inherit_account_quota=True,
    )

    key = await store.get_auth_context_with_spend("h-keyless-budget")
    assert (float(key["quota_daily_cost_usd"]), float(key["quota_monthly_cost_usd"])) == (2, 5)


async def test_an_unset_daily_quota_leaves_the_first_key_its_default(store):
    """A monthly cap alone, set before any key, does not blank the daily quota."""
    await store.update_key(_USER, quota_monthly_cost_usd=5)

    await store.create_key(
        key_hash="h-keyless-monthly",
        key_prefix="hyi-klm",
        user_id=_USER,
        quota_daily_cost_usd=100,
        inherit_account_quota=True,
    )

    key = await store.get_auth_context_with_spend("h-keyless-monthly")
    assert (float(key["quota_daily_cost_usd"]), float(key["quota_monthly_cost_usd"])) == (100, 5)
    assert await store.get_account_budgets(_USER) == (100.0, 5.0)


async def test_requests_are_checked_against_the_record_not_a_keys_copy(store):
    """A key the previous release creates mid-rollout carries its role's default."""
    from serving.storage.postgres_operational import reconcile_key_quota_copies

    await store.create_key(
        key_hash="h-copy", key_prefix="hyi-cpy", user_id=_USER, quota_daily_cost_usd=2
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET quota_daily_cost_usd = 1000, quota_monthly_cost_usd = NULL "
            "WHERE key_hash = 'h-copy'"
        )

    key = await store.get_auth_context_with_spend("h-copy")
    grant = (await store.get_quota_context_for_user(_USER))[0]
    assert float(key["quota_daily_cost_usd"]) == 2
    assert float(grant["quota_daily_cost_usd"]) == 2

    async with store._pool.acquire() as conn:
        assert await reconcile_key_quota_copies(conn) == 1
        assert await reconcile_key_quota_copies(conn) == 0
        copy = await conn.fetchval(
            "SELECT quota_daily_cost_usd FROM api_keys WHERE key_hash = 'h-copy'"
        )
    assert float(copy) == 2


async def test_a_quota_edit_reaches_the_organization(store):
    """The organization is where a rolled-back release enforces the account quota from."""
    await store.create_key(key_hash="h-org-a", key_prefix="hyi-oga", user_id=_USER)

    await store.update_key(_USER, quota_daily_cost_usd=1, quota_monthly_cost_usd=2)

    async with store._pool.acquire() as conn:
        org = await conn.fetchrow(
            "SELECT budget_daily_usd, budget_monthly_usd FROM organizations WHERE id = $1",
            legacy_org_id(_USER),
        )
    assert (float(org["budget_daily_usd"]), float(org["budget_monthly_usd"])) == (1, 2)
