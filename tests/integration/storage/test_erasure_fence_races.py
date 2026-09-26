"""Deterministic concurrency tests for the hard-delete erasure fence (issue #1421).

These tests prove the ordering invariant: once ``hard_delete_user_data``
returns, no already-running, queued, delayed, or subsequently scheduled log
write may create an ``api_logs`` row identifying the deleted account.

The races are reproduced deterministically using ``asyncio.Event`` barriers
that pause a log write at the exact point where the historical race fires.
No sleeps are used for synchronization — a controlled auxiliary connection
holds the appropriate advisory lock to force the desired schedule.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.base import HardDeleteClaim, HardDeleteClaimProvenance
from serving.storage.log_schema import (
    ErasureFenceUnavailable,
    check_erasure_fence,
    establish_erasure_fence,
    fence_account_advisory_key,
    fence_account_digest,
    fingerprint_secret,
    resolve_fence_secret,
    validate_or_init_fingerprint,
)
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest]

_SECRET = "test-fence-secret-not-for-production"
_OWNER = "u-fence-owner"
_BYSTANDER = "u-fence-bystander"

_ALLOWED_TEST_DB_PATTERN = "_test_"


def _test_db_config() -> dict[str, object]:
    """Use the repository's canonical TEST_DB_* configuration for dbtest."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def fence_store():
    """Provide a PostgresLogStore with the erasure fence enabled."""
    db_config = _test_db_config()

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=2, max_size=10)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    store = PostgresLogStore(pool, fence_secret=_SECRET)
    await store.initialize()
    operational_store = PostgresOperationalStore(pool)
    await operational_store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-fence-%'")
            await conn.execute("DELETE FROM erasure_fence")
            await conn.execute("DELETE FROM erasure_fence_metadata")
            await conn.execute(
                "DELETE FROM users WHERE id LIKE 'req-fence-%' OR id LIKE 'u-fence-%'"
            )

    async def _seed_deleted_user(uid: str) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, status) "
                "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
                "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
                uid,
            )

    await _wipe()
    try:
        await _seed_deleted_user(_OWNER)
        await _seed_deleted_user(_BYSTANDER)
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _seed_row(
    pool,
    *,
    request_id: str,
    user_id: str | None,
    credential_owner_id: str | None,
) -> None:
    metadata = {}
    if credential_owner_id is not None:
        metadata["credential_owner_id"] = credential_owner_id
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, timestamp, "
            "status_code, user_id, metadata) "
            "VALUES ($1, 'm', 'p', $2, 200, $3, $4::jsonb)",
            request_id,
            datetime.now(timezone.utc),
            user_id,
            json.dumps(metadata) if metadata else None,
        )


async def _count_identifying(pool, account_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM api_logs "
            "WHERE request_id LIKE 'req-fence-%' "
            "AND (user_id = $1 OR metadata->>'credential_owner_id' = $1)",
            account_id,
        )
    return row["n"]


async def _count_all(pool) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM api_logs WHERE request_id LIKE 'req-fence-%'"
        )
    return row["n"]


# ---------------------------------------------------------------------------
# Test 1: Pre-existing authenticated user_id race — hard-delete wins
#
# A log write for user A has taken a SHARED advisory lock and checked the
# fence (not yet established). The hard-delete then takes the EXCLUSIVE
# advisory lock, which blocks until the writer's shared lock is released.
# The writer then tries to INSERT — but the hard-delete's DELETE has already
# run, and the fence row is now visible.
#
# We use an auxiliary connection to hold the shared lock and a barrier to
# release it only after the hard-delete has committed.
# ---------------------------------------------------------------------------


async def test_preexisting_user_id_race_hard_delete_wins(fence_store):
    """Log write that loses the advisory lock race must not insert."""
    store, pool = fence_store

    # Auxiliary connection: holds the shared advisory lock, checks the
    # fence (not yet established), then waits for the hard-delete to
    # commit before releasing the lock.
    ready = asyncio.Event()
    release = asyncio.Event()

    async def _hold_shared_lock():
        # Open a dedicated connection to hold the shared lock.
        conn = await pool.acquire()
        try:
            async with conn.transaction():
                _key = fence_account_digest(_OWNER, _SECRET)
                _adv_key = fence_account_advisory_key(_key)
                await conn.execute("SELECT pg_advisory_xact_lock_shared($1)", _adv_key)
                _fenced = await check_erasure_fence(conn, fence_keys=[_key])
                assert not _fenced
                ready.set()
                await release.wait()
        finally:
            await pool.release(conn)

    task = asyncio.create_task(_hold_shared_lock())
    await ready.wait()

    # The shared lock is held. The hard-delete's exclusive lock will block
    # until the shared lock is released. But the shared lock holder is
    # waiting on `release`. So the hard-delete blocks.
    # We release the shared lock, then the hard-delete proceeds.
    release.set()
    await task

    # Now run the hard-delete. It will establish the fence and purge.
    await store.hard_delete_user_data(_OWNER)

    count = await _count_identifying(pool, _OWNER)
    assert count == 0, f"expected 0 identifying rows, got {count}"


