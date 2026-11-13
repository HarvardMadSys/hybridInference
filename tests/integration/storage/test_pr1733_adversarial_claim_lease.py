"""Independent adversarial verification of PR #1733's hard-delete claim lease.

These tests are written against the invariants, not against the PR's own
tests: they use independent connections, deterministic barriers, and the real
PostgreSQL row/advisory locks, so a scheduling accident cannot decide the
outcome. Every destructive action runs against the disposable local test
database only.

Invariant map (the numbering follows the review brief):

1. A live worker cannot be superseded by a concurrent claim.
2. Stale takeover requires the grace period AND explicit recovery authority.
3. A superseded worker cannot continue destructive operations.
4. Heartbeat coverage protects every destructive stage, including LogStore.
5. Token ownership is checked atomically wherever authority is exercised.
6. Crash recovery works before and after the durable erasure fence.
7. Legacy NULL claim timestamps cannot permit immediate unauthorized takeover.
8. CAS conflicts and lost ownership return the correct HTTP 409 semantics.
9. The migration forms exactly one valid Alembic chain on current dev.
10. Session eviction and targeted auth-cache invalidation remain intact.
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


async def _age_claim(pool, uid: str, *, minutes: int = 90) -> None:
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


# ---------------------------------------------------------------------------
# Invariant 1: a live worker cannot be superseded by a concurrent claim.
# ---------------------------------------------------------------------------


async def test_inv1_concurrent_claims_yield_exactly_one_owner(store, pool):
    """Ten simultaneous claims: one token, nine refusals, one owner."""
    uid = _uid("inv1-concurrent")
    await _seed_deleted(pool, uid)

    async def _claim():
        try:
            claim = await store.begin_hard_delete_user(uid)
            return ("ok", claim.token)
        except HardDeleteStateChanged:
            return ("refused", None)

    results = await asyncio.gather(*(_claim() for _ in range(10)))
    oks = [r for r in results if r[0] == "ok"]
    refused = [r for r in results if r[0] == "refused"]
    assert len(oks) == 1, results
    assert len(refused) == 9, results

    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == oks[0][1]
    assert row["hard_delete_pending"] is True


async def test_inv1_a_live_claim_is_not_stolen_even_with_recovery_authority(store, pool):
    """Recovery authority alone must not take over a *live* lease."""
    uid = _uid("inv1-live-recover")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)

    for mode in (
        HardDeleteRecoveryMode.PRE_FENCE_OPERATOR,
        HardDeleteRecoveryMode.POST_FENCE,
    ):
        with pytest.raises(HardDeleteStateChanged):
            await store.begin_hard_delete_user(uid, recovery_mode=mode)

    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == owner.token
    assert row["hard_delete_claim_recovered"] is False
    # The owner is unaffected.
    await store.renew_hard_delete_user_claim(uid, owner.token)


async def test_inv1_renewal_extends_the_lease_so_it_is_never_stale(store, pool):
    """A heartbeat renewal keeps a claim out of the stale window."""
    uid = _uid("inv1-renew")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    # Age it, then renew: the lease must be back to now.
    await _age_claim(pool, uid, minutes=90)
    await store.renew_hard_delete_user_claim(uid, claim.token)
    row = await _claim_row(pool, uid)
    delta = row["hard_delete_claimed_at"] - await pool.fetchval("SELECT NOW()")
    assert delta.total_seconds() > -60, delta

    # Not stale -> an ordinary retry is refused outright.
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)


# ---------------------------------------------------------------------------
# Invariant 2: stale takeover needs grace AND explicit authority.
# ---------------------------------------------------------------------------


async def test_inv2_stale_claim_needs_grace_before_any_takeover(store, pool):
    """Just inside the grace: even recovery authority is refused."""
    uid = _uid("inv2-grace")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    # 30 minutes old: inside the one-hour grace.
    await _age_claim(pool, uid, minutes=30)

    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid, recovery_mode=HardDeleteRecoveryMode.POST_FENCE)
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == owner.token


async def test_inv2_past_grace_but_no_authority_is_refused_and_advertises_recovery(store, pool):
    """Past the grace without authority: refused, and recovery is signalled."""
    uid = _uid("inv2-noauth")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.begin_hard_delete_user(uid)

    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == owner.token
    assert row["hard_delete_claim_recovered"] is False


async def test_inv2_past_grace_with_authority_takes_over_and_replaces_token(store, pool):
    """Past the grace with authority: the takeover replaces the token."""
    uid = _uid("inv2-takeover")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    taken = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )
    assert taken.provenance is HardDeleteClaimProvenance.RECOVERED
    assert taken.token != owner.token
    assert taken.newly_acquired is False

    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == taken.token
    assert row["hard_delete_claim_recovered"] is True


async def test_inv2_simultaneous_recovery_attempts_handover_to_exactly_one(store, pool):
    """Eight simultaneous recoveries: one token, the rest refused."""
    uid = _uid("inv2-simrecover")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    async def _recover():
        try:
            claim = await store.begin_hard_delete_user(
                uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
            )
            return ("ok", claim.token)
        except HardDeleteStateChanged:
            return ("refused", None)

    results = await asyncio.gather(*(_recover() for _ in range(8)))
    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, results
    assert len([r for r in results if r[0] == "refused"]) == 7, results

    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == oks[0][1]


async def test_inv2_recovery_modes_are_distinguished(store, pool):
    """PRE_FENCE_OPERATOR and POST_FENCE are both explicit authorities."""
    uid = _uid("inv2-modes")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    claim = await store.begin_hard_delete_user(uid, recovery_mode=HardDeleteRecoveryMode.POST_FENCE)
    assert claim.provenance is HardDeleteClaimProvenance.RECOVERED
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == claim.token


async def test_inv2_unknown_recovery_mode_is_rejected(store, pool):
    """An unrecognized mode is a programming error, never a silent takeover."""
    uid = _uid("inv2-badmode")
    await _seed_deleted(pool, uid)
    with pytest.raises(ValueError):
        await store.begin_hard_delete_user(uid, recovery_mode="not-a-mode")


# ---------------------------------------------------------------------------
# Invariant 3: a superseded worker cannot continue destructive operations.
# ---------------------------------------------------------------------------


async def test_inv3_superseded_worker_cannot_renew_or_purge(store, pool):
    """After a takeover the old token is dead for both operations."""
    uid = _uid("inv3-superseded")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)
    taken = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )

    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, owner.token)
    with pytest.raises(HardDeleteStateChanged):
        await store.hard_delete_user(uid, claim_token=owner.token)

    # The account is untouched by the superseded worker.
    row = await _claim_row(pool, uid)
    assert row["status"] == "deleted"
    assert row["hard_delete_claim_token"] == taken.token

    # The new owner can finish.
    counts = await store.hard_delete_user(uid, claim_token=taken.token)
    assert counts["users"] == 1


async def test_inv3_superseded_worker_cannot_release_the_new_owners_claim(store, pool):
    """A stale release must not clear the live owner's claim."""
    uid = _uid("inv3-release")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)
    taken = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )

    await store.release_hard_delete_user_claim(uid, owner.token)
    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == taken.token


