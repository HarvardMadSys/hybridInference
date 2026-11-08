"""Project storage against real Postgres: ownership, archiving and spend counters."""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.exceptions import ProjectNotFoundError
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.storage.project_backfill import (
    default_project_id,
    ensure_personal_org,
    personal_org_id,
)
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_USER = "u-projects-test"
_PROJECT_IDS = ("proj_eval_test", "proj_archive_test", "proj_auth_test")


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    operational = PostgresOperationalStore(pool)
    await operational.initialize()

    async def _wipe(conn) -> None:
        await conn.execute(
            "DELETE FROM spend_counters WHERE scope_id = $1 OR scope_id = ANY($2::text[])",
            personal_org_id(_USER),
            [default_project_id(_USER), *_PROJECT_IDS],
        )
        await conn.execute("DELETE FROM api_keys WHERE user_id = $1", _USER)
        await conn.execute("DELETE FROM projects WHERE org_id = $1", personal_org_id(_USER))
        await conn.execute("DELETE FROM org_members WHERE user_id = $1", _USER)
        await conn.execute("DELETE FROM organizations WHERE id = $1", personal_org_id(_USER))
        await conn.execute("DELETE FROM users WHERE id = $1", _USER)

    async with pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')",
            _USER,
            f"{_USER}@x.test",
        )
        await ensure_personal_org(conn, user_id=_USER, user_name="Project Tester")

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
    assert key["org_id"] == personal_org_id(_USER)
    assert default is not None
    assert default["id"] == default_project_id(_USER)


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

    await store.increment_user_cost(
        _USER,
        1.25,
        org_id=personal_org_id(_USER),
        project_id=project_id,
    )
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


async def test_the_account_budget_falls_back_to_the_legacy_key_quota(store):
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
    assert identity["org_id"] == personal_org_id(_USER)


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


async def test_a_suspended_organization_reaches_the_auth_context(store):
    await store.create_key(key_hash="h-projects-suspend", key_prefix="hyi-sus", user_id=_USER)

    await store.update_org(personal_org_id(_USER), {"status": "suspended"})

    identity = await store.get_auth_context_with_spend("h-projects-suspend")
    assert identity is not None
    assert identity["org_status"] == "suspended"
    assert await store.get_quota_context_for_user(_USER) == []

    await store.update_org(personal_org_id(_USER), {"status": "active"})


async def test_hard_delete_erases_personal_projects_and_their_counters(store):
    await store.create_key(key_hash="h-projects-erase", key_prefix="hyi-era", user_id=_USER)
    await store.increment_user_cost(
        _USER, 1, project_id=default_project_id(_USER), org_id=personal_org_id(_USER)
    )
    await store.delete_user(_USER)
    claim = await store.begin_hard_delete_user(_USER)

    await store.hard_delete_user(_USER, claim_token=claim)

    async with store._pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT 1 FROM organizations WHERE id = $1", personal_org_id(_USER)
        )
        assert not await conn.fetchval(
            "SELECT 1 FROM projects WHERE org_id = $1", personal_org_id(_USER)
        )
        assert not await conn.fetchval(
            "SELECT 1 FROM spend_counters WHERE scope_id = ANY($1::text[])",
            [personal_org_id(_USER), default_project_id(_USER)],
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
    await store.increment_user_cost(
        _USER,
        0.5,
        org_id=personal_org_id(_USER),
        project_id=default_id,
    )
    context = (await store.get_quota_context_for_user(_USER))[0]
    assert context["project_id"] == default_id
    assert context["project_allowed_models"] == ["m-1"]
    assert float(context["project_spent_today_usd"]) == pytest.approx(0.5)
    assert float(context["project_budget_daily_usd"]) == 2
    key = await store.get_auth_context_with_spend("h-projects-grant")
    assert key["project_id"] == "proj_auth_test"


async def test_default_is_provisioned_before_startup_backfill(store):
    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM organizations WHERE id = $1", personal_org_id(_USER))
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
                    "INSERT INTO organizations (id, name, kind) VALUES ('org_foreign_test', 'Foreign', 'personal')"
                )
                await conn.execute(
                    "UPDATE projects SET org_id = 'org_foreign_test' WHERE id = $1", project_id
                )
    try:
        with pytest.raises(ProjectNotFoundError):
            await store.create_key(
                key_hash="h-projects-invalid",
                key_prefix="hyi-invalid",
                user_id=_USER,
                project_id=project_id,
            )
        assert await store.count_active_keys(_USER) == 0
    finally:
        if state == "foreign":
            async with store._pool.acquire() as conn:
                await conn.execute("DELETE FROM organizations WHERE id = 'org_foreign_test'")


async def test_account_quota_edits_replace_the_personal_org_ceiling(store):
    await store.create_key(key_hash="h-quota-edit", key_prefix="hyi-qed", user_id=_USER)
    await store.update_org(
        personal_org_id(_USER), {"budget_daily_usd": 10, "budget_monthly_usd": 100}
    )
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
    increment = CostIncrement(
        _USER, "2026-11-08", None, 0.25, 1, personal_org_id(_USER), project_id
    )
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


async def test_an_admin_key_quota_becomes_the_organization_ceiling(store):
    await store.update_org(personal_org_id(_USER), {"budget_daily_usd": 10})

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


async def test_an_organization_ceiling_edit_reaches_the_accounts_keys(store):
    await store.create_key(
        key_hash="h-projects-org-edit",
        key_prefix="hyi-org",
        user_id=_USER,
        inherit_account_quota=True,
    )

    await store.update_org(personal_org_id(_USER), {"budget_daily_usd": 50})

    key = await store.get_active_key_by_account(_USER)
    auth = await store.get_auth_context_with_spend("h-projects-org-edit")
    daily, _ = await store.get_account_budgets(_USER)
    assert key is not None and key["quota_daily_cost_usd"] == 50
    assert auth is not None and auth["quota_daily_cost_usd"] == 50
    assert daily == 50.0
