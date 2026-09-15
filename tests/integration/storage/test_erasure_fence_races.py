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
import hashlib
import json
import os
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.base import HardDeleteClaim, HardDeleteClaimProvenance
from serving.storage.database import DatabaseLogger
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
    # The operational initializer owns the table but the legacy logger owns
    # the api_key_encrypted migration. Keep the fixture's schema representative
    # of the production bootstrap before exercising key INSERTs.
    legacy_logger = DatabaseLogger({}, fence_secret=_SECRET)
    legacy_logger.pool = pool
    await legacy_logger._create_tables()

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


@pytest_asyncio.fixture
async def separate_store_schemas(request):
    """Provide LogStore and OperationalStore pools over separate schemas."""
    db_config = _test_db_config()
    schema_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:12]
    operational_schema = f"fence_op_{schema_digest}"
    log_schema = f"fence_log_{schema_digest}"

    try:
        admin_conn = await asyncpg.connect(**db_config)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{operational_schema}" CASCADE')
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{log_schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{operational_schema}"')
        await admin_conn.execute(f'CREATE SCHEMA "{log_schema}"')
    finally:
        await admin_conn.close()

    operational_pool = None
    log_pool = None
    try:
        operational_pool = await asyncpg.create_pool(
            **db_config,
            min_size=2,
            max_size=5,
            server_settings={"search_path": operational_schema},
        )
        log_pool = await asyncpg.create_pool(
            **db_config,
            min_size=2,
            max_size=5,
            server_settings={"search_path": log_schema},
        )
        log_store = PostgresLogStore(log_pool, fence_secret=_SECRET)
        await log_store.initialize()
        op_store = PostgresOperationalStore(operational_pool)
        await op_store.initialize()
        async with operational_pool.acquire() as conn:
            # This column is normally added by DatabaseLogger, which is not
            # present in the operational-only database for this test.
            await conn.execute(
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS api_key_encrypted TEXT"
            )
        yield op_store, log_store, operational_pool, log_pool
    finally:
        if operational_pool is not None:
            await operational_pool.close()
        if log_pool is not None:
            await log_pool.close()
        admin_conn = await asyncpg.connect(**db_config)
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{operational_schema}" CASCADE')
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{log_schema}" CASCADE')
        finally:
            await admin_conn.close()


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
        lambda: Settings(),
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


async def test_resume_missing_user_fails_without_target_audit(fence_store):
    """A missing user cannot be resumed or acquire a target audit row."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", _OWNER)
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="no longer exists"):
        await op_store.resume_user(
            _OWNER,
            admin_ip="127.0.0.1",
            admin_id="admin",
            reason="stale resume",
            email="owner@example.com",
        )

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )
    assert after == before


async def test_resume_wrong_status_fails_without_target_audit(fence_store):
    """Only a deleted, unclaimed row can be resumed."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', hard_delete_pending = FALSE, "
            "hard_delete_claim_token = NULL WHERE id = $1",
            _OWNER,
        )
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="cannot be mutated"):
        await op_store.resume_user(
            _OWNER,
            admin_ip="127.0.0.1",
            admin_id="admin",
            reason="wrong status",
            email="owner@example.com",
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM users WHERE id = $1", _OWNER)
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )
    assert row["status"] == "active"
    assert after == before