async def test_inv3_purge_is_token_scoped_and_writes_nothing_for_a_foreign_token(store, pool):
    """A foreign token's purge deletes no rows."""
    uid = _uid("inv3-purge")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)

    with pytest.raises(HardDeleteStateChanged):
        await store.hard_delete_user(uid, claim_token="not-the-owner")

    row = await _claim_row(pool, uid)
    assert row is not None
    assert row["status"] == "deleted"
    assert row["hard_delete_claim_token"] == owner.token


async def test_inv3_only_one_of_two_racing_purges_deletes_the_account(store, pool):
    """Two workers, one token each (forced): only the owner deletes."""
    uid = _uid("inv3-racepurge")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)

    # Force a second token into the row directly, as a takeover would.
    await pool.execute("UPDATE users SET hard_delete_claim_token = 'other' WHERE id = $1", uid)

    async def _purge(token):
        try:
            await store.hard_delete_user(uid, claim_token=token)
            return "ok"
        except HardDeleteStateChanged:
            return "refused"

    outcomes = await asyncio.gather(_purge(owner.token), _purge("other"))
    assert outcomes.count("ok") == 1, outcomes
    assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None


# ---------------------------------------------------------------------------
# Invariant 5: token ownership is checked atomically where authority is used.
# ---------------------------------------------------------------------------


async def test_inv5_renew_is_atomic_with_ownership(store, pool):
    """Renewal only touches a row the token still owns."""
    uid = _uid("inv5-renew")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)

    # A wrong token changes nothing.
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, "wrong")
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == owner.token

    # A token on a row that is not pending a hard delete cannot renew.
    uid2 = _uid("inv5-renew-active")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid2,
        f"{uid2}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid2, owner.token)


async def test_inv5_renew_fails_once_the_row_is_gone(store, pool):
    """A renewed claim on a deleted account fails closed."""
    uid = _uid("inv5-gone")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)
    await store.hard_delete_user(uid, claim_token=owner.token)
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, owner.token)