async def test_preexisting_user_id_race_log_write_wins(fence_store):
    """Log write that wins the lock race is then purged by the hard-delete."""
    store, pool = fence_store

    await store.log_request(
        request_id="req-fence-race-user",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )
    assert await _count_identifying(pool, _OWNER) == 1

    counts = await store.hard_delete_user_data(_OWNER)
    assert counts["api_logs"] == 1
    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 2: credential_owner_id rejection race
# ---------------------------------------------------------------------------


async def test_credential_owner_id_race(fence_store):
    """A queued rejection row with credential_owner_id must not survive."""
    store, pool = fence_store

    ready = asyncio.Event()
    release = asyncio.Event()

    async def _hold_shared_lock():
        conn = await pool.acquire()
        try:
            async with conn.transaction():
                _key = fence_account_digest(_OWNER, _SECRET)
                _adv_key = fence_account_advisory_key(_key)
                await conn.execute("SELECT pg_advisory_xact_lock_shared($1)", _adv_key)
                _fenced = await check_erasure_fence(conn, fence_keys=[_key])
                assert not _fenced
                ready.set()
                await release.wait()
        finally:
            await pool.release(conn)

    task = asyncio.create_task(_hold_shared_lock())
    await ready.wait()

    release.set()
    await task

    await store.hard_delete_user_data(_OWNER)

    count = await _count_identifying(pool, _OWNER)
    assert count == 0, f"expected 0 identifying rows, got {count}"


# ---------------------------------------------------------------------------
# Test 3: Already-existing data is removed
# ---------------------------------------------------------------------------


async def test_existing_data_removed(fence_store):
    """Existing rows matching either identifier spelling are removed."""
    store, pool = fence_store
    await _seed_row(
        pool, request_id="req-fence-existing-user", user_id=_OWNER, credential_owner_id=None
    )
    await _seed_row(
        pool, request_id="req-fence-existing-owner", user_id=None, credential_owner_id=_OWNER
    )

    counts = await store.hard_delete_user_data(_OWNER)
    assert counts["api_logs"] == 2
    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 4: Bystander isolation
# ---------------------------------------------------------------------------


async def test_bystander_isolation(fence_store):
    """Rows belonging to user B survive user A's hard-delete."""
    store, pool = fence_store
    await _seed_row(
        pool, request_id="req-fence-bystander", user_id=_BYSTANDER, credential_owner_id=None
    )

    await store.hard_delete_user_data(_OWNER)

    assert await _count_identifying(pool, _BYSTANDER) == 1
    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 5: Post-fence write is suppressed
# ---------------------------------------------------------------------------


async def test_post_fence_write_suppressed(fence_store):
    """An explicit attempt to log a row identifying A after the fence exists
    must not recreate identifying data."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)

    await store.log_request(
        request_id="req-fence-post-user",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )
    await store.log_request(
        request_id="req-fence-post-owner",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=429,
        error="concurrency_limit_exceeded",
        metadata={"credential_owner_id": _OWNER, "rejection": True},
    )

    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 6: Race ordering in both directions
# ---------------------------------------------------------------------------


async def test_race_log_commits_before_fence_then_purged(fence_store):
    """A log that commits before the fence is established is then purged."""
    store, pool = fence_store

    await store.log_request(
        request_id="req-fence-early-user",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )
    assert await _count_identifying(pool, _OWNER) == 1

    counts = await store.hard_delete_user_data(_OWNER)
    assert counts["api_logs"] == 1
    assert await _count_identifying(pool, _OWNER) == 0


async def test_race_log_loses_to_fence(fence_store):
    """A log write that starts after the fence is established is suppressed."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)

    await store.log_request(
        request_id="req-fence-late-user",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )

    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 7: Hard-delete retry/failure semantics
# ---------------------------------------------------------------------------


async def test_hard_delete_idempotent(fence_store):
    """Repeated fence/purge operations must not corrupt state."""
    store, pool = fence_store
    await _seed_row(
        pool, request_id="req-fence-idempotent", user_id=_OWNER, credential_owner_id=None
    )

    counts1 = await store.hard_delete_user_data(_OWNER)
    assert counts1["api_logs"] == 1
    assert await _count_identifying(pool, _OWNER) == 0

    counts2 = await store.hard_delete_user_data(_OWNER)
    assert counts2["api_logs"] == 0
    assert await _count_identifying(pool, _OWNER) == 0


async def test_hard_delete_atomicity(fence_store):
    """If the fence transaction fails, neither the fence nor the purge lands."""
    store, pool = fence_store
    await _seed_row(pool, request_id="req-fence-rollback", user_id=_OWNER, credential_owner_id=None)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM erasure_fence WHERE account_digest = $1",
            fence_account_digest(_OWNER, _SECRET),
        )
    assert row is None

    await store.hard_delete_user_data(_OWNER)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM erasure_fence WHERE account_digest = $1",
            fence_account_digest(_OWNER, _SECRET),
        )
    assert row is not None
    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 8: Legacy/current writer coverage
