"""Service accounts against PostgreSQL: one transaction, and the unique login name.

What a mock cannot show: that the account, its role, its Default project and
its first key are written together or not at all, so a failure part way
leaves nothing to block a retry; and that of two creates racing on one login
name the unique index admits one and the other is told the name is taken.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from serving.exceptions import LoginNameTakenError
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = "u-svcacct-"
LOGIN_PREFIX = "svcacct-"


async def _cleanup(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys WHERE key_hash LIKE 'svcacct-%'")
        await conn.execute("DELETE FROM api_keys WHERE user_id LIKE $1", f"{PREFIX}%")
        await conn.execute("DELETE FROM account_quotas WHERE user_id LIKE $1", f"{PREFIX}%")
        await conn.execute("DELETE FROM projects WHERE user_id LIKE $1", f"{PREFIX}%")
        await conn.execute("DELETE FROM users WHERE id LIKE $1", f"{PREFIX}%")


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    op_store = PostgresOperationalStore(pool)
    await op_store.initialize()
    await _cleanup(pool)
    try:
        yield op_store
    finally:
        await _cleanup(pool)
        await pool.close()


def _create(store: PostgresOperationalStore, n: str, *, login: str, key_hash: str | None = None):
    return store.create_service_account(
        user_id=f"{PREFIX}{n}",
        login_name=login,
        password_hash="unknowable",
        user_name=f"Bot {n}",
        role="pro",
        key_hash=key_hash or f"svcacct-hash-{n}",
        key_prefix=f"hyi-svc-{n}"[:12],
        quota_daily_cost_usd=Decimal("12.50"),
    )


async def test_the_account_its_role_project_and_key_are_written_together(store):
    row = await _create(store, "one", login=f"{LOGIN_PREFIX}One")
    assert row["id"] is not None

    user = await store.get_user_by_login_name(f"{LOGIN_PREFIX}one")
    assert user is not None
    assert user["id"] == f"{PREFIX}one"
    assert user["login_name"] == f"{LOGIN_PREFIX}one"
    assert user["email"] is None
    assert user["role"] == "pro"
    assert user["status"] == "active"
    assert user["is_service_account"] is True

    project = await store.get_default_project(f"{PREFIX}one")
    assert project is not None
    keys = await store.list_account_keys(f"{PREFIX}one")
    assert len(keys) == 1
    assert keys[0]["project_id"] == project["id"]
    async with store._pool.acquire() as conn:
        quota = await conn.fetchval(
            "SELECT quota_daily_cost_usd FROM api_keys WHERE user_id = $1", f"{PREFIX}one"
        )
    assert quota == Decimal("12.50")


async def test_a_failure_part_way_leaves_nothing_behind(store):
    # A key hash some other key already holds fails the key insert, after the
    # account row was written in the same transaction.
    await _create(store, "first", login=f"{LOGIN_PREFIX}first", key_hash="svcacct-dup")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _create(store, "second", login=f"{LOGIN_PREFIX}second", key_hash="svcacct-dup")

    assert await store.get_user_by_login_name(f"{LOGIN_PREFIX}second") is None
    assert await store.get_user_by_id(f"{PREFIX}second") is None
    # So a retry with the same login name goes through.
    await _create(store, "second", login=f"{LOGIN_PREFIX}second")
    assert len(await store.list_account_keys(f"{PREFIX}second")) == 1


async def test_a_taken_login_name_is_refused_case_insensitively(store):
    await _create(store, "taken", login=f"{LOGIN_PREFIX}taken")
    with pytest.raises(LoginNameTakenError) as refused:
        await _create(store, "again", login=f"{LOGIN_PREFIX}TAKEN")
    assert refused.value.login_name == f"{LOGIN_PREFIX}taken"
    assert await store.get_user_by_id(f"{PREFIX}again") is None


async def test_two_concurrent_creates_of_one_name_admit_exactly_one(store):
    results = await asyncio.gather(
        *(_create(store, f"race-{i}", login=f"{LOGIN_PREFIX}race") for i in range(4)),
        return_exceptions=True,
    )
    created = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(created) == 1
    assert len(refused) == 3
    assert all(isinstance(r, LoginNameTakenError) for r in refused), refused
    winner = await store.get_user_by_login_name(f"{LOGIN_PREFIX}race")
    assert winner is not None
    assert len(await store.list_account_keys(winner["id"])) == 1
