"""Binding a verified email or phone never takes another account's identity.

``bind_user_email`` / ``bind_user_phone`` write without checking first: the
unique constraint on ``users`` is what refuses an address another account
holds. That refusal had only run with the store mocked. Emails are stored
lowercased and the constraint compares exactly, so a differently cased copy of
a taken address must be refused too.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def accounts():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-bind-%'")

    await _wipe()
    for user_id in ("u-bind-owner", "u-bind-other"):
        await store.create_user(
            user_id=user_id, email=f"{user_id}@bind.test", password_hash="x", status="active"
        )
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _identity(pool: Any, user_id: str) -> tuple[Any, ...]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, email_verified, phone FROM users WHERE id = $1", user_id
        )
    return tuple(row)


async def test_a_free_email_is_bound_lowercased_and_verified(accounts):
    store, pool = accounts

    assert await store.bind_user_email("u-bind-owner", "  New.Address@Bind.TEST ") is True

    assert await _identity(pool, "u-bind-owner") == ("new.address@bind.test", True, None)


@pytest.mark.parametrize("taken", ["u-bind-other@bind.test", "U-Bind-Other@BIND.test"])
async def test_an_email_another_account_holds_is_refused(accounts, taken):
    store, pool = accounts
    before = (await _identity(pool, "u-bind-owner"), await _identity(pool, "u-bind-other"))

    assert await store.bind_user_email("u-bind-owner", taken) is False

    assert (await _identity(pool, "u-bind-owner"), await _identity(pool, "u-bind-other")) == before


async def test_a_phone_another_account_holds_is_refused(accounts):
    store, pool = accounts
    assert await store.bind_user_phone("u-bind-other", "+15550100") is True

    assert await store.bind_user_phone("u-bind-owner", "+15550100") is False

    assert (await _identity(pool, "u-bind-owner"))[2] is None
    assert (await _identity(pool, "u-bind-other"))[2] == "+15550100"


@pytest.mark.parametrize("status", ["suspended", "deleted", "pending_approval"])
async def test_an_account_that_is_not_active_binds_nothing(accounts, status):
    store, pool = accounts
    await store.update_user_fields("u-bind-owner", status=status)

    assert await store.bind_user_email("u-bind-owner", "fresh@bind.test") is False

    assert (await _identity(pool, "u-bind-owner"))[0] == "u-bind-owner@bind.test"