async def test_stale_soft_delete_after_hard_delete_cannot_recreate_identity(fence_store):
    """A stale soft-delete cannot recreate a purged user or target audit row."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM admin_audit_log WHERE action IN "
            "('stale_mutation_unverified', 'stale_mutation_unknown')"
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', hard_delete_pending = FALSE, "
            "hard_delete_claim_token = NULL WHERE id = $1",
            _OWNER,
        )
    snapshot = await op_store.get_user_by_id(_OWNER)
    assert snapshot is not None
    await op_store.delete_user(
        _OWNER,
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="initial soft delete",
        email=snapshot["email"],
    )
    claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    await op_store.hard_delete_user(
        _OWNER,
        claim_token=claim.token,
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="permanent delete",
        email=snapshot["email"],
    )

    async with pool.acquire() as conn:
        audit_before = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="no longer exists"):
        await op_store.delete_user(
            _OWNER,
            admin_ip="127.0.0.1",
            admin_id="admin",
            reason="stale soft delete",
            email=snapshot["email"],
        )

    await op_store.log_admin_action(
        admin_ip="127.0.0.1",
        action="stale_mutation",
        target_user_id=_OWNER,
        target_missing_identity_fenced=True,
    )
    await op_store.log_admin_action(
        admin_ip="127.0.0.1",
        action="stale_mutation_unverified",
        target_user_id=_OWNER,
        details={"reason": "stale pre-transaction fence result"},
        target_missing_identity_fenced=False,
    )
    await op_store.log_admin_action(
        admin_ip="127.0.0.1",
        action="stale_mutation_unknown",
        target_user_id=_OWNER,
        target_missing_identity_fenced=None,
    )

    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE id = $1", _OWNER)
        key_count = await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", _OWNER)
        session_count = await conn.fetchval(
            "SELECT COUNT(*) FROM auth_sessions WHERE user_id = $1", _OWNER
        )
        token_count = await conn.fetchval(
            "SELECT COUNT(*) FROM email_verification_tokens WHERE user_id = $1",
            _OWNER,
        )
        token_count += await conn.fetchval(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = $1",
            _OWNER,
        )
        audit_after = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )
        redacted = await conn.fetch(
            "SELECT action, target_user_id, details->>'target_user_id_redacted' AS redaction "
            "FROM admin_audit_log WHERE action IN "
            "('stale_mutation_unverified', 'stale_mutation_unknown') "
            "ORDER BY action"
        )
    assert user_count == 0
    assert key_count == 0
    assert session_count == 0
    assert token_count == 0
    assert audit_after == audit_before
    assert [(row["action"], row["target_user_id"], row["redaction"]) for row in redacted] == [
        ("stale_mutation_unknown", None, "missing_identity"),
        ("stale_mutation_unverified", None, "missing_identity"),
    ]


async def test_stale_admin_update_after_hard_delete_cannot_recreate_identity(fence_store):
    """Stale user/key/preference updates fail after permanent removal."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', hard_delete_pending = FALSE, "
            "hard_delete_claim_token = NULL WHERE id = $1",
            _OWNER,
        )
    snapshot = await op_store.get_user_by_id(_OWNER)
    assert snapshot is not None
    await op_store.delete_user(
        _OWNER,
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="initial soft delete",
        email=snapshot["email"],
    )
    claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    await op_store.hard_delete_user(
        _OWNER,
        claim_token=claim.token,
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="permanent delete",
        email=snapshot["email"],
    )

    async with pool.acquire() as conn:
        audit_before = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )

    stale_mutations = (
        lambda: op_store.update_user_fields(_OWNER, admin_note="stale update"),
        lambda: op_store.update_user_preferences(_OWNER, {"theme": "dark"}),
        lambda: op_store.create_key(
            key_hash="hash", key_prefix="prefix", user_id=_OWNER, missing_identity_fenced=True
        ),
        lambda: op_store.update_key(_OWNER, quota_daily_cost_usd=10, missing_identity_fenced=True),
        lambda: op_store.regenerate_key(
            _OWNER,
            new_key_hash="new-hash",
            new_key_prefix="new-prefix",
            missing_identity_fenced=True,
        ),
    )
    for mutation in stale_mutations:
        with pytest.raises(HardDeleteStateChanged, match="no longer exists"):
            await mutation()

    # Removing a stale credential is safe even after the identity's fence is
    # durable. There are no keys left here, so this is intentionally a no-op.
    await op_store.revoke_key(_OWNER, missing_identity_fenced=True)

    await op_store.log_admin_action(
        admin_ip="127.0.0.1",
        action="stale_update",
        target_user_id=_OWNER,
        target_missing_identity_fenced=True,
    )

    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE id = $1", _OWNER)
        key_count = await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", _OWNER)
        audit_after = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )
    assert user_count == 0
    assert key_count == 0
    assert audit_after == audit_before


