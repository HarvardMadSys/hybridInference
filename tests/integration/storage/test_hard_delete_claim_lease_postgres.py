"""Hard-delete claim lease semantics against real PostgreSQL (0003)."""

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
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_claim_sets_lease_and_renew_extends(store, pool):
    uid = _uid("lease")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') ON CONFLICT (id) DO UPDATE SET status='deleted'",
        uid,
        f"{uid}@example.com",
    )
    tok = await store.begin_hard_delete_user(uid)
    row = await pool.fetchrow(
        "SELECT hard_delete_claimed_at, hard_delete_claim_recovered FROM users WHERE id=$1", uid
    )
    assert row["hard_delete_claimed_at"] is not None
    assert row["hard_delete_claim_recovered"] is False
    await store.renew_hard_delete_user_claim(uid, tok)
    # superseded token must fail closed
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, "wrong-token")
    await store.release_hard_delete_user_claim(uid, tok)
    row2 = await pool.fetchrow(
        "SELECT hard_delete_pending, hard_delete_claimed_at FROM users WHERE id=$1", uid
    )
    assert row2["hard_delete_pending"] is False


@pytest.mark.asyncio
async def test_null_claimed_at_is_stamped_and_refused(store, pool):
    uid = _uid("nullclaim")
    await pool.execute(
        "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token, hard_delete_claimed_at) VALUES ($1, $2, 'deleted', TRUE, 'legacy', NULL) ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=TRUE, hard_delete_claim_token='legacy', hard_delete_claimed_at=NULL",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    row = await pool.fetchrow("SELECT hard_delete_claimed_at FROM users WHERE id=$1", uid)
    assert row["hard_delete_claimed_at"] is not None


@pytest.mark.asyncio
async def test_concurrent_begin_and_resume_serialize(store, pool):
    """Concurrent resume vs hard-delete: exactly one winner, no zombie claim.

    Regression proof for the expected-status race: both operations take the
    user row lock, so they serialize. Whichever commits first, the loser
    fails closed with HardDeleteStateChanged and the final row is consistent:
    either active with no pending claim (resume won), or deleted with a
    pending claim owned by the winner's token (begin won) — never active
    with a stranded claim, and never deleted with a claim resume cleared.
    """
    import asyncio

    for trial in range(5):
        uid = _uid(f"race{trial}")
        await pool.execute(
            "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=FALSE, "
            "hard_delete_claim_token=NULL",
            uid,
            f"{uid}@example.com",
        )

        async def _begin(uid=uid):
            try:
                return ("claimed", await store.begin_hard_delete_user(uid))
            except HardDeleteStateChanged:
                return ("refused", None)

        async def _resume(uid=uid):
            try:
                await store.resume_user(uid)
                return ("resumed", None)
            except HardDeleteStateChanged:
                return ("refused", None)

        results = await asyncio.gather(_begin(), _resume())
        outcomes = [r[0] for r in results]
        # Exactly one winner: resume xor claim, the other refused.
        assert outcomes.count("refused") == 1, outcomes
        assert ("resumed" in outcomes) != ("claimed" in outcomes), outcomes

        row = await pool.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id=$1",
            uid,
        )
        assert row is not None
        if "resumed" in outcomes:
            assert row["status"] == "active"
            assert row["hard_delete_pending"] is False
            assert row["hard_delete_claim_token"] is None
            # The loser (begin) must stay refused on retry: no zombie claim.
            with pytest.raises(HardDeleteStateChanged):
                await store.begin_hard_delete_user(uid)
        else:
            assert row["status"] == "deleted"
            assert row["hard_delete_pending"] is True
            winner_token = dict(results)["claimed"]
            assert row["hard_delete_claim_token"] == winner_token
            # The loser (resume) must stay refused while the claim lives.
            with pytest.raises(HardDeleteStateChanged):
                await store.resume_user(uid)
            # No zombie: the winner can still finish the purge.
            counts = await store.hard_delete_user(uid, claim_token=winner_token)
            assert counts["users"] == 1
            assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None


@pytest.mark.asyncio
async def test_resume_requires_deleted_status(store, pool):
    """Resume on a live account fails closed instead of re-activating it."""
    uid = _uid("resume-active")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.resume_user(uid)
    row = await pool.fetchrow("SELECT status FROM users WHERE id=$1", uid)
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_expected_status_transitions_fail_closed_on_stale_read(store, pool):
    """Stale expected_status / pending claim blocks every status transition."""
    # approve/reject on a non-pending account: no write.
    uid = _uid("stale-approve")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.approve_user(uid, admin_id="admin")
    with pytest.raises(HardDeleteStateChanged):
        await store.reject_user(uid, admin_id="admin", reason="late")
    assert (await pool.fetchrow("SELECT status FROM users WHERE id=$1", uid))["status"] == "active"

    # update/suspend with a stale expected_status: no write.
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, expected_status="suspended", status="suspended")
    with pytest.raises(HardDeleteStateChanged):
        await store.suspend_user(uid, expected_status="suspended")
    assert (await pool.fetchrow("SELECT status FROM users WHERE id=$1", uid))["status"] == "active"

    # A pending hard-delete claim blocks transitions even with the right status.
    uid2 = _uid("claimed-transition")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=FALSE",
        uid2,
        f"{uid2}@example.com",
    )
    tok = await store.begin_hard_delete_user(uid2)
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid2, expected_status="deleted", status="active")
    with pytest.raises(HardDeleteStateChanged):
        await store.suspend_user(uid2, expected_status="deleted")
    row = await pool.fetchrow(
        "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id=$1", uid2
    )
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == tok
    await store.release_hard_delete_user_claim(uid2, tok)
