"""Projects against real Postgres: CRUD, spend separation, and deletion."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_USER_PREFIX = "u-proj-"


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    operational = PostgresOperationalStore(pool)
    await operational.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-proj-%'")
            await conn.execute("DELETE FROM user_monthly_cost WHERE user_id LIKE 'u-proj-%'")
            await conn.execute("DELETE FROM api_keys WHERE user_id LIKE 'u-proj-%'")
            await conn.execute("DELETE FROM projects WHERE account_id LIKE 'u-proj-%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-proj-%'")

    await _wipe()
    try:
        yield operational
    finally:
        await _wipe()
        await pool.close()


async def _account(store: PostgresOperationalStore, suffix: str) -> str:
    user_id = f"{_USER_PREFIX}{suffix}"
    await store.create_user(
        user_id=user_id,
        email=f"{suffix}@proj.test",
        password_hash="x",
    )
    await store.update_user_fields(
        user_id,
        daily_cost_quota_usd=Decimal("100.00"),
        monthly_cost_quota_usd=Decimal("500.00"),
    )
    return user_id


async def test_crud_and_spend_are_kept_apart(store):
    account = await _account(store, "a")
    project = await store.create_project(
        project_id="prj_test_a",
        account_id=account,
        name="Research",
        daily_cost_quota_usd=Decimal("10.00"),
        monthly_cost_quota_usd=Decimal("50.00"),
        allowed_models=["model-a"],
    )
    assert project["name"] == "Research"

    listed = await store.list_projects(account)
    assert [row["id"] for row in listed] == ["prj_test_a"]

    updated = await store.update_project(
        account, "prj_test_a", name="Research 2", allowed_models=["model-a", "model-b"]
    )
    assert updated is not None
    assert updated["name"] == "Research 2"
    assert updated["allowed_models"] == ["model-a", "model-b"]

    await store.increment_user_cost(account, 3.5, project_id="prj_test_a")

    assert await store.get_project_cost("prj_test_a", "today") == pytest.approx(3.5)
    assert await store.get_project_cost("prj_test_a", "month") == pytest.approx(3.5)
    assert await store.get_user_cost_today(account) == pytest.approx(3.5)


async def test_a_global_request_does_not_charge_a_project(store):
    account = await _account(store, "b")
    await store.create_project(project_id="prj_test_b", account_id=account, name="Other")

    await store.increment_user_cost(account, 2.0, project_id="prj_test_b")
    await store.increment_user_cost(account, 1.0)

    assert await store.get_project_cost("prj_test_b", "today") == pytest.approx(2.0)
    assert await store.get_user_cost_today(account) == pytest.approx(3.0)


async def test_deleting_a_project_revokes_its_keys(store):
    account = await _account(store, "c")
    await store.create_project(project_id="prj_test_c", account_id=account, name="Doomed")
    await store.create_key(
        key_hash="h-proj-c",
        key_prefix="sk-proj-c",
        user_id=account,
        account_id=account,
        project_id="prj_test_c",
    )

    revoked = await store.delete_project(account, "prj_test_c")

    assert revoked == 1
    assert await store.list_projects(account) == []
    async with store._pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM api_keys WHERE key_hash = 'h-proj-c'")
    assert status == "revoked"


async def test_a_project_key_resolves_its_budgets_in_the_auth_context(store):
    account = await _account(store, "d")
    await store.create_project(
        project_id="prj_test_d",
        account_id=account,
        name="Capped",
        daily_cost_quota_usd=Decimal("5.00"),
        allowed_models=["model-a"],
    )
    await store.create_key(
        key_hash="h-proj-d",
        key_prefix="sk-proj-d",
        user_id=account,
        account_id=account,
        project_id="prj_test_d",
    )

    context = await store.get_auth_context_with_spend("h-proj-d")

    assert context is not None
    assert context["project_id"] == "prj_test_d"
    assert context["project_daily_cost_quota_usd"] == Decimal("5.00")
    assert context["project_allowed_models"] == ["model-a"]
    assert context["quota_daily_cost_usd"] == Decimal("100.00")


async def test_a_deleted_project_cannot_be_listed_or_updated(store):
    account = await _account(store, "e")
    await store.create_project(project_id="prj_test_e", account_id=account, name="Gone")
    await store.delete_project(account, "prj_test_e")

    assert await store.get_project(account, "prj_test_e") is None
    assert await store.update_project(account, "prj_test_e", name="Nope") is None
    assert await store.delete_project(account, "prj_test_e") == 0


async def test_names_are_unique_per_account(store):
    first = await _account(store, "f")
    second = await _account(store, "g")
    await store.create_project(project_id="prj_test_f", account_id=first, name="Same")

    await store.create_project(project_id="prj_test_g", account_id=second, name="Same")

    import asyncpg

    with pytest.raises(asyncpg.UniqueViolationError):
        await store.create_project(project_id="prj_test_f2", account_id=first, name="same")