@pytest.mark.asyncio
async def test_key_only_identity_lifecycle_and_audit_redacts_missing_identity(fence_store):
    """Legacy key-only credentials remain manageable while missing audit targets redact."""
    _store, pool = fence_store
    user_id = "u-fence-key-only-lifecycle"
    op_store = PostgresOperationalStore(pool)

    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action IN "
                "('key_only_fenced_revoke', 'legacy_key_only_fenced_revoke', "
                "'key_only_update', 'legacy_key_only_update')"
            )

        with pytest.raises(HardDeleteStateChanged, match="no longer exists"):
            await op_store.create_key(
                key_hash="key-only-hash-new",
                key_prefix="key-only-prefix-new",
                user_id=user_id,
                missing_identity_fenced=False,
            )

        # This is a legacy row that predates the transactional user-identity
        # guard; it is seeded directly so the test does not use the guarded
        # new-key creation path to manufacture a missing identity.
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, account_id) "
                "VALUES ($1, $2, $3, NULL)",
                "key-only-hash-1",
                "key-only-prefix-1",
                user_id,
            )
        await op_store.update_key(user_id, notes="legacy key-only", missing_identity_fenced=False)
        old_prefix = await op_store.regenerate_key(
            user_id,
            new_key_hash="key-only-hash-2",
            new_key_prefix="key-only-prefix-2",
            missing_identity_fenced=False,
        )
        assert old_prefix == "key-only-prefix-1"

        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action="key_only_update",
            target_user_id=user_id,
            target_missing_identity_fenced=False,
        )
        from serving.servers.auth import log_admin_action
        from serving.storage.database import DatabaseLogger

        legacy_logger = DatabaseLogger({}, fence_secret=_SECRET)
        legacy_logger.pool = pool
        await log_admin_action(
            legacy_logger,
            "127.0.0.1",
            "legacy_key_only_update",
            user_id,
            target_missing_identity_fenced=False,
        )
        # Destructive removal does not need LogStore's fence result.
        await op_store.revoke_key(user_id)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT status FROM api_keys WHERE user_id = $1", user_id)
                == "revoked"
            )
        await op_store.revoke_key(user_id, hard_delete=True)

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", user_id)
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1", user_id
                )
                == 0
            )
            redacted = await conn.fetch(
                "SELECT action, target_user_id, details->>'target_user_id_redacted' AS redaction "
                "FROM admin_audit_log WHERE action IN "
                "('key_only_update', 'legacy_key_only_update') ORDER BY action"
            )
        assert [(row["action"], row["target_user_id"], row["redaction"]) for row in redacted] == [
            ("key_only_update", None, "missing_identity"),
            ("legacy_key_only_update", None, "missing_identity"),
        ]
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)


@pytest.mark.asyncio
async def test_non_user_audit_target_is_not_redacted_as_missing_user(fence_store):
    """Provider/resource targets retain their audit identity without a user row."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)
    action = "provider_target_audit_regression"
    target = "provider-without-a-user-row"

    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", action)

        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action=action,
            target_user_id=target,
            target_is_user=False,
            details={"provider": target, "disabled": True},
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT target_user_id, details FROM admin_audit_log WHERE action = $1",
                action,
            )
        assert row["target_user_id"] == target
        details = row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details == {"provider": target, "disabled": True}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", action)


async def test_audit_after_committed_mutation_during_hard_delete_is_redacted(fence_store):
    """A claimed user audit records success without retaining identity data."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)
    action = "pending_mutation_audit_regression"
    legacy_action = f"{action}_legacy"

    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", action)
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", legacy_action)
            await conn.execute(
                "UPDATE users SET hard_delete_pending = TRUE, "
                "hard_delete_claim_token = 'live-claim' WHERE id = $1",
                _OWNER,
            )

        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action=action,
            target_user_id=_OWNER,
            details={"email": "owner@example.com", "reason": "already committed"},
            target_missing_identity_fenced=True,
        )
        from serving.servers.auth import log_admin_action
        from serving.storage.database import DatabaseLogger

        legacy_logger = DatabaseLogger({}, fence_secret=_SECRET)
        legacy_logger.pool = pool
        await log_admin_action(
            legacy_logger,
            "127.0.0.1",
            legacy_action,
            _OWNER,
            {"email": "owner@example.com", "reason": "already committed"},
            target_missing_identity_fenced=True,
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT target_user_id, details FROM admin_audit_log WHERE action = $1",
                action,
            )
            legacy_row = await conn.fetchrow(
                "SELECT target_user_id, details FROM admin_audit_log WHERE action = $1",
                legacy_action,
            )
        assert row["target_user_id"] is None
        assert legacy_row["target_user_id"] is None
        details = row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        legacy_details = legacy_row["details"]
        if isinstance(legacy_details, str):
            legacy_details = json.loads(legacy_details)
        assert details == {"target_user_id_redacted": "hard_delete_in_progress"}
        assert legacy_details == {"target_user_id_redacted": "hard_delete_in_progress"}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", action)
            await conn.execute("DELETE FROM admin_audit_log WHERE action = $1", legacy_action)
            await conn.execute(
                "UPDATE users SET hard_delete_pending = FALSE, hard_delete_claim_token = NULL "
                "WHERE id = $1",
                _OWNER,
            )


