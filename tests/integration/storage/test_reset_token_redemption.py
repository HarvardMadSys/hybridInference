"""Spending a password reset link is one transaction (Postgres).

``redeem_reset_token`` sets the new password, retires every link the account
holds and ends its sessions together. Of any number of concurrent redemptions
of one account's links -- the same link, or sibling links -- exactly one
resets the password, and a failure part-way leaves everything as it was.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_operational_store
from serving.servers.routers import auth_routes
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils import password as password_utils
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: How many redemptions race in the concurrency cases.
RACERS = 4


@pytest.fixture
def accounts() -> list[str]:
    """The synthetic accounts a test made, whose links and sessions are removed after it."""
    return []


@pytest_asyncio.fixture
async def pool(accounts):
    """A pool with room for every racer and the connection holding them back."""
    pool = await postgres.create_pool(min_size=1, max_size=RACERS + 3)
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            for table in ("password_reset_tokens", "auth_sessions"):
                await conn.execute(f"DELETE FROM {table} WHERE user_id = ANY($1::text[])", accounts)
        await pool.close()


@pytest_asyncio.fixture
async def store(pool) -> PostgresOperationalStore:
    return PostgresOperationalStore(pool)


async def _account(
    store: PostgresOperationalStore, accounts: list[str], *, sessions: int = 2
) -> str:
    tag = uuid.uuid4().hex[:12]
    user_id = f"u-reset-{tag}"
    accounts.append(user_id)
    await store.create_user(
        user_id=user_id,
        email=f"reset-{tag}@example.test",
        password_hash="hash-before-reset",
        email_verified=True,
    )
    for _ in range(sessions):
        sid = uuid.uuid4().hex
        await store.create_session(
            session_id=f"sess_{sid}",
            user_id=user_id,
            refresh_token_hash=f"refresh-{sid}",
            jti=f"jti-{sid}",
            sid=f"sid-{sid}",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    return user_id


async def _link(
    store: PostgresOperationalStore, user_id: str, *, expires_in: timedelta = timedelta(hours=1)
) -> str:
    token = f"link-{uuid.uuid4().hex}"
    await store.create_reset_token(
        token=token, user_id=user_id, expires_at=datetime.now(UTC) + expires_in
    )
    return token


async def _state(pool: Any, user_id: str) -> tuple[str | None, list[str], int]:
    """The account's password hash, its reset links and how many sessions it holds."""
    async with pool.acquire() as conn:
        password_hash = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1", user_id
        )
        links = await conn.fetch(
            "SELECT token FROM password_reset_tokens WHERE user_id = $1 ORDER BY token", user_id
        )
        sessions = await conn.fetchval(
            "SELECT count(*) FROM auth_sessions WHERE user_id = $1", user_id
        )
    return password_hash, [row["token"] for row in links], sessions


async def _race(pool: Any, user_id: str, redemptions: list[Any]) -> list[Any]:
    """Run *redemptions* together, all of them reaching the account lock before any passes it.

    The account row is held from another connection until every redemption is
    waiting for it, so they all overlap however the scheduler runs them. The
    waiters are counted from a third connection, outside any transaction:
    inside one, ``pg_stat_activity`` keeps answering from its first snapshot.
    """
    async with pool.acquire() as holder, pool.acquire() as observer:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", user_id)
        tasks = [asyncio.create_task(redemption) for redemption in redemptions]
        try:
            async with asyncio.timeout(15):
                while await observer.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query = 'SELECT id FROM users WHERE id = $1 FOR UPDATE'"
                ) < len(tasks):
                    await asyncio.sleep(0.02)
        except BaseException:
            for task in tasks:
                task.cancel()
            await transaction.rollback()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        await transaction.commit()
        return await asyncio.gather(*tasks)


async def test_a_live_link_resets_the_password_retires_every_link_and_ends_sessions(
    store, pool, accounts
):
    owner = await _account(store, accounts)
    link = await _link(store, owner)
    sibling = await _link(store, owner)
    bystander = await _account(store, accounts, sessions=1)
    bystander_link = await _link(store, bystander)

    redemption = await store.redeem_reset_token(link, password_hash="hash-after-reset")

    assert redemption == ("reset", owner)
    assert await _state(pool, owner) == ("hash-after-reset", [], 0)
    # Another account is left exactly as it was.
    assert await _state(pool, bystander) == ("hash-before-reset", [bystander_link], 1)
    # Spent, with its sibling: neither link resets anything again.
    for spent in (link, sibling):
        assert (await store.redeem_reset_token(spent, password_hash="again")).outcome == "invalid"
    assert (await _state(pool, owner))[0] == "hash-after-reset"


