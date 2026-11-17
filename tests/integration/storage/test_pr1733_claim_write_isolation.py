"""A write attempted during an active claim cannot interfere with erasure.

The claim guard refuses the write, so the question this file answers is
narrower and stronger: even if a writer *did* reach the row while a claim is
active, the erasure must still complete and still delete the account.

Two shapes are exercised, because they are the two that could differ:

- a write that races the erasure, repeatedly, while the claim is live;
- a write attempted after the claim has gone stale (the lease outlived the
  recovery grace) but before the takeover, which is the window where a
  stalled claim sits.

The erasure's own correctness does not depend on the writer: the final purge
is token-scoped and runs under the row lock, so it deletes the account
whatever else touched the row.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=12)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def store(pool):
    return PostgresOperationalStore(pool)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _seed_deleted(pool, uid: str) -> None:
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=FALSE, "
        "hard_delete_claim_token=NULL, hard_delete_claimed_at=NULL, "
        "hard_delete_claim_recovered=FALSE",
        uid,
        f"{uid}@example.com",
    )


async def test_a_racing_write_cannot_stop_the_erasure(store, pool):
    """A writer hammering the row while the claim is live does not stop it."""
    for trial in range(5):
        uid = _uid(f"race-write{trial}")
        await _seed_deleted(pool, uid)
        claim = await store.begin_hard_delete_user(uid)

        stop = asyncio.Event()

        async def _writer(uid=uid, trial=trial, stop=stop):
            refused = 0
            while not stop.is_set():
                try:
                    await store.update_user_fields(uid, email=f"moved{trial}@example.com")
                except HardDeleteStateChanged:
                    refused += 1
                await asyncio.sleep(0)
            return refused

        writer = asyncio.create_task(_writer())
        await asyncio.sleep(0.05)
        counts = await store.hard_delete_user(uid, claim_token=claim.token)
        stop.set()
        refused = await writer

        assert counts["users"] == 1
        assert refused > 0, "the writer was never refused"
        assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None


async def test_a_write_during_a_stalled_claim_cannot_stop_the_erasure(store, pool):
    """The stale-lease window does not admit writes either."""
    uid = _uid("stalled-write")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    # The lease outlived the recovery grace: the claim sits, unrenewed.
    await pool.execute(
        "UPDATE users SET hard_delete_claimed_at = NOW() - make_interval(mins => 90) WHERE id = $1",
        uid,
    )

    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, email="moved@example.com")
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, role="admin")
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, preferences='{"theme":"dark"}')

    counts = await store.hard_delete_user(uid, claim_token=claim.token)
    assert counts["users"] == 1
    assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None


async def test_a_write_racing_the_takeover_cannot_strand_the_account(store, pool):
    """A writer racing the recovery takeover changes nothing."""
    uid = _uid("takeover-write")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await pool.execute(
        "UPDATE users SET hard_delete_claimed_at = NOW() - make_interval(mins => 90) WHERE id = $1",
        uid,
    )

    async def _writer():
        refused = 0
        for _ in range(20):
            try:
                await store.update_user_fields(uid, email="moved@example.com")
            except HardDeleteStateChanged:
                refused += 1
            await asyncio.sleep(0)
        return refused

    async def _takeover():
        from serving.storage.base import HardDeleteRecoveryMode

        return await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )

    refused, taken = await asyncio.gather(_writer(), _takeover())

    assert refused == 20, refused
    counts = await store.hard_delete_user(uid, claim_token=taken.token)
    assert counts["users"] == 1
    assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None