@pytest.mark.asyncio
async def test_fenced_key_only_identity_cannot_regain_credential_but_can_revoke(
    fence_store,
):
    """A durable fence blocks credential creation/mutation without blocking removal."""
    store, pool = fence_store
    user_id = "u-fence-key-only-erased"
    op_store = PostgresOperationalStore(pool)

    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action IN "
                "('key_only_fenced_revoke', 'legacy_key_only_fenced_revoke')"
            )

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, account_id) "
                "VALUES ($1, $2, $3, NULL)",
                "erased-key-hash",
                "erased-key-prefix",
                user_id,
            )
        await store.hard_delete_user_data(user_id)
        assert await store.account_has_erasure_fence(user_id) is True

        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.create_key(
                key_hash="resurrection-hash",
                key_prefix="resurrection-prefix",
                user_id=user_id,
                missing_identity_fenced=True,
            )
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.update_key(
                user_id, notes="must not change", missing_identity_fenced=True
            )
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.regenerate_key(
                user_id,
                new_key_hash="resurrection-hash-2",
                new_key_prefix="resurrection-prefix-2",
                missing_identity_fenced=True,
            )

        await op_store.revoke_key(user_id, missing_identity_fenced=True)
        from serving.servers.auth import log_admin_action
        from serving.storage.database import DatabaseLogger

        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action="key_only_fenced_revoke",
            target_user_id=user_id,
            details={"key_prefix": "erased-key-prefix"},
            target_missing_identity_fenced=True,
        )
        legacy_logger = DatabaseLogger({}, fence_secret=_SECRET)
        legacy_logger.pool = pool
        await log_admin_action(
            legacy_logger,
            "127.0.0.1",
            "legacy_key_only_fenced_revoke",
            user_id,
            {"key_prefix": "erased-key-prefix"},
            target_missing_identity_fenced=True,
        )
        await op_store.revoke_key(user_id, hard_delete=True, missing_identity_fenced=True)

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", user_id)
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1", user_id
                )
                == 0
            )
            redacted = await conn.fetchval(
                "SELECT COUNT(*) FROM admin_audit_log "
                "WHERE details->>'target_user_id_redacted' = 'erasure_fence' "
                "AND action IN ('key_only_fenced_revoke', 'legacy_key_only_fenced_revoke')"
            )
        assert redacted == 2
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action IN "
                "('key_only_fenced_revoke', 'legacy_key_only_fenced_revoke')"
            )
            await conn.execute(
                "DELETE FROM erasure_fence WHERE account_digest = $1",
                fence_account_digest(user_id, _SECRET),
            )


@pytest.mark.asyncio
async def test_separate_store_schemas_route_fence_checks_to_log_store(
    separate_store_schemas, monkeypatch
):
    """Operational key/audit writes do not require LogStore-owned tables."""
    op_store, log_store, operational_pool, log_pool = separate_store_schemas
    user_id = "u-fence-separate-store"
    fence_checks: list[str] = []
    original_check = log_store.account_has_erasure_fence

    async def _record_fence_check(checked_user_id: str) -> bool:
        fence_checks.append(checked_user_id)
        return await original_check(checked_user_id)

    monkeypatch.setattr(log_store, "account_has_erasure_fence", _record_fence_check)

    async with operational_pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('erasure_fence')") is None
    async with log_pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('erasure_fence')") == "erasure_fence"

    try:
        unfenced = await log_store.account_has_erasure_fence(user_id)
        assert unfenced is False
        async with operational_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, account_id) "
                "VALUES ($1, $2, $3, NULL)",
                "separate-store-hash-1",
                "separate-store-prefix-1",
                user_id,
            )
        await op_store.update_key(user_id, notes="separate-store", missing_identity_fenced=unfenced)
        old_prefix = await op_store.regenerate_key(
            user_id,
            new_key_hash="separate-store-hash-2",
            new_key_prefix="separate-store-prefix-2",
            missing_identity_fenced=unfenced,
        )
        assert old_prefix == "separate-store-prefix-1"
        await op_store.revoke_key(user_id)
        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action="separate_store_unfenced",
            target_user_id=user_id,
            target_missing_identity_fenced=unfenced,
        )

        await log_store.hard_delete_user_data(user_id)
        fenced = await log_store.account_has_erasure_fence(user_id)
        assert fenced is True

        with pytest.raises(HardDeleteStateChanged, match="no longer exists"):
            await op_store.create_key(
                key_hash="separate-store-stale-negative-hash",
                key_prefix="separate-store-stale-negative-prefix",
                user_id=user_id,
                missing_identity_fenced=False,
            )
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.create_key(
                key_hash="separate-store-hash-3",
                key_prefix="separate-store-prefix-3",
                user_id=user_id,
                missing_identity_fenced=fenced,
            )
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.update_key(
                user_id, notes="must not change", missing_identity_fenced=fenced
            )
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.regenerate_key(
                user_id,
                new_key_hash="separate-store-hash-4",
                new_key_prefix="separate-store-prefix-4",
                missing_identity_fenced=fenced,
            )

        # Destructive removal remains allowed for a stale fenced credential.
        await op_store.revoke_key(user_id, hard_delete=True)
        await op_store.log_admin_action(
            admin_ip="127.0.0.1",
            action="separate_store_fenced_revoke",
            target_user_id=user_id,
            target_missing_identity_fenced=fenced,
        )

        async with operational_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_audit_log "
                    "WHERE action = 'separate_store_fenced_revoke' "
                    "AND target_user_id IS NULL "
                    "AND details->>'target_user_id_redacted' = 'erasure_fence'"
                )
                == 1
            )
    finally:
        async with operational_pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action IN "
                "('separate_store_unfenced', 'separate_store_fenced_revoke')"
            )

    assert fence_checks == [user_id, user_id]