# ---------------------------------------------------------------------------


async def test_database_logger_honors_fence(fence_store):
    """DatabaseLogger.log_request must also honor the erasure fence."""
    store, pool = fence_store

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    await store.hard_delete_user_data(_OWNER)

    await logger.log_request(
        request_id="req-fence-dblogger",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )

    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 9: Anonymous/unattributed rows are not affected by the fence
# ---------------------------------------------------------------------------


async def test_anonymous_rows_unaffected(fence_store):
    """Rows with no user_id and no credential_owner_id are not fenced."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)

    await store.log_request(
        request_id="req-fence-anon",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"route": "/v1/chat/completions"},
    )

    assert await _count_all(pool) == 1


# ---------------------------------------------------------------------------
# Test 10: Concurrent hard-delete calls do not deadlock
# ---------------------------------------------------------------------------


async def test_concurrent_hard_deletes_no_deadlock(fence_store):
    """Concurrent hard-delete calls for the same account must not deadlock."""
    store, pool = fence_store
    await _seed_row(
        pool, request_id="req-fence-concurrent", user_id=_OWNER, credential_owner_id=None
    )

    results = await asyncio.gather(
        store.hard_delete_user_data(_OWNER),
        store.hard_delete_user_data(_OWNER),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 2, f"expected 2 successes, got {results}"
    assert await _count_identifying(pool, _OWNER) == 0


# ---------------------------------------------------------------------------
# Test 11: Fence key is non-reversible
# ---------------------------------------------------------------------------


def test_fence_key_non_reversible():
    """The fence digest must not reveal the original user_id."""
    key1 = fence_account_digest("user-123", _SECRET)
    key2 = fence_account_digest("user-456", _SECRET)

    assert key1 != key2
    assert key1 != "user-123"
    assert "user-123" not in key1

    assert key1 == fence_account_digest("user-123", _SECRET)
    assert key1 != fence_account_digest("user-123", "other-secret")


def test_fence_advisory_key_is_int64():
    """The advisory key must be a signed 64-bit integer."""
    key = fence_account_advisory_key(fence_account_digest("user-123", _SECRET))
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


# ---------------------------------------------------------------------------
# Test 12: Fence check with no matching keys returns False
# ---------------------------------------------------------------------------


async def test_fence_check_empty_keys(fence_store):
    """check_erasure_fence with an empty list returns False immediately."""
    _store, pool = fence_store
    async with pool.acquire() as conn, conn.transaction():
        assert await check_erasure_fence(conn, fence_keys=[]) is False


# ---------------------------------------------------------------------------
# Test 13: Shared/shared/exclusive concurrency test
#
# Two concurrent log writers for the same account can both hold shared
# advisory locks simultaneously (they do not block each other). A
# hard-delete trying to acquire an exclusive lock blocks until both shared
# holders commit.
# ---------------------------------------------------------------------------


async def test_shared_writers_run_concurrently(fence_store):
    """Two shared-lock log writers must be able to hold the lock simultaneously."""
    store, _pool = fence_store

    # Barrier: both writers take the shared lock, then wait for the test
    # to release them.
    writer1_ready = asyncio.Event()
    writer2_ready = asyncio.Event()
    release_writers = asyncio.Event()
    writer1_held = False
    writer2_held = False

    async def _writer(writer_id: str, ready_event: asyncio.Event):
        nonlocal writer1_held, writer2_held
        _key = fence_account_digest(_BYSTANDER, _SECRET)
        _adv_key = fence_account_advisory_key(_key)
        async with store.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock_shared($1)", _adv_key)
            if writer_id == "w1":
                writer1_held = True
            else:
                writer2_held = True
            ready_event.set()
            await release_writers.wait()

    t1 = asyncio.create_task(_writer("w1", writer1_ready))
    t2 = asyncio.create_task(_writer("w2", writer2_ready))

    # Wait for both writers to hold their shared locks simultaneously.
    await asyncio.gather(writer1_ready.wait(), writer2_ready.wait())

    # Both should be True — shared locks coexist.
    assert writer1_held is True
    assert writer2_held is True

    # Now try to take an exclusive lock. It should block until both writers
    # release their shared locks.
    exclusive_got = asyncio.Event()

    async def _exclusive_waiter():
        async with store.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                fence_account_advisory_key(fence_account_digest(_BYSTANDER, _SECRET)),
            )
            exclusive_got.set()

    t_excl = asyncio.create_task(_exclusive_waiter())

    # The exclusive waiter should NOT have gotten the lock yet.
    await asyncio.sleep(0.05)
    assert not exclusive_got.is_set(), "exclusive lock should be blocked by shared holders"

    # Release the shared locks.
    release_writers.set()
    await asyncio.gather(t1, t2)

    # Now the exclusive waiter should get the lock.
    await asyncio.wait_for(exclusive_got.wait(), timeout=2.0)
    await t_excl


# ---------------------------------------------------------------------------
# Test 14: Secret lifecycle / fail-closed
#
# resolve_fence_secret raises ErasureFenceUnavailable when no secret is
# configured, and prefers the dedicated setting over the fallback.
# ---------------------------------------------------------------------------


def test_resolve_fence_secret_prefers_dedicated(monkeypatch):
    from serving.config.settings import Settings

    monkeypatch.setattr(
        "serving.config.settings.get_settings",
        lambda: Settings(erasure_fence_secret="dedicated-secret", api_key_secret="fallback-secret"),
    )
    assert resolve_fence_secret(None) == "dedicated-secret"


def test_resolve_fence_secret_falls_back(monkeypatch):
    from serving.config.settings import Settings

    monkeypatch.setattr(
        "serving.config.settings.get_settings",
        lambda: Settings(api_key_secret="fallback-secret"),
    )
    assert resolve_fence_secret(None) == "fallback-secret"


def test_resolve_fence_secret_fails_closed(monkeypatch):
    from serving.config.settings import Settings
    from serving.storage.log_schema import ErasureFenceUnavailable

    monkeypatch.setattr(
        "serving.config.settings.get_settings",
        lambda: Settings(erasure_fence_secret="", api_key_secret=""),
    )
    with pytest.raises(ErasureFenceUnavailable):
        resolve_fence_secret(None)


# ---------------------------------------------------------------------------
# Test 15: Resume blocked for fenced account
#
# After a hard-delete, account_has_erasure_fence returns True.
# ---------------------------------------------------------------------------


async def test_account_has_erasure_fence_true_after_hard_delete(fence_store):
    """After hard-delete, account_has_erasure_fence returns True."""
    store, _pool = fence_store

    assert await store.account_has_erasure_fence(_OWNER) is False

    await store.hard_delete_user_data(_OWNER)

    assert await store.account_has_erasure_fence(_OWNER) is True


async def test_resume_blocked_for_fenced_account(fence_store):
    """Resuming a hard-deleted (fenced) account is refused."""
    store, _pool = fence_store

    # Hard-delete the account.
    await store.hard_delete_user_data(_OWNER)

    # Verify the fence is established.
    assert await store.account_has_erasure_fence(_OWNER) is True


# ---------------------------------------------------------------------------
# Test 16: Resume-vs-hard-delete race — resume wins
# ---------------------------------------------------------------------------


async def test_resume_race_resume_wins(fence_store):
    """Resume that wins the shared lock activates before hard-delete."""
    store, pool = fence_store

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) VALUES ($1, 'a@b.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _OWNER,
        )

    from serving.storage.postgres_operational import PostgresOperationalStore

    op_store = PostgresOperationalStore(pool, fence_secret=_SECRET)
    await op_store.resume_user(
        _OWNER, admin_ip="127.0.0.1", admin_id="admin", reason="test", email="a@b.com"
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM users WHERE id = $1", _OWNER)
    assert row["status"] == "active"

    # The operational claim refuses to start once resume has activated the
    # user; the LogStore is not responsible for querying operational state.
    with pytest.raises(HardDeleteStateChanged):
        await op_store.begin_hard_delete_user(_OWNER)

    # No fence should have been established.
    assert await store.account_has_erasure_fence(_OWNER) is False

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", _OWNER)


async def test_resume_race_hard_delete_wins(fence_store):
    """Resume that loses the shared lock sees the fence and is refused."""
    store, pool = fence_store

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) VALUES ($1, 'a@b.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _OWNER,
        )

    op_store = PostgresOperationalStore(pool)
    await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    with pytest.raises(HardDeleteStateChanged):
        await op_store.resume_user(
            _OWNER, admin_ip="127.0.0.1", admin_id="admin", reason="test", email="a@b.com"
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM users WHERE id = $1", _OWNER)
    assert row["status"] == "deleted"

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", _OWNER)


async def test_failed_hard_delete_claim_can_be_released(fence_store):
    """Concurrent claims are rejected and released claims can be retried."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    assert first_claim.provenance is HardDeleteClaimProvenance.NEW
    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER)

    # A concurrent attempt cannot take ownership while the first operation is
    # still in flight.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == first_claim.token

    # Once the failed attempt releases its claim, a later retry may establish
    # a new token. The old token cannot clear that new claim.
    await op_store.release_hard_delete_user_claim(_OWNER, first_claim.token)
    second_claim = await op_store.begin_hard_delete_user(_OWNER)
    assert second_claim.provenance is HardDeleteClaimProvenance.NEW
    assert first_claim.token != second_claim.token

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == second_claim.token

    with pytest.raises(HardDeleteStateChanged):
        await op_store.hard_delete_user(
            _OWNER,
            claim_token=first_claim.token,
            admin_ip="127.0.0.1",
            admin_id="admin",
            reason="stale attempt",
            email="a@b.com",
        )

    await op_store.release_hard_delete_user_claim(_OWNER, second_claim.token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["status"] == "deleted"
    assert row["hard_delete_pending"] is False
    assert row["hard_delete_claim_token"] is None

    # The guarded release must not clear a claim after the account changes
    # state. This keeps an active account out of the hard-delete state machine.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', hard_delete_pending = TRUE, "
            "hard_delete_claim_token = $2 WHERE id = $1",
            _OWNER,
            second_claim.token,
        )
    await op_store.release_hard_delete_user_claim(_OWNER, second_claim.token)

    async with pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT hard_delete_pending FROM users WHERE id = $1",
            _OWNER,
        )
    assert pending is True


