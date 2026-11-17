"""The claim guard on ``update_user_fields``: fail closed, and only the writes
that the claim is about.

PR #1733 gave ``update_user_fields`` a pending-hard-delete-claim guard, so
no write can change what is being erased. A follow-up review of the
verification branch relaxed that guard for writes that do not name ``status``
(``user_name``, ``password_hash``, ...), on the reasoning that such a write
cannot strand a claim. That relaxation was wrong for two reasons:

- it permitted changes to identity fields — ``email``, ``role``,
  ``preferences`` — on an account being erased, which is exactly what the
  claim exists to prevent, and which every other store refuses (see
  ``batch_store._lock_owner``, which refuses *any* work for an account marked
  ``hard_delete_pending``);
- it was not needed. A claimed account is always soft-deleted, so the
  self-service writers cannot reach it: they require an ``active`` account.

The guard is therefore unconditional, and the routes that can reach it report
the conflict instead of letting it escape.

These tests pin that behaviour.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


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


async def _seed(pool, uid: str, *, status: str = "active") -> None:
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, $3) "
        "ON CONFLICT (id) DO UPDATE SET status = $3, hard_delete_pending = FALSE, "
        "hard_delete_claim_token = NULL, hard_delete_claimed_at = NULL, "
        "hard_delete_claim_recovered = FALSE",
        uid,
        f"{uid}@example.com",
        status,
    )


async def _claim(pool, uid: str) -> str:
    claim = await PostgresOperationalStore(pool).begin_hard_delete_user(uid)
    return claim.token


# ---------------------------------------------------------------------------
# While a claim is active: every write is refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("user_name", "renamed"),
        ("password_hash", "new-hash"),
        ("email", "moved@example.com"),
        ("role", "admin"),
        ("preferences", '{"theme":"dark"}'),
        ("max_concurrent_requests", 99),
    ],
)
async def test_every_write_to_a_claimed_account_is_refused(store, pool, column, value):
    """No write may change what is being erased.

    This includes the identity fields: the claim exists so the erasure sees a
    fixed account, and every other store refuses any work for an account
    marked ``hard_delete_pending``.
    """
    uid = _uid("claimed")
    await _seed(pool, uid, status="deleted")
    await _claim(pool, uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, **{column: value})

    # Nothing was written.
    row = await pool.fetchrow(f"SELECT {column} FROM users WHERE id = $1", uid)
    assert row[column] != value


async def test_a_compare_and_swap_on_a_claimed_account_is_refused(store, pool):
    """A stale or valid ``expected_status`` is refused the same way."""
    uid = _uid("claimed-cas")
    await _seed(pool, uid, status="deleted")
    await _claim(pool, uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, expected_status="deleted", user_name="renamed")
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, expected_status="deleted", status="active")

    row = await pool.fetchrow("SELECT user_name, status FROM users WHERE id = $1", uid)
    assert row["user_name"] != "renamed"
    assert row["status"] == "deleted"


async def test_a_refused_write_leaves_the_claim_intact(store, pool):
    """The guard refuses the write; it does not disturb the claim."""
    uid = _uid("claimed-intact")
    await _seed(pool, uid, status="deleted")
    token = await _claim(pool, uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, email="moved@example.com")

    row = await pool.fetchrow(
        "SELECT hard_delete_pending, hard_delete_claim_token, hard_delete_claimed_at "
        "FROM users WHERE id = $1",
        uid,
    )
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == token
    assert row["hard_delete_claimed_at"] is not None


# ---------------------------------------------------------------------------
# Without a claim: writes to active and to unclaimed soft-deleted accounts
# are unchanged.
# ---------------------------------------------------------------------------


async def test_a_plain_write_on_an_active_account_succeeds(store, pool):
    uid = _uid("active")
    await _seed(pool, uid)
    await store.update_user_fields(uid, user_name="renamed")
    row = await pool.fetchrow("SELECT user_name FROM users WHERE id = $1", uid)
    assert row["user_name"] == "renamed"


async def test_a_plain_write_on_an_unclaimed_soft_deleted_account_succeeds(store, pool):
    """A soft-deleted account with no claim is not being erased yet.

    The guard is about the claim, not about the status: the admin's notes
    and the account's own fields stay writable until a claim exists.
    """
    uid = _uid("unclaimed-deleted")
    await _seed(pool, uid, status="deleted")
    await store.update_user_fields(uid, user_name="renamed")
    row = await pool.fetchrow("SELECT user_name FROM users WHERE id = $1", uid)
    assert row["user_name"] == "renamed"


async def test_a_write_after_the_claim_is_released_succeeds(store, pool):
    """Once the claim is released the account is writable again."""
    uid = _uid("released")
    await _seed(pool, uid, status="deleted")
    token = await _claim(pool, uid)
    await store.release_hard_delete_user_claim(uid, token)

    await store.update_user_fields(uid, user_name="renamed")
    row = await pool.fetchrow("SELECT user_name FROM users WHERE id = $1", uid)
    assert row["user_name"] == "renamed"


async def test_a_status_write_on_an_active_account_succeeds(store, pool):
    """The ordinary status write is unaffected."""
    uid = _uid("active-status")
    await _seed(pool, uid)
    await store.update_user_fields(uid, status="suspended")
    row = await pool.fetchrow("SELECT status FROM users WHERE id = $1", uid)
    assert row["status"] == "suspended"
