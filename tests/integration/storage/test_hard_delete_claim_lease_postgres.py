"""Hard-delete claim lease semantics against real PostgreSQL (0016).

Covers the exclusion, recovery and fencing rules the lease exists for:

- a live claim is refused, never silently taken over;
- a claim that outlived the one-hour recovery grace may be taken over only by
  an explicit recovery (operator attestation or a durable erasure fence), and
  the takeover replaces the token;
- a claim whose lease column is NULL predates the columns and is stamped, then
  protected for the grace period instead of being taken over at once;
- a worker that crashed before the erasure fence leaves an account that an
  operator can recover after the grace;
- a superseded worker cannot complete any destructive stage.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteRecoveryAvailable, HardDeleteStateChanged
from serving.storage.base import HardDeleteClaimProvenance, HardDeleteRecoveryMode
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


async def _seed_deleted(pool, uid: str) -> None:
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=FALSE, "
        "hard_delete_claim_token=NULL, hard_delete_claimed_at=NULL, "
        "hard_delete_claim_recovered=FALSE",
        uid,
        f"{uid}@example.com",
    )


async def _age_claim(pool, uid: str, *, minutes: int = 90) -> None:
    """Move a claim's lease past the recovery grace period."""
    await pool.execute(
        "UPDATE users SET hard_delete_claimed_at = NOW() - make_interval(mins => $2) WHERE id = $1",
        uid,
        minutes,
    )


async def _claim_row(pool, uid: str):
    return await pool.fetchrow(
        "SELECT status, hard_delete_pending, hard_delete_claim_token, "
        "hard_delete_claimed_at, hard_delete_claim_recovered "
        "FROM users WHERE id = $1",
        uid,
    )


async def test_claim_sets_lease_and_renew_extends(store, pool):
    uid = _uid("lease")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)
    assert claim.provenance is HardDeleteClaimProvenance.NEW
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claimed_at"] is not None
    assert row["hard_delete_claim_recovered"] is False
    assert row["hard_delete_claim_token"] == claim.token
    await store.renew_hard_delete_user_claim(uid, claim.token)
    # superseded token must fail closed
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, "wrong-token")
    await store.release_hard_delete_user_claim(uid, claim.token)
    row2 = await pool.fetchrow(
        "SELECT hard_delete_pending, hard_delete_claimed_at FROM users WHERE id=$1", uid
    )
    assert row2["hard_delete_pending"] is False
    assert row2["hard_delete_claimed_at"] is None


async def test_live_claim_is_refused_not_taken_over(store, pool):
    """A second caller must never share or steal a live claim's token."""
    uid = _uid("live-claim")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    # Even an operator recovery cannot take a live claim.
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )

    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == claim.token
    assert row["hard_delete_claim_recovered"] is False
    # The owner is unaffected: it can still renew and complete.
    await store.renew_hard_delete_user_claim(uid, claim.token)


async def test_legacy_null_claim_is_stamped_and_protected(store, pool):
    """A NULL lease is stamped and refused; the stamp protects the claim.

    The regression the reviewer reported: before grace gating, refusing once and
    taking over on the immediate retry protected nothing.
    """
    uid = _uid("nullclaim")
    await pool.execute(
        "INSERT INTO users (id, email, status, hard_delete_pending, "
        "hard_delete_claim_token, hard_delete_claimed_at) "
        "VALUES ($1, $2, 'deleted', TRUE, 'legacy', NULL) "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=TRUE, "
        "hard_delete_claim_token='legacy', hard_delete_claimed_at=NULL",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claimed_at"] is not None
    assert row["hard_delete_claim_token"] == "legacy"

    # The immediate retry is still refused: the stamp is a live lease now.
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == "legacy"


async def test_stale_claim_requires_explicit_recovery(store, pool):
    """Past the grace, an ordinary retry is told recovery is available."""
    uid = _uid("stale")
    await _seed_deleted(pool, uid)
    first = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid)

    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.begin_hard_delete_user(uid)
    # A refused recovery attempt must not have handed out its token.
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == first.token
    assert row["hard_delete_claim_recovered"] is False

    recovered = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )
    assert recovered.provenance is HardDeleteClaimProvenance.RECOVERED
    assert recovered.token != first.token
    assert recovered.newly_acquired is False
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == recovered.token
    assert row["hard_delete_claim_recovered"] is True

    # The superseded worker is fenced out of every later operation.
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, first.token)
    with pytest.raises(HardDeleteStateChanged):
        await store.hard_delete_user(uid, claim_token=first.token)

    # The recovering worker finishes.
    counts = await store.hard_delete_user(uid, claim_token=recovered.token)
    assert counts["users"] == 1