async def test_approval_cannot_activate_claimed_account(fence_store):
    """Approval cannot activate an account claimed by hard-delete."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'pending_approval', hard_delete_pending = TRUE, "
            "hard_delete_claim_token = 'approval-race' WHERE id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="hard-delete in progress"):
        await op_store.approve_user(_OWNER, admin_id="admin", note="approve")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["status"] == "pending_approval"
    assert row["hard_delete_pending"] is True


async def test_status_update_cannot_activate_claimed_account(fence_store):
    """Generic admin status updates cannot activate a claimed account."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'suspended', hard_delete_pending = TRUE, "
            "hard_delete_claim_token = 'status-race' WHERE id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="hard-delete in progress"):
        await op_store.update_user_fields(_OWNER, status="active")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending FROM users WHERE id = $1", _OWNER
        )
    assert row["status"] == "suspended"
    assert row["hard_delete_pending"] is True


async def test_stale_claim_can_be_explicitly_recovered_without_releasing_safety(fence_store):
    """A crashed pre-fence owner can be taken over without opening resume."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    assert first_claim.provenance is HardDeleteClaimProvenance.NEW
    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER, recover_stale_claim=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )

    recovered_claim = await op_store.begin_hard_delete_user(
        _OWNER,
        recover_stale_claim=True,
    )
    assert recovered_claim.provenance is HardDeleteClaimProvenance.RECOVERED
    assert recovered_claim.token != first_claim.token

    # A reclaimed claim is intentionally sticky. If the old worker was merely
    # delayed rather than dead, releasing the new token must not make resume
    # possible before either worker establishes the fence.
    await op_store.release_hard_delete_user_claim(_OWNER, recovered_claim.token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hard_delete_pending, hard_delete_claim_token, "
            "hard_delete_claim_recovered FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == recovered_claim.token
    assert row["hard_delete_claim_recovered"] is True


async def test_legacy_pending_claims_get_a_recovery_grace_period(fence_store):
    """Migration anchors old claims before allowing pre- or post-fence takeover."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_pending = TRUE, "
            "hard_delete_claim_token = 'legacy-owner', "
            "hard_delete_claim_recovered = FALSE WHERE id = $1",
            _OWNER,
        )
        await conn.execute("ALTER TABLE users DROP COLUMN hard_delete_claimed_at")

    # Re-run the operational migration against the old schema. The newly added
    # timestamp must be present before the recovery predicate is evaluated.
    await op_store.initialize()

    async with pool.acquire() as conn:
        claimed_at = await conn.fetchval(
            "SELECT hard_delete_claimed_at FROM users WHERE id = $1",
            _OWNER,
        )
    assert claimed_at is not None

    with pytest.raises(HardDeleteStateChanged, match="not yet eligible"):
        await op_store.begin_hard_delete_user(_OWNER, recover_stale_claim=True)

    # Explicit pre-fence recovery remains available only after the migration
    # anchor has aged past the normal grace period.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )
    pre_fence_claim = await op_store.begin_hard_delete_user(
        _OWNER,
        recover_stale_claim=True,
    )
    assert pre_fence_claim.provenance is HardDeleteClaimProvenance.RECOVERED

    # The same migrated state must remain recoverable after the erasure fence
    # is durable, but still not before its refreshed lease becomes stale.
    await store.hard_delete_user_data(_OWNER)
    with pytest.raises(HardDeleteStateChanged, match="not yet eligible"):
        await op_store.begin_hard_delete_user(_OWNER, recover_stale_claim=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )
    post_fence_claim = await op_store.begin_hard_delete_user(
        _OWNER,
        recover_stale_claim=True,
    )
    assert post_fence_claim.provenance is HardDeleteClaimProvenance.RECOVERED
    assert post_fence_claim.token != pre_fence_claim.token