@pytest.mark.asyncio
async def test_key_only_mutation_waits_for_inflight_erasure_fence(fence_store, monkeypatch):
    """A key mutation cannot observe a false negative during fence commit."""
    _store, pool = fence_store
    user_id = "u-fence-key-only-inflight"
    op_store = PostgresOperationalStore(pool)
    fence_key = fence_account_digest(user_id, _SECRET)
    advisory_key = fence_account_advisory_key(fence_key)
    writer_ready = asyncio.Event()
    release_writer = asyncio.Event()
    check_started = asyncio.Event()

    from serving.storage import postgres_log

    original_check = postgres_log.check_erasure_fence

    async def _check_with_barrier(conn, *, fence_keys):
        check_started.set()
        return await original_check(conn, fence_keys=fence_keys)

    monkeypatch.setattr(postgres_log, "check_erasure_fence", _check_with_barrier)

    async def _writer() -> None:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", advisory_key)
            await conn.execute(
                "INSERT INTO erasure_fence (account_digest) VALUES ($1) "
                "ON CONFLICT (account_digest) DO NOTHING",
                fence_key,
            )
            writer_ready.set()
            await release_writer.wait()

    try:
        writer_task = asyncio.create_task(_writer())
        await writer_ready.wait()

        fence_task = asyncio.create_task(_store.account_has_erasure_fence(user_id))
        # The LogStore fence lookup blocks on the uncommitted exclusive writer.
        await check_started.wait()
        assert not fence_task.done()

        release_writer.set()
        await writer_task
        assert await fence_task is True
        with pytest.raises(HardDeleteStateChanged, match="erasure fence"):
            await op_store.create_key(
                key_hash="inflight-hash",
                key_prefix="inflight-prefix",
                user_id=user_id,
                missing_identity_fenced=True,
            )
    finally:
        release_writer.set()
        if "writer_task" in locals() and not writer_task.done():
            await writer_task
        if "fence_task" in locals() and not fence_task.done():
            await fence_task
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id)
            await conn.execute("DELETE FROM erasure_fence WHERE account_digest = $1", fence_key)


async def test_account_has_erasure_fence_waits_for_inflight_writer(fence_store, monkeypatch):
    """Fence verification synchronizes with an uncommitted exclusive writer."""
    store, pool = fence_store
    fence_key = fence_account_digest(_OWNER, _SECRET)
    advisory_key = fence_account_advisory_key(fence_key)
    writer_ready = asyncio.Event()
    release_writer = asyncio.Event()
    verifier_started = asyncio.Event()

    from serving.storage import postgres_log

    original_check = postgres_log.check_erasure_fence

    async def _check_with_barrier(conn, *, fence_keys):
        verifier_started.set()
        return await original_check(conn, fence_keys=fence_keys)

    monkeypatch.setattr(postgres_log, "check_erasure_fence", _check_with_barrier)

    async def _writer() -> None:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", advisory_key)
            await conn.execute(
                "INSERT INTO erasure_fence (account_digest) VALUES ($1) "
                "ON CONFLICT (account_digest) DO NOTHING",
                fence_key,
            )
            writer_ready.set()
            await release_writer.wait()

    writer_task = asyncio.create_task(_writer())
    try:
        await writer_ready.wait()
        verifier_task = asyncio.create_task(store.account_has_erasure_fence(_OWNER))
        await verifier_started.wait()
        assert not verifier_task.done()

        release_writer.set()
        await writer_task
        assert await verifier_task is True
    finally:
        release_writer.set()
        if not writer_task.done():
            await writer_task


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

    op_store = PostgresOperationalStore(pool)
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