async def test_recovered_claim_is_not_released_after_pre_fence_failure(store, pool):
    """Recovery provenance is sticky: a pre-fence failure keeps the claim."""
    uid = _uid("sticky")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid)
    recovered = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )

    await store.release_hard_delete_user_claim(uid, recovered.token)

    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == recovered.token
    assert row["hard_delete_claim_recovered"] is True

    # An operator can still decide again after another grace period.
    await _age_claim(pool, uid)
    again = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )
    assert again.provenance is HardDeleteClaimProvenance.RECOVERED
    assert again.token != recovered.token


async def test_concurrent_recovery_hands_out_exactly_one_token(store, pool):
    """Two operators recovering at once: one proceeds, the other is refused."""
    uid = _uid("recover-race")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid)

    async def _recover():
        try:
            claim = await store.begin_hard_delete_user(
                uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
            )
            return ("recovered", claim.token)
        except HardDeleteStateChanged:
            return ("refused", None)

    results = await asyncio.gather(_recover(), _recover())
    outcomes = [r[0] for r in results]
    assert outcomes.count("recovered") == 1, outcomes
    assert outcomes.count("refused") == 1, outcomes
    winner_token = dict(results)["recovered"]
    assert await pool.fetchval("SELECT hard_delete_claim_token FROM users WHERE id = $1", uid) == (
        winner_token
    )


async def test_resume_clears_a_stale_claim_only_with_operator_confirmation(store, pool):
    """Crash before the fence: resume recovers the abandoned claim after grace."""
    uid = _uid("crash-before-fence")
    await _seed_deleted(pool, uid)
    abandoned = await store.begin_hard_delete_user(uid)

    # The worker crashed here: no fence, no release, hard_delete_pending set.
    with pytest.raises(HardDeleteStateChanged):
        await store.resume_user(uid)
    # Confirming recovery inside the grace period is refused too.
    with pytest.raises(HardDeleteStateChanged):
        await store.resume_user(uid, recover_stale_claim=True)
    row = await _claim_row(pool, uid)
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True

    await _age_claim(pool, uid)
    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.resume_user(uid)
    row = await _claim_row(pool, uid)
    assert row["status"] == "deleted"
    assert row["hard_delete_claim_token"] == abandoned.token

    await store.resume_user(uid, recover_stale_claim=True)
    row = await _claim_row(pool, uid)
    assert row["status"] == "active"
    assert row["hard_delete_pending"] is False
    assert row["hard_delete_claim_token"] is None
    assert row["hard_delete_claimed_at"] is None
    assert row["hard_delete_claim_recovered"] is False


async def test_concurrent_begin_and_resume_serialize(store, pool):
    """Concurrent resume vs hard-delete: exactly one winner, no zombie claim.

    Regression proof for the expected-status race: both operations take the
    user row lock, so they serialize. Whichever commits first, the loser
    fails closed with HardDeleteStateChanged and the final row is consistent:
    either active with no pending claim (resume won), or deleted with a
    pending claim owned by the winner's token (begin won) — never active
    with a stranded claim, and never deleted with a claim resume cleared.
    """
    for trial in range(5):
        uid = _uid(f"race{trial}")
        await _seed_deleted(pool, uid)

        async def _begin(uid=uid):
            try:
                claim = await store.begin_hard_delete_user(uid)
                return ("claimed", claim.token)
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
    await _seed_deleted(pool, uid2)
    claim = await store.begin_hard_delete_user(uid2)
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid2, expected_status="deleted", status="active")
    with pytest.raises(HardDeleteStateChanged):
        await store.suspend_user(uid2, expected_status="deleted")
    row = await pool.fetchrow(
        "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id=$1", uid2
    )
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == claim.token
    await store.release_hard_delete_user_claim(uid2, claim.token)