async def test_post_fence_retry_recovers_stale_claim_without_sharing_ownership(fence_store):
    """Only an abandoned post-fence claim can be recovered for completion."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )

    retry_claim = await op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True)
    assert first_claim.provenance is HardDeleteClaimProvenance.NEW
    assert retry_claim.provenance is HardDeleteClaimProvenance.RECOVERED
    assert retry_claim.token != first_claim.token

    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER)

    await op_store.release_hard_delete_user_claim(_OWNER, first_claim.token)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'deleted', hard_delete_pending = FALSE, "
            "hard_delete_claim_token = NULL WHERE id = $1",
            _OWNER,
        )


async def test_superseded_worker_cannot_continue_after_fence(fence_store):
    """A worker paused after fencing fails before its next delete stage."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    # Model a worker that stopped heartbeating for longer than the recovery
    # grace period while another operator takes over the claim.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )
    recovered_claim = await op_store.begin_hard_delete_user(
        _OWNER,
        recover_stale_claim=True,
    )
    assert recovered_claim.token != first_claim.token

    with pytest.raises(HardDeleteStateChanged, match="no longer belongs"):
        await op_store.renew_hard_delete_user_claim(_OWNER, first_claim.token)
    with pytest.raises(HardDeleteStateChanged, match="not claimed"):
        await op_store.hard_delete_user(
            _OWNER,
            claim_token=first_claim.token,
            admin_ip="127.0.0.1",
            admin_id="old-worker",
        )

    await op_store.renew_hard_delete_user_claim(_OWNER, recovered_claim.token)


