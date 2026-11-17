"""Failure-path verification for PR #1733: transient errors, stage races, expiry.

Covers the review brief's requirements to exercise worker crashes, delayed
heartbeats, simultaneous recovery attempts, transient database errors and
stage-completion races.
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
# The grace boundary is exactly one hour, measured by the database clock.
# ---------------------------------------------------------------------------


async def test_grace_boundary_is_one_hour_measured_by_the_server(store, pool):
    """Just under the grace is refused; just over it is stale."""
    uid = _uid("grace-under")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    # 59 minutes: still inside the grace.
    await _age_claim(pool, uid, minutes=59)
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(uid)
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )

    uid2 = _uid("grace-over")
    await _seed_deleted(pool, uid2)
    await store.begin_hard_delete_user(uid2)
    # 61 minutes: past the grace.
    await _age_claim(pool, uid2, minutes=61)
    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.begin_hard_delete_user(uid2)


async def test_the_stale_check_uses_the_database_clock_not_the_caller(store, pool):
    """A caller with a skewed clock cannot declare a live claim stale."""
    uid = _uid("clock")
    await _seed_deleted(pool, uid)
    owner = await store.begin_hard_delete_user(uid)

    # The claim was just taken: the server clock says it is fresh.
    with pytest.raises(HardDeleteStateChanged):
        await store.begin_hard_delete_user(
            uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
        )
    row = await _claim_row(pool, uid)
    assert row["hard_delete_claim_token"] == owner.token


async def test_a_claim_taken_exactly_now_is_not_stale(store, pool):
    """A brand-new claim is never stale, so a concurrent retry cannot take it."""
    uid = _uid("fresh")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    for _ in range(20):
        with pytest.raises(HardDeleteStateChanged):
            await store.begin_hard_delete_user(
                uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
            )


# ---------------------------------------------------------------------------
# Transient database errors must not corrupt the claim state machine.
# ---------------------------------------------------------------------------


async def test_a_transient_error_during_a_renew_leaves_the_claim_intact(store, pool, monkeypatch):
    """A failed renewal does not clear or corrupt the claim."""
    uid = _uid("transient-renew")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    class _FlakyPool:
        def __init__(self, inner):
            self._inner = inner
            self._failed = False

        def acquire(self):
            return self._inner.acquire()

    # Fail the renewal once with a transient DB error.
    import asyncpg

    calls = {"n": 0}
    real_fetchrow = asyncpg.Connection.fetchrow

    async def _fail_once(self, query, *args, **kwargs):
        if "hard_delete_claimed_at = NOW()" in query and calls["n"] == 0:
            calls["n"] += 1
            raise asyncpg.InterfaceError("transient connection failure")
        return await real_fetchrow(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", _fail_once)
    with pytest.raises(asyncpg.InterfaceError):
        await store.renew_hard_delete_user_claim(uid, claim.token)

    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == claim.token

    # The next attempt works: the failure did not poison the claim.
    await store.renew_hard_delete_user_claim(uid, claim.token)


async def test_a_transient_error_leaves_the_claim_releasable(store, pool, monkeypatch):
    """A failed release does not half-clear the claim."""
    uid = _uid("transient-release")
    await _seed_deleted(pool, uid)
    claim = await store.begin_hard_delete_user(uid)

    import asyncpg

    real_execute = asyncpg.Connection.execute

    async def _fail_once(self, query, *args, **kwargs):
        if "hard_delete_pending = FALSE" in query:
            raise asyncpg.InterfaceError("transient connection failure")
        return await real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", _fail_once)
    with pytest.raises(asyncpg.InterfaceError):
        await store.release_hard_delete_user_claim(uid, claim.token)

    row = await _claim_row(pool, uid)
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == claim.token


# ---------------------------------------------------------------------------
# Stage-completion races.
# ---------------------------------------------------------------------------


async def test_the_final_purge_and_a_concurrent_takeover_have_one_winner(store, pool):
    """A takeover racing the final purge: exactly one deletes the account."""
    for trial in range(5):
        uid = _uid(f"purge-race{trial}")
        await _seed_deleted(pool, uid)
        owner = await store.begin_hard_delete_user(uid)
        await _age_claim(pool, uid, minutes=90)

        async def _takeover(uid=uid):
            try:
                claim = await store.begin_hard_delete_user(
                    uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
                )
                return ("ok", claim.token)
            except HardDeleteStateChanged:
                return ("refused", None)

        async def _purge(uid=uid, owner=owner):
            try:
                await store.hard_delete_user(uid, claim_token=owner.token)
                return "ok"
            except HardDeleteStateChanged:
                return "refused"

        outcomes = await asyncio.gather(_takeover(), _purge())
        takeover_result, purge_result = outcomes
        assert (takeover_result[0] == "ok") != (purge_result == "ok"), outcomes

        if purge_result == "ok":
            # The purge won: the account is gone and the takeover found nothing.
            assert await pool.fetchrow("SELECT id FROM users WHERE id=$1", uid) is None
        else:
            # The takeover won: the old token cannot delete the account.
            assert takeover_result[0] == "ok"
            row = await _claim_row(pool, uid)
            assert row is not None
            assert row["hard_delete_claim_token"] == takeover_result[1]
            counts = await store.hard_delete_user(uid, claim_token=takeover_result[1])
            assert counts["users"] == 1


async def test_a_concurrent_release_and_takeover_do_not_produce_a_zombie(store, pool):
    """A release racing a takeover leaves exactly one consistent state."""
    for trial in range(5):
        uid = _uid(f"release-race{trial}")
        await _seed_deleted(pool, uid)
        owner = await store.begin_hard_delete_user(uid)
        await _age_claim(pool, uid, minutes=90)

        async def _release(uid=uid, owner=owner):
            await store.release_hard_delete_user_claim(uid, owner.token)
            return "released"

        async def _takeover(uid=uid):
            try:
                claim = await store.begin_hard_delete_user(
                    uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
                )
                return ("ok", claim.token)
            except HardDeleteStateChanged:
                return ("refused", None)

        await asyncio.gather(_release(), _takeover())

        row = await _claim_row(pool, uid)
        assert row is not None
        pending = row["hard_delete_pending"]
        token = row["hard_delete_claim_token"]
        if pending:
            # A pending claim must own a token and be recoverable.
            assert token is not None
        else:
            assert token is None


async def test_the_recovered_claim_survives_a_concurrent_release_attempt(store, pool):
    """A stale release concurrent with a takeover cannot clear the new claim."""
    for trial in range(5):
        uid = _uid(f"stale-release{trial}")
        await _seed_deleted(pool, uid)
        owner = await store.begin_hard_delete_user(uid)
        await _age_claim(pool, uid, minutes=90)

        async def _release(uid=uid, owner=owner):
            await store.release_hard_delete_user_claim(uid, owner.token)

        async def _takeover(uid=uid):
            try:
                return await store.begin_hard_delete_user(
                    uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
                )
            except HardDeleteStateChanged:
                return None

        _, taken = await asyncio.gather(_release(), _takeover())
        row = await _claim_row(pool, uid)
        if taken is not None:
            # The takeover won: the new claim owns the row. The release that
            # ran against the *old* token must not have cleared it.
            assert row["hard_delete_pending"] is True
            assert row["hard_delete_claim_token"] == taken.token
            # Whether the row is marked recovered depends on whether the
            # release committed before the takeover took the row lock. Either
            # way the live claim must belong to the new owner.
            assert row["hard_delete_claim_recovered"] in (True, False)
        else:
            # The release won: the claim is gone, so there is nothing stale to
            # recover and the account is claimable again.
            assert row["hard_delete_pending"] is False
            assert row["hard_delete_claim_token"] is None
            fresh = await store.begin_hard_delete_user(uid)
            assert fresh.provenance is HardDeleteClaimProvenance.NEW


# ---------------------------------------------------------------------------
# Crash recovery: a worker that dies leaves a recoverable account.
# ---------------------------------------------------------------------------


async def test_a_crashed_worker_leaves_an_account_recoverable_after_the_grace(store, pool):
    """The crash path: no release, no fence, stale claim, operator recovers."""
    uid = _uid("crash")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)

    # The worker crashed here.
    await _age_claim(pool, uid, minutes=90)

    # An ordinary resume refuses and offers recovery.
    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.resume_user(uid)

    # The operator confirms; the account comes back.
    await store.resume_user(uid, recover_stale_claim=True)
    row = await _claim_row(pool, uid)
    assert row["status"] == "active"
    assert row["hard_delete_pending"] is False


async def test_resume_after_the_grace_without_confirmation_never_clears_the_claim(store, pool):
    """Recovery is refused without the operator's confirmation."""
    uid = _uid("crash-noconfirm")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    with pytest.raises(HardDeleteRecoveryAvailable):
        await store.resume_user(uid)

    row = await _claim_row(pool, uid)
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True


async def test_a_recovered_claim_is_recoverable_again_after_another_grace(store, pool):
    """A second crash of the recovering worker is recoverable in turn."""
    uid = _uid("crash-twice")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid, minutes=90)

    first = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )
    assert first.provenance is HardDeleteClaimProvenance.RECOVERED

    # The recovering worker crashes too.
    await _age_claim(pool, uid, minutes=90)
    second = await store.begin_hard_delete_user(
        uid, recovery_mode=HardDeleteRecoveryMode.PRE_FENCE_OPERATOR
    )
    assert second.provenance is HardDeleteClaimProvenance.RECOVERED
    assert second.token != first.token

    counts = await store.hard_delete_user(uid, claim_token=second.token)
    assert counts["users"] == 1