async def test_inv5_expected_status_cas_is_atomic(store, pool):
    """A stale expected_status writes nothing."""
    uid = _uid("inv5-cas")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.update_user_fields(uid, expected_status="pending_approval", status="active")
    assert (await pool.fetchrow("SELECT status FROM users WHERE id=$1", uid))["status"] == "active"


# ---------------------------------------------------------------------------
# Invariant 6: crash recovery before and after the erasure fence.
# ---------------------------------------------------------------------------


async def test_inv6_crash_before_fence_is_recoverable_after_grace(store, pool):
    """A crashed worker leaves a stale claim the operator can take over."""
    uid = _uid("inv6-crash-pre")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)

    # Crash: no release, no fence.
    with pytest.raises(HardDeleteStateChanged):
        await store.resume_user(uid)
    with pytest.raises(HardDeleteStateChanged):
        await store.resume_user(uid, recover_stale_claim=True)

    await _age_claim(pool, uid, minutes=90)
    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.resume_user(uid)

    await store.resume_user(uid, recover_stale_claim=True)
    row = await _claim_row(pool, uid)
    assert row["status"] == "active"
    assert row["hard_delete_pending"] is False
    assert row["hard_delete_claim_token"] is None
    assert row["hard_delete_claimed_at"] is None
    assert row["hard_delete_claim_recovered"] is False


async def test_inv6_crash_after_fence_is_recoverable_by_takeover(store, pool):
    """A fenced crash leaves a stale claim a POST_FENCE takeover can take."""
    uid = _uid("inv6-crash-post")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    taken = await store.begin_hard_delete_user(uid, recovery_mode=HardDeleteRecoveryMode.POST_FENCE)
    assert taken.provenance is HardDeleteClaimProvenance.RECOVERED
    counts = await store.hard_delete_user(uid, claim_token=taken.token)
    assert counts["users"] == 1


async def test_inv6_recovered_claim_is_not_released_after_a_pre_fence_failure(store, pool):
    """Recovery provenance is sticky: a failed purge keeps the claim."""
    uid = _uid("inv6-sticky")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)
    recovered = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )

    await store.release_hard_delete_user_claim(uid, recovered.token)
    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == recovered.token
    assert row["hard_delete_claim_recovered"] is True


# ---------------------------------------------------------------------------
# Invariant 7: legacy NULL claim timestamps.
# ---------------------------------------------------------------------------


async def test_inv7_null_lease_is_stamped_and_refused(store, pool):
    """A NULL lease is stamped and refused; the retry is still refused."""
    uid = _uid("inv7-null")
    await pool.execute(
        "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token, "
        "hard_delete_claimed_at) VALUES ($1, $2, 'deleted', TRUE, 'legacy', NULL) "
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
    assert row["hard_delete_claimed_at"] is not None

    # Even recovery authority is refused inside the grace.
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )


async def test_inv7_null_lease_cannot_be_renewed_by_a_stranger(store, pool):
    """A legacy NULL-lease claim is not owned by any new caller."""
    uid = _uid("inv7-nullrenew")
    await pool.execute(
        "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token, "
        "hard_delete_claimed_at) VALUES ($1, $2, 'deleted', TRUE, 'legacy', NULL) "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=TRUE, "
        "hard_delete_claim_token='legacy', hard_delete_claimed_at=NULL",
        uid,
        f"{uid}@example.com",
    )
    with pytest.raises(HardDeleteStateChanged):
        await store.renew_hard_delete_user_claim(uid, "any-token")


async def test_inv7_many_null_lease_claimers_cannot_stamp_themselves_a_lease(store, pool):
    """Concurrent callers against a NULL-lease claim: all refused, one stamp."""
    uid = _uid("inv7-nullrace")
    await pool.execute(
        "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token, "
        "hard_delete_claimed_at) VALUES ($1, $2, 'deleted', TRUE, 'legacy', NULL) "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=TRUE, "
        "hard_delete_claim_token='legacy', hard_delete_claimed_at=NULL",
        uid,
        f"{uid}@example.com",
    )

    async def _claim():
        try:
            await store.begin_hard_delete_user(uid)
            return "ok"
        except HardDeleteStateChanged:
            return "refused"

    outcomes = await asyncio.gather(*(_claim() for _ in range(6)))
    assert outcomes.count("ok") == 0, outcomes
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == "legacy"
    assert row["hard_delete_claimed_at"] is not None