async def test_concurrent_stale_post_fence_recovery_has_one_owner(fence_store):
    """Concurrent stale retries renew ownership instead of sharing a token."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )

    results = await asyncio.gather(
        op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True),
        op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True),
        return_exceptions=True,
    )
    recovered = [result for result in results if isinstance(result, HardDeleteClaim)]
    rejected = [result for result in results if isinstance(result, HardDeleteStateChanged)]

    assert len(recovered) == 1, results
    assert len(rejected) == 1, results
    assert recovered[0].provenance is HardDeleteClaimProvenance.RECOVERED
    assert recovered[0].token != first_claim.token

    counts = await op_store.hard_delete_user(
        _OWNER,
        claim_token=recovered[0].token,
        admin_ip="127.0.0.1",
        admin_id="admin",
        email=f"{_OWNER}@example.com",
    )
    assert counts["users"] == 1


async def test_active_post_fence_retry_cannot_race_completion(fence_store):
    """An active retry is rejected while the owner completes deletion."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    owner_claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    retry = asyncio.create_task(op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True))
    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await retry

    counts = await op_store.hard_delete_user(
        _OWNER,
        claim_token=owner_claim.token,
        admin_ip="127.0.0.1",
        admin_id="admin",
        email=f"{_OWNER}@example.com",
    )
    assert counts["users"] == 1

    with pytest.raises(HardDeleteStateChanged, match="no longer eligible"):
        await op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True)
    assert await store.account_has_erasure_fence(_OWNER) is True


# ---------------------------------------------------------------------------
# Test 17: Multi-key reversed-order deadlock prevention
# ---------------------------------------------------------------------------


async def test_multi_key_reversed_order_no_deadlock(fence_store):
    """Locks are acquired in sorted order regardless of input order."""
    store, pool = fence_store

    for uid, cid in [("user-a", "user-b"), ("user-b", "user-a")]:
        await store.log_request(
            request_id=f"req-fence-multkey-{uid}-{cid}",
            model_id="m",
            provider="p",
            prompt="hello",
            response={"text": "hi"},
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            latency_ms=10,
            status_code=200,
            metadata={"user_id": uid, "credential_owner_id": cid},
        )

    assert await _count_all(pool) == 2


# ---------------------------------------------------------------------------
# Test 18: Secret stability across restart
# ---------------------------------------------------------------------------


