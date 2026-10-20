"""Signing the last account out of a browser races a sign-in into it."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store():
    """Use a disposable schema, never another test's or deployment's rows."""
    admin = await postgres.connect(timeout=2)
    schema = f"browser_race_test_{uuid.uuid4().hex}"
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await postgres.create_pool(
            min_size=3, max_size=6, server_settings={"search_path": schema}
        )
        operational = PostgresOperationalStore(pool)
        await operational.initialize()
        yield operational
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def browser_with(store, user_id: str) -> str:
    browser_id = uuid.uuid4().hex
    await store.create_browser_session(
        browser_session_id=browser_id,
        secret_hash=uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert await store.attach_browser_session_account(
        browser_id, user_id=user_id, auth_session_id=f"{user_id}-session"
    )
    return browser_id


async def state(store, browser_id: str) -> tuple[bool, list[str]]:
    async with store._pool.acquire() as conn:
        revoked = await conn.fetchval(
            "SELECT revoked FROM browser_sessions WHERE id = $1", browser_id
        )
        members = await conn.fetch(
            "SELECT user_id FROM browser_session_accounts WHERE browser_session_id = $1 "
            "ORDER BY user_id",
            browser_id,
        )
    return revoked, [row["user_id"] for row in members]


async def test_last_sign_out_retires_the_browser_and_refuses_later_sign_ins(store):
    browser_id = await browser_with(store, "user-a")
    assert await store.detach_browser_session_account(browser_id, "user-a") == 0
    assert await state(store, browser_id) == (True, [])
    assert not await store.attach_browser_session_account(
        browser_id, user_id="user-b", auth_session_id="user-b-session"
    )
    assert await state(store, browser_id) == (True, [])


async def test_sign_out_keeps_a_browser_that_still_holds_an_account(store):
    browser_id = await browser_with(store, "user-a")
    assert await store.attach_browser_session_account(
        browser_id, user_id="user-b", auth_session_id="user-b-session"
    )
    assert await store.detach_browser_session_account(browser_id, "user-a") == 1
    assert await state(store, browser_id) == (False, ["user-b"])


@pytest.mark.parametrize("sign_in_first", [False, True])
async def test_racing_sign_in_is_never_left_inside_a_retired_browser(store, sign_in_first):
    browser_id = await browser_with(store, "user-a")
    async with store._pool.acquire() as holder:
        # Hold the browser row so both operations queue on it in a known order.
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM browser_sessions WHERE id = $1 FOR UPDATE", browser_id)

        async def sign_out():
            return await store.detach_browser_session_account(browser_id, "user-a")

        async def sign_in():
            return await store.attach_browser_session_account(
                browser_id, user_id="user-b", auth_session_id="user-b-session"
            )

        first, second = (sign_in, sign_out) if sign_in_first else (sign_out, sign_in)
        first_task = asyncio.create_task(first())
        await asyncio.sleep(0.2)
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0.2)
        await tx.commit()
        results = await asyncio.gather(first_task, second_task)

    revoked, members = await state(store, browser_id)
    if sign_in_first:
        attached, remaining = results
        assert attached is True
        assert remaining == 1
        assert (revoked, members) == (False, ["user-b"])
    else:
        remaining, attached = results
        assert remaining == 0
        assert attached is False
        assert (revoked, members) == (True, [])
