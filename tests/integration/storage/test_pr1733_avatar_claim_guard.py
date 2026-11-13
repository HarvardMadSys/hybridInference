"""The avatar write must be refused while a hard-delete claim is active.

``set_user_avatar`` is the one account mutation in the self-service path that
does not go through ``update_user_fields``, so it did not inherit the claim
guard. A request authenticated before the soft delete could land after the
erasure claim was established and still write the account's portrait.

The other self-service writers reach their guard through ``update_user_fields``
or through a route's own status check; the avatar write reached neither.

These tests are red on the unguarded code and green with the guard.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_AVATAR = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=10)
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


async def test_an_avatar_write_on_a_claimed_account_is_refused(store, pool):
    """The claim refuses the write, whatever the column."""
    uid = _uid("avatar-claim")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.set_user_avatar(uid, _AVATAR)

    row = await pool.fetchrow("SELECT avatar_data_url FROM users WHERE id = $1", uid)
    assert row["avatar_data_url"] != _AVATAR


async def test_removing_an_avatar_on_a_claimed_account_is_refused(store, pool):
    """Clearing the portrait is a write too."""
    uid = _uid("avatar-clear")
    await _seed_deleted(pool, uid)
    await store.set_user_avatar(uid, _AVATAR)
    await store.begin_hard_delete_user(uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.set_user_avatar(uid, None)

    row = await pool.fetchrow("SELECT avatar_data_url FROM users WHERE id = $1", uid)
    assert row["avatar_data_url"] == _AVATAR


async def test_a_refused_avatar_write_leaves_the_claim_intact(store, pool):
    """The guard refuses the write; it does not disturb the claim."""
    uid = _uid("avatar-intact")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.set_user_avatar(uid, _AVATAR)

    row = await pool.fetchrow(
        "SELECT hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1", uid
    )
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == claim.token


async def test_an_avatar_write_on_an_ordinary_account_succeeds(store, pool):
    """The ordinary case is unchanged."""
    uid = _uid("avatar-ordinary")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid,
        f"{uid}@example.com",
    )
    await store.set_user_avatar(uid, _AVATAR)
    row = await pool.fetchrow("SELECT avatar_data_url FROM users WHERE id = $1", uid)
    assert row["avatar_data_url"] == _AVATAR


async def test_an_avatar_write_on_an_unclaimed_soft_deleted_account_succeeds(store, pool):
    """The guard is about the claim, not about the status."""
    uid = _uid("avatar-unclaimed")
    await _seed_deleted(pool, uid)
    await store.set_user_avatar(uid, _AVATAR)
    row = await pool.fetchrow("SELECT avatar_data_url FROM users WHERE id = $1", uid)
    assert row["avatar_data_url"] == _AVATAR
