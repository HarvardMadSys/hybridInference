"""Batch lifecycle races against real PostgreSQL, interleaved deterministically.

Two lifecycles that cross transactions:

- a webhook removed while a sealing transaction writes an event under it
  must take that event with it, and an event left without its webhook (by an
  older version) must never stop other users' events from being delivered;
- a file batch accepted from an upload must keep that upload's bytes until it
  is validated, whatever a concurrent collection read: either the batch is
  refused because the deletion won, or the bytes stay.

Interleavings are forced with advisory-lock barriers in triggers and by
waiting until a session is seen blocked in ``pg_stat_activity``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import (
    BatchInputUnavailable,
    BatchStore,
    _enqueue_event,
    terminal_event,
)
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=10)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def users(pool):
    created: list[str] = []

    def make() -> str:
        user = f"race-{uuid.uuid4().hex[:10]}"
        created.append(user)
        return user

    try:
        yield make
    finally:
        store = BatchStore(pool)
        for user in created:
            await store.delete_user_batches(user)


async def _blocked(pool: Any, pid: int, *, kind: str) -> None:
    """Wait until session *pid* is waiting on a lock of *kind* (``Lock`` or ``advisory``)."""
    async with pool.acquire() as conn:
        for _ in range(300):
            row = await conn.fetchrow(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = $1", pid
            )
            if row and (row["wait_event_type"] == kind or row["wait_event"] == kind):
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"session {pid} never waited on a {kind} lock")


async def _session_pid(pool: Any, running: asyncio.Task, known: set[int]) -> int:
    """The backend pid of the connection a task just took from the pool."""
    async with pool.acquire() as conn:
        for _ in range(300):
            pids = {
                row["pid"]
                for row in await conn.fetch(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND state = 'active' AND pid <> pg_backend_pid()"
                )
            }
            fresh = pids - known
            if fresh:
                return fresh.pop()
            if running.done():
                raise AssertionError("the task finished without blocking")
            await asyncio.sleep(0.01)
    raise AssertionError("no new session appeared")


# -- webhooks ------------------------------------------------------------------------


async def test_removing_a_webhook_waits_for_an_event_being_written_and_takes_it(pool, users):
    hooks = BatchWebhookStore(pool)
    owner, other = users(), users()
    for user in (owner, other):
        await hooks.set_webhook(
            user_id=user, url="https://hooks.example.com/h", secret_encrypted="s", enabled=True
        )
    async with pool.acquire() as sealing:
        transaction = sealing.transaction()
        await transaction.start()
        await _enqueue_event(
            sealing, owner, "batch-a", terminal_event("batch-a", "openai_inline", "completed")
        )
        known = {
            row["pid"]
            for row in await sealing.fetch(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database()"
            )
        }
        removal = asyncio.create_task(hooks.delete_webhook(owner))
        pid = await _session_pid(pool, removal, known)
        await _blocked(pool, pid, kind="Lock")
        assert not removal.done(), "the removal must wait for the event being written"
        await transaction.commit()
    assert await removal is True
    async with pool.acquire() as conn:
        left = await conn.fetchval(
            "SELECT count(*) FROM batch_webhook_events WHERE user_id = $1 AND status = 'pending'",
            owner,
        )
    assert left == 0, "an event outlived the webhook it was written under"
    # Another user's delivery is unaffected.
    async with pool.acquire() as conn:
        await _enqueue_event(
            conn, other, "batch-b", terminal_event("batch-b", "openai_inline", "completed")
        )
    (claimed,) = await hooks.claim_due_events(worker_id="w", limit=1, lease_seconds=60)
    assert claimed["user_id"] == other


async def test_an_event_is_not_written_once_the_webhook_is_gone(pool, users):
    hooks = BatchWebhookStore(pool)
    owner = users()
    await hooks.set_webhook(
        user_id=owner, url="https://hooks.example.com/h", secret_encrypted="s", enabled=True
    )
    assert await hooks.delete_webhook(owner)
    async with pool.acquire() as conn:
        await _enqueue_event(
            conn, owner, "batch-c", terminal_event("batch-c", "openai_inline", "completed")
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM batch_webhook_events WHERE user_id = $1", owner
            )
            == 0
        )


async def test_an_orphaned_event_neither_starves_others_nor_stays_pending(pool, users):
    hooks = BatchWebhookStore(pool)
    orphan_owner, healthy = users(), users()
    await hooks.set_webhook(
        user_id=healthy, url="https://hooks.example.com/h", secret_encrypted="s", enabled=True
    )
    async with pool.acquire() as conn:
        # As an older version could leave it: older than every other event.
        await conn.execute(
            "INSERT INTO batch_webhook_events (id, user_id, batch_id, event_type, payload, "
            "next_attempt_at) VALUES ($1, $2, 'batch-o', 'batch.completed', '{}', "
            "now() - interval '1 hour')",
            f"evt_orphan_{uuid.uuid4().hex[:8]}",
            orphan_owner,
        )
        await _enqueue_event(
            conn, healthy, "batch-h", terminal_event("batch-h", "openai_inline", "completed")
        )
    for attempt in range(3):
        claimed = await hooks.claim_due_events(worker_id=f"w{attempt}", limit=1, lease_seconds=60)
        assert [row["user_id"] for row in claimed] == ([healthy] if attempt == 0 else [])
    assert await hooks.fail_exhausted() >= 1
    async with pool.acquire() as conn:
        status, error = await conn.fetchrow(
            "SELECT status, last_error FROM batch_webhook_events WHERE user_id = $1",
            orphan_owner,
        )
    assert (status, error) == ("failed", "webhook removed")


async def test_erasure_takes_an_event_written_while_it_ran(pool, users):
    hooks, store = BatchWebhookStore(pool), BatchStore(pool)
    owner = users()
    await hooks.set_webhook(
        user_id=owner, url="https://hooks.example.com/h", secret_encrypted="s", enabled=True
    )
    async with pool.acquire() as sealing:
        transaction = sealing.transaction()
        await transaction.start()
        await _enqueue_event(
            sealing, owner, "batch-e", terminal_event("batch-e", "openai_inline", "completed")
        )
        known = {
            row["pid"]
            for row in await sealing.fetch(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database()"
            )
        }
        erasure = asyncio.create_task(store.delete_user_batches(owner))
        pid = await _session_pid(pool, erasure, known)
        await _blocked(pool, pid, kind="Lock")
        await transaction.commit()
    await erasure
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM batch_webhook_events WHERE user_id = $1", owner
            )
            == 0
        )
    assert await hooks.get_webhook(owner) is None


# -- file batch inputs ---------------------------------------------------------------


async def _upload(files: BatchFileStore, owner: str) -> str:
    data = b'{"custom_id":"a","method":"POST","url":"/v1/chat/completions","body":{}}\n'
    file_id = await files.create_upload(
        user_id=owner,
        filename="in.jsonl",
        purpose="batch",
        max_total_bytes=10**6,
        max_uploads_per_hour=100,
    )
    await files.append_chunk(
        file_id=file_id, user_id=owner, seq=0, data=data, max_total_bytes=10**6
    )
    await files.finish_upload(
        file_id=file_id,
        user_id=owner,
        sha256=hashlib.sha256(data).hexdigest(),
        line_count=1,
        retention_seconds=3600,
        check_owner=False,
    )
    return file_id


async def _create_from(store: BatchStore, owner: str, file_id: str) -> dict[str, Any]:
    return await store.create_batch(
        batch_id=f"batch_{uuid.uuid4().hex}",
        user_id=owner,
        key_hash=None,
        endpoint="/v1/chat/completions",
        model_ids=[],
        items=[],
        metadata=None,
        completion_window_seconds=3600,
        api_surface="openai_file",
        phase="validating",
        input_file_id=file_id,
    )


class _Barrier:
    """Pause every statement that fires *trigger* on *table* until released."""

    def __init__(self, pool: Any, *, table: str, timing: str, lock: int) -> None:
        self._pool = pool
        self._table = table
        self._timing = timing
        self._lock = lock
        self._name = f"race_barrier_{uuid.uuid4().hex[:8]}"
        self._gate: Any = None

    async def __aenter__(self) -> _Barrier:
        self._gate = await self._pool.acquire()
        await self._gate.execute(
            f"CREATE FUNCTION {self._name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"PERFORM pg_advisory_xact_lock({self._lock}); "
            f"IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$"
        )
        await self._gate.execute(
            f"CREATE TRIGGER {self._name} {self._timing} ON {self._table} "
            f"FOR EACH ROW EXECUTE FUNCTION {self._name}()"
        )
        await self._gate.execute("SELECT pg_advisory_lock($1)", self._lock)
        return self

    async def release(self) -> None:
        await self._gate.execute("SELECT pg_advisory_unlock($1)", self._lock)

    async def __aexit__(self, *exc: Any) -> None:
        await self._gate.execute("SELECT pg_advisory_unlock_all()")
        await self._gate.execute(f"DROP TRIGGER {self._name} ON {self._table}")
        await self._gate.execute(f"DROP FUNCTION {self._name}()")
        await self._pool.release(self._gate)


async def _waiting_advisory(pool: Any) -> int:
    async with pool.acquire() as conn:
        for _ in range(300):
            pid = await conn.fetchval(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event = 'advisory' LIMIT 1"
            )
            if pid:
                return pid
            await asyncio.sleep(0.01)
    raise AssertionError("nothing reached the barrier")


async def test_a_create_racing_a_collection_that_already_chose_the_input_is_refused(pool, users):
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = users()
    file_id = await _upload(files, owner)
    assert await files.get_file(file_id, user_id=owner) is not None  # the router's check
    assert await files.delete_file(file_id, user_id=owner)
    async with _Barrier(pool, table="batch_files", timing="BEFORE DELETE", lock=710262001) as gate:
        collection = asyncio.create_task(files.collect_garbage())
        await _waiting_advisory(pool)
        create = asyncio.create_task(_create_from(store, owner, file_id))
        await asyncio.sleep(0.2)
        await gate.release()
        assert (await collection)["collected"] == 1
        with pytest.raises(BatchInputUnavailable):
            await create
    assert not await files.upload_exists(file_id)
    assert await store.list_batches(user_id=owner, limit=5) == []


async def test_a_collection_with_a_stale_choice_leaves_an_accepted_batchs_input(pool, users):
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = users()
    file_id = await _upload(files, owner)
    async with _Barrier(pool, table="batch_jobs", timing="BEFORE INSERT", lock=710262002) as gate:
        # The create has pinned the input and stops just before storing the batch.
        create = asyncio.create_task(_create_from(store, owner, file_id))
        await _waiting_advisory(pool)
        # Meanwhile the owner deletes the file and a collection chooses it.
        assert await files.delete_file(file_id, user_id=owner)
        assert await files.collect_upload(file_id) is False, "the pinned row must be skipped"
        await gate.release()
        batch = await create
    assert batch["phase"] == "validating"
    # A collection whose choice predates the batch re-checks after locking.
    assert await files.collect_upload(file_id) is False
    assert (await files.collect_garbage())["collected"] == 0
    assert await files.upload_exists(file_id)
    assert len(await files.read_chunks(file_id)) == 1


async def test_an_input_stays_pinned_until_every_validating_batch_is_done(pool, users):
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = users()
    file_id = await _upload(files, owner)
    first = await _create_from(store, owner, file_id)
    second = await _create_from(store, owner, file_id)
    assert await files.delete_file(file_id, user_id=owner)
    for batch in (first, second):
        assert (await files.collect_garbage())["collected"] == 0
        assert await files.upload_exists(file_id)
        assert await store.fail_validation(batch_id=batch["id"], worker_id=None, errors={})
    assert (await files.collect_garbage())["collected"] == 1
    assert not await files.upload_exists(file_id)


async def test_a_deleted_or_expired_input_cannot_start_a_batch(pool, users):
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner, stranger = users(), users()
    deleted = await _upload(files, owner)
    await files.delete_file(deleted, user_id=owner)
    expired = await _upload(files, owner)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_files SET expires_at = now() - interval '1 second' WHERE id = $1",
            expired,
        )
    theirs = await _upload(files, stranger)
    for file_id in (deleted, expired):
        with pytest.raises(BatchInputUnavailable):
            await _create_from(store, owner, file_id)
    with pytest.raises(BatchInputUnavailable):
        await _create_from(store, owner, theirs)
    assert await store.list_batches(user_id=owner, limit=5) == []