async def test_concrete_user_mutations_reject_active_claim(fence_store):
    """Every concrete user mutation rejects before changing a claimed row."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', hard_delete_pending = TRUE, "
            "hard_delete_claim_token = 'mutation-race', preferences = '{}'::jsonb "
            "WHERE id = $1",
            _OWNER,
        )
        before_audit = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )

    mutation_calls = [
        op_store.delete_user(
            _OWNER,
            admin_ip="127.0.0.1",
            admin_id="admin",
            reason="stale delete",
            email="owner@example.com",
        ),
        op_store.update_user_fields(_OWNER, admin_note="stale update"),
        op_store.update_user_preferences(_OWNER, {"theme": "dark"}),
        op_store.create_key(key_hash="hash", key_prefix="prefix", user_id=_OWNER),
        op_store.update_key(_OWNER, quota_daily_cost_usd=10),
        op_store.revoke_key(_OWNER),
        op_store.regenerate_key(
            _OWNER,
            new_key_hash="new-hash",
            new_key_prefix="new-prefix",
        ),
    ]
    for mutation in mutation_calls:
        with pytest.raises(HardDeleteStateChanged, match="hard-delete in progress"):
            await mutation

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token, "
            "preferences = '{}'::jsonb AS empty_preferences "
            "FROM users WHERE id = $1",
            _OWNER,
        )
        after_audit = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_audit_log WHERE target_user_id = $1",
            _OWNER,
        )
        key_count = await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1", _OWNER)
    assert row["status"] == "active"
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == "mutation-race"
    assert row["empty_preferences"] is True
    assert after_audit == before_audit
    assert key_count == 0


async def test_rejection_cannot_change_claimed_account(fence_store):
    """Rejection cannot change status while hard-delete owns the claim."""
    _store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'pending_approval', hard_delete_pending = TRUE, "
            "hard_delete_claim_token = 'rejection-race' WHERE id = $1",
            _OWNER,
        )

    with pytest.raises(HardDeleteStateChanged, match="hard-delete in progress"):
        await op_store.reject_user(_OWNER, admin_id="admin", reason="not eligible")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending FROM users WHERE id = $1", _OWNER
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
    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER, recover_stale_claim=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            _OWNER,
        )

    recovered_token = await op_store.begin_hard_delete_user(
        _OWNER,
        recover_stale_claim=True,
    )
    assert recovered_token.provenance is HardDeleteClaimProvenance.RECOVERED
    assert recovered_token.token != first_claim.token

    # A reclaimed claim is intentionally sticky. If the old worker was merely
    # delayed rather than dead, releasing the new token must not make resume
    # possible before either worker establishes the fence.
    await op_store.release_hard_delete_user_claim(_OWNER, recovered_token.token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hard_delete_pending, hard_delete_claim_token, "
            "hard_delete_claim_recovered FROM users WHERE id = $1",
            _OWNER,
        )
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] == recovered_token.token
    assert row["hard_delete_claim_recovered"] is True


async def test_post_fence_retry_recovers_stale_claim_without_sharing_ownership(fence_store):
    """Only an abandoned post-fence claim can be recovered for completion."""
    store, pool = fence_store
    op_store = PostgresOperationalStore(pool)

    first_claim = await op_store.begin_hard_delete_user(_OWNER)
    await store.hard_delete_user_data(_OWNER)
    assert await store.account_has_erasure_fence(_OWNER) is True

    with pytest.raises(HardDeleteStateChanged, match="already has a hard-delete"):
        await op_store.begin_hard_delete_user(_OWNER, allow_existing_fence=True)

    # Once the original worker is considered abandoned, a post-fence retry may
    # safely finish with its token. The durable fence prevents any identifying
    # log writes while this takeover waits for the operational wipe.
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

    # This is the post-fence/pre-wipe window from the production race: the
    # second caller must not receive the owner's live token.
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

    # A retry after the owner has completed is deterministic too: the user is
    # gone, while the durable fence remains present.
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