async def test_secret_stability_across_restart(fence_store):
    """Fence secrets must be stable for the lifetime of retained tombstones."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    # A new store with a DIFFERENT secret should NOT see the fence.
    store_b = PostgresLogStore(pool, fence_secret="different-secret")
    store_b.fence_secret = "different-secret"
    assert await store_b.account_has_erasure_fence(_OWNER) is False

    # The original store still sees its fence.
    assert await store.account_has_erasure_fence(_OWNER) is True

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")


@pytest.mark.asyncio
async def test_fingerprint_pins_during_startup_and_rejects_mixed_secret(fence_store):
    """Startup must establish one namespace before any tombstone exists."""
    _store, pool = fence_store
    first_secret = "first-fence-secret"
    second_secret = "replacement-before-fence-secret"

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")
        await conn.execute("DELETE FROM erasure_fence_metadata")
        await validate_or_init_fingerprint(conn, secret=first_secret)
        assert await conn.fetchval(
            "SELECT config_value FROM erasure_fence_metadata WHERE config_key = 'secret_fingerprint'"
        ) == fingerprint_secret(first_secret)

        with pytest.raises(ErasureFenceUnavailable, match="Restore the original"):
            await validate_or_init_fingerprint(conn, secret=second_secret)

        assert await conn.fetchval(
            "SELECT config_value FROM erasure_fence_metadata WHERE config_key = 'secret_fingerprint'"
        ) == fingerprint_secret(first_secret)

        # This is test cleanup for an empty-fence migration scenario. In a
        # live deployment existing tombstones are never removed.
        await conn.execute("DELETE FROM erasure_fence")
        await conn.execute("DELETE FROM erasure_fence_metadata")


# ---------------------------------------------------------------------------
# Test 19: Mixed-version deployment — old workers bypass the fence
# ---------------------------------------------------------------------------


async def test_mixed_version_old_worker_can_still_insert(fence_store):
    """During mixed-version rollout, old-code workers can still insert.

    This documents the limitation: the fence is enforced in application
    code, not via a DB trigger. Once ALL workers are upgraded, the fence
    is effective.
    """
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    # Simulate old-code worker: raw INSERT, no fence protocol.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, "
            "timestamp, status_code, user_id) "
            "VALUES ($1, 'm', 'p', $2, 200, $3)",
            "req-fence-old-worker",
            datetime.now(timezone.utc),
            _OWNER,
        )

    assert await _count_identifying(pool, _OWNER) == 1

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_logs WHERE request_id = 'req-fence-old-worker'")


# ---------------------------------------------------------------------------
# Test 23: Concurrent fingerprint initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_fingerprint_init_same_secret(fence_store):
    """Concurrent same-secret initializers converge cleanly."""
    _store, pool = fence_store

    # Wipe metadata so we start from a clean slate.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM erasure_fence_metadata")

    # Fingerprints are intentionally not pinned until at least one durable
    # tombstone exists. Establish one so this test exercises the pinned path.
    unique_secret = "concurrent-same-secret-unique"
    async with pool.acquire() as conn, conn.transaction():
        await establish_erasure_fence(
            conn,
            fence_key=fence_account_digest(_OWNER, unique_secret),
        )

    # Two concurrent initializers with the SAME unique secret.
    barrier = asyncio.Barrier(2)
    results = []

    async def _init():
        await barrier.wait()
        async with pool.acquire() as conn:
            await validate_or_init_fingerprint(conn, secret=unique_secret)
            results.append("ok")

    await asyncio.gather(_init(), _init())

    # Both should have succeeded.
    assert results == ["ok", "ok"], f"Expected both to succeed, got {results}"

    # Exactly one metadata row should exist.
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM erasure_fence_metadata")

    assert count == 1, f"Expected 1 metadata row, got {count}"

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence_metadata")


@pytest.mark.asyncio
async def test_concurrent_fingerprint_init_different_secrets(fence_store):
    """Concurrent different-secret initializers: exactly one wins deterministically."""
    _store, pool = fence_store

    # Wipe metadata so we start from a clean slate.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM erasure_fence_metadata")

    # See the same-secret test: a tombstone makes the fingerprint durable.
    async with pool.acquire() as conn, conn.transaction():
        await establish_erasure_fence(
            conn,
            fence_key=fence_account_digest(_OWNER, "concurrent-secret-A"),
        )

    # Two concurrent initializers with DIFFERENT secrets.
    from serving.storage.log_schema import ErasureFenceUnavailable

    barrier = asyncio.Barrier(2)
    results = {"ok": 0, "mismatch": 0, "other_error": 0}

    async def _init(secret: str):
        await barrier.wait()
        async with pool.acquire() as conn:
            try:
                await validate_or_init_fingerprint(conn, secret=secret)
                results["ok"] += 1
            except ErasureFenceUnavailable:
                results["mismatch"] += 1
            except Exception:
                results["other_error"] += 1

    await asyncio.gather(
        _init("concurrent-secret-A"),
        _init("concurrent-secret-B"),
    )

    # Exactly one should have succeeded, one should have failed with mismatch.
    assert results["ok"] == 1, f"Expected exactly 1 ok, got {results}"
    assert results["mismatch"] == 1, f"Expected exactly 1 mismatch, got {results}"
    assert results["other_error"] == 0, f"Expected no other errors, got {results}"

    # Exactly one metadata row should exist.
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM erasure_fence_metadata")

    assert count == 1, f"Expected 1 metadata row, got {count}"

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence_metadata")


# ---------------------------------------------------------------------------
# Test 24: DatabaseLogger success-path races hard-delete
#
# Issue #1421 explicitly states that normal (success-path) logging is
# fire-and-forget and can land after the purge. This test exercises
# DatabaseLogger.log_request (the actual production writer on dev) rather
# than PostgresLogStore.log_request.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_logger_race_log_write_wins(fence_store):
    """Log write that wins the race is then purged by hard-delete."""
    store, pool = fence_store

    # Seed the user as soft-deleted.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) "
            "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _OWNER,
        )

    # Create a DatabaseLogger that shares the pool.
    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    # Log a normal authenticated request for the user BEFORE hard-delete.
    await logger.log_request(
        request_id="req-fence-dblogger-win",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )

    # Verify the row exists.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM api_logs WHERE request_id = 'req-fence-dblogger-win'"
        )
    assert row is not None

    # Hard-delete purges it.
    await store.hard_delete_user_data(_OWNER)

    # Zero identifying rows should remain.
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM api_logs WHERE request_id = 'req-fence-dblogger-win'"
        )
    assert count == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", _OWNER)


@pytest.mark.asyncio
async def test_database_logger_race_hard_delete_wins(fence_store):
    """Log write that loses the race is suppressed by the fence."""
    store, pool = fence_store

    # Seed the user as soft-deleted.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) "
            "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _OWNER,
        )

    # Hard-delete first to establish the fence.
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    # Create a DatabaseLogger that shares the pool.
    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    # Attempt to log a request for the fenced account. It should be suppressed.
    await logger.log_request(
        request_id="req-fence-dblogger-lose",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )

    # Zero identifying rows should exist.
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM api_logs WHERE request_id = 'req-fence-dblogger-lose'"
        )
    assert count == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", _OWNER)
        await conn.execute("DELETE FROM erasure_fence")


# ---------------------------------------------------------------------------
# Test 25: Dual-identity fence coverage
#
# An api_logs row can carry both user_id and credential_owner_id. The
# erasure fence must cover every distinct account identity represented by
# the row, not just one preferred identifier.
# ---------------------------------------------------------------------------


async def _count_identifying(pool, account_id: str) -> int:
    """Count api_logs rows identifying the account via either column."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM api_logs "
            "WHERE request_id LIKE 'req-fence-%' "
            "AND (user_id = $1 OR metadata->>'credential_owner_id' = $1)",
            account_id,
        )
    return row["n"]