@pytest.mark.parametrize("case", ["unknown", "expired", "used", "account_gone"])
async def test_a_refused_link_changes_nothing(store, pool, accounts, case):
    owner = await _account(store, accounts)
    if case == "unknown":
        await _link(store, owner)
        token, expected = "link-that-was-never-sent", "invalid"
    elif case == "expired":
        token, expected = await _link(store, owner, expires_in=-timedelta(minutes=1)), "expired"
    elif case == "used":
        # A link marked used by an earlier release, which set ``used_at``.
        token, expected = await _link(store, owner), "used"
        await store.mark_reset_used(token)
    else:
        orphan = f"u-reset-gone-{uuid.uuid4().hex[:12]}"
        accounts.append(orphan)
        token, expected = await _link(store, orphan), "invalid"
    before = await _state(pool, owner)

    redemption = await store.redeem_reset_token(token, password_hash="hash-after-reset")

    assert redemption == (expected, None)
    assert await _state(pool, owner) == before


async def test_concurrent_redemptions_of_one_link_reset_the_password_once(store, pool, accounts):
    owner = await _account(store, accounts)
    link = await _link(store, owner)
    hashes = [f"hash-from-request-{n}" for n in range(RACERS)]

    results = await _race(
        pool, owner, [store.redeem_reset_token(link, password_hash=h) for h in hashes]
    )

    outcomes = [result.outcome for result in results]
    assert sorted(outcomes) == ["invalid"] * (RACERS - 1) + ["reset"]
    winner = hashes[outcomes.index("reset")]
    assert await _state(pool, owner) == (winner, [], 0)


async def test_concurrent_redemptions_of_sibling_links_reset_the_password_once(
    store, pool, accounts
):
    owner = await _account(store, accounts)
    links = [await _link(store, owner) for _ in range(RACERS)]
    hashes = [f"hash-from-link-{n}" for n in range(RACERS)]

    results = await _race(
        pool,
        owner,
        [store.redeem_reset_token(t, password_hash=h) for t, h in zip(links, hashes, strict=True)],
    )

    outcomes = [result.outcome for result in results]
    assert sorted(outcomes) == ["invalid"] * (RACERS - 1) + ["reset"]
    winner = hashes[outcomes.index("reset")]
    assert await _state(pool, owner) == (winner, [], 0)


class _FailingConnection:
    """A connection whose statements containing *fragment* fail; everything else is real."""

    def __init__(self, conn: Any, fragment: str) -> None:
        self._conn = conn
        self._fragment = fragment

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: str, *args: Any) -> Any:
        if self._fragment in query:
            raise RuntimeError("the database went away mid-reset")
        return await self._conn.execute(query, *args)


class _FailingPool:
    def __init__(self, pool: Any, fragment: str) -> None:
        self._pool = pool
        self._fragment = fragment

    def acquire(self) -> Any:
        pool, fragment = self._pool, self._fragment

        class _Acquire:
            async def __aenter__(self) -> _FailingConnection:
                self._context = pool.acquire()
                return _FailingConnection(await self._context.__aenter__(), fragment)

            async def __aexit__(self, *exc: Any) -> Any:
                return await self._context.__aexit__(*exc)

        return _Acquire()


@pytest.mark.parametrize(
    "failing_step",
    ["DELETE FROM password_reset_tokens", "DELETE FROM auth_sessions"],
)
async def test_a_reset_that_fails_part_way_leaves_everything_as_it_was(
    store, pool, accounts, failing_step
):
    owner = await _account(store, accounts)
    link = await _link(store, owner)
    sibling = await _link(store, owner)
    before = await _state(pool, owner)
    failing = PostgresOperationalStore(_FailingPool(pool, failing_step))

    with pytest.raises(RuntimeError, match="mid-reset"):
        await failing.redeem_reset_token(link, password_hash="hash-after-reset")

    # The password was written before the failure; it is rolled back with the rest.
    assert await _state(pool, owner) == before
    assert sorted(before[1]) == sorted([link, sibling])
    # The link was not spent: it still resets the password.
    assert (await store.redeem_reset_token(link, password_hash="hash-after-retry")).outcome == (
        "reset"
    )
    assert await _state(pool, owner) == ("hash-after-retry", [], 0)


async def test_two_requests_with_one_link_reset_the_password_once(store, pool, accounts):
    """The endpoint, through the cached store the gateway runs: one 200, one refusal."""
    owner = await _account(store, accounts)
    link = await _link(store, owner)
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_operational_store] = lambda: CachedOperationalStore(
        store, InMemoryCache()
    )
    passwords = ["Reset-Request-One-1", "Reset-Request-Two-2"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        responses = await asyncio.gather(
            *(
                client.post("/auth/reset-password", json={"token": link, "new_password": p})
                for p in passwords
            )
        )

    assert sorted(r.status_code for r in responses) == [200, 400]
    refused = next(r for r in responses if r.status_code == 400)
    assert refused.json()["detail"] == "Invalid or expired reset token."
    password_hash, links, sessions = await _state(pool, owner)
    assert (links, sessions) == ([], 0)
    winner = passwords[[r.status_code for r in responses].index(200)]
    loser = passwords[[r.status_code for r in responses].index(400)]
    assert password_utils.verify_password(winner, password_hash)
    assert not password_utils.verify_password(loser, password_hash)