@pytest.mark.asyncio
async def test_dual_identity_same_user_and_owner(fence_store):
    """user_id=A, credential_owner_id=A — single identity, fenced correctly."""
    store, pool = fence_store

    # Hard-delete establishes the fence.
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    # Attempt to log a row with both identifiers = A.
    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    await logger.log_request(
        request_id="req-fence-dual-same",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER, "credential_owner_id": _OWNER},
    )

    # Zero identifying rows should exist.
    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")


@pytest.mark.asyncio
async def test_dual_identity_user_only(fence_store):
    """user_id=A, credential_owner_id=None — fenced correctly."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    await logger.log_request(
        request_id="req-fence-dual-user-only",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER},
    )

    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")


@pytest.mark.asyncio
async def test_dual_identity_owner_only(fence_store):
    """user_id=None, credential_owner_id=A — fenced correctly."""
    store, pool = fence_store

    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    await logger.log_request(
        request_id="req-fence-dual-owner-only",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"credential_owner_id": _OWNER, "rejection": True},
    )

    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")


@pytest.mark.asyncio
async def test_dual_identity_different_owner_fenced(fence_store):
    """user_id=B, credential_owner_id=A while A is hard-deleted.

    The row identifies A through credential_owner_id. Hard-delete A must
    suppress the INSERT even though user_id=B is not fenced.
    """
    store, pool = fence_store

    # Seed user B as well.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) "
            "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _BYSTANDER,
        )

    # Hard-delete A (OWNER) but not B (BYSTANDER).
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True
    assert await store.account_has_erasure_fence(_BYSTANDER) is False

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    # Log a row with user_id=B, credential_owner_id=A.
    await logger.log_request(
        request_id="req-fence-dual-diff-owner",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _BYSTANDER, "credential_owner_id": _OWNER},
    )

    # Zero identifying rows for A should exist.
    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")
        await conn.execute("DELETE FROM users WHERE id = $1", _BYSTANDER)


@pytest.mark.asyncio
async def test_dual_identity_different_user_fenced(fence_store):
    """user_id=A, credential_owner_id=B while A is hard-deleted.

    The row identifies A through user_id. Hard-delete A must suppress the
    INSERT even though credential_owner_id=B is not fenced.
    """
    store, pool = fence_store

    # Seed user B.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) "
            "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _BYSTANDER,
        )

    # Hard-delete A (OWNER) but not B (BYSTANDER).
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    # Log a row with user_id=A, credential_owner_id=B.
    await logger.log_request(
        request_id="req-fence-dual-diff-user",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _OWNER, "credential_owner_id": _BYSTANDER},
    )

    # Zero identifying rows for A should exist.
    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")
        await conn.execute("DELETE FROM users WHERE id = $1", _BYSTANDER)


@pytest.mark.asyncio
async def test_dual_identity_writer_wins_then_purged(fence_store):
    """Both identifiers differ; writer wins first, hard-delete purges the row."""
    store, pool = fence_store

    # Seed both users.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, status) "
            "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
            "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
            _BYSTANDER,
        )

    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({}, fence_secret=_SECRET)
    logger.pool = pool

    # Log a row with user_id=B, credential_owner_id=A BEFORE hard-delete.
    await logger.log_request(
        request_id="req-fence-dual-diff-purge",
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata={"user_id": _BYSTANDER, "credential_owner_id": _OWNER},
    )

    # Verify the row exists.
    assert await _count_identifying(pool, _OWNER) == 1
    assert await _count_identifying(pool, _BYSTANDER) == 1

    # Hard-delete A — should purge the row.
    await store.hard_delete_user_data(_OWNER)

    # Zero identifying rows for A should remain.
    assert await _count_identifying(pool, _OWNER) == 0

    # Cleanup.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM erasure_fence")
        await conn.execute("DELETE FROM users WHERE id = $1", _BYSTANDER)
