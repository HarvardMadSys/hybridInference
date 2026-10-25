"""The batch store against Postgres.

The API, worker and admin tests use stand-ins, so this is where the SQL runs. It
carries the promises the rest of batch inference relies on: a model's cap holds
across every claimer at once, models never wait on each other, batches take
turns, a dead worker's item comes back, a worker's write applies only under its
own claim, a cancel is never lost, and expiry, finalization, retention and both
deletes remove what they should and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from serving.storage import batch_store
from serving.storage.batch_store import (
    ACTIVE_BATCH_STATUSES,
    TERMINAL_BATCH_STATUSES,
    BatchDeleteInProgress,
    BatchOwnerUnavailable,
    BatchStore,
)
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ENDPOINT = "/v1/chat/completions"
BATCH_KEYS = {
    "id",
    "user_id",
    "key_hash",
    "endpoint",
    "status",
    "model_ids",
    "request_count",
    "completed_count",
    "failed_count",
    "metadata",
    "error",
    "cost_usd",
    "created_at",
    "in_progress_at",
    "completed_at",
    "failed_at",
    "expired_at",
    "cancelling_at",
    "cancelled_at",
    "expires_at",
    "paused_until",
}
RATE_LIMITED = {"code": "rate_limited", "message": "slow down", "status": 429}
_TEST_USERS = ("alice", "bob", "carol", "dave", "erin")


async def _wipe(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM batch_jobs")
        await conn.execute("DELETE FROM batch_model_limits")


async def _reset_test_users(pool: Any, *, create: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])", list(_TEST_USERS))
        if create:
            await conn.executemany(
                "INSERT INTO users (id, email, password_hash, status) "
                "VALUES ($1, $2, 'x', 'active')",
                [(user, f"batch-store-{user}@example.invalid") for user in _TEST_USERS],
            )


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=6)
    await BatchStore(pool).initialize()
    # Claims, sweeps and model stats read every batch in the database, so each
    # test starts from none (no other test file writes these tables).
    await _wipe(pool)
    await _reset_test_users(pool, create=True)
    try:
        yield pool
    finally:
        await _wipe(pool)
        await _reset_test_users(pool, create=False)
        await pool.close()


@pytest.fixture
def store(pool):
    return BatchStore(pool)


def _model() -> str:
    return f"model-{uuid.uuid4().hex[:8]}"


def _items(model: str, count: int, *, prefix: str = "req") -> list[dict[str, Any]]:
    return [
        {
            "custom_id": f"{prefix}-{i}",
            "model_id": model,
            "request": {"model": model, "messages": [{"role": "user", "content": f"q{i} ✓"}]},
        }
        for i in range(count)
    ]


async def _create(
    store: BatchStore,
    items: list[dict[str, Any]],
    *,
    user: str = "alice",
    window: int = 3600,
    metadata: dict[str, Any] | None = None,
    key_hash: str | None = "key-hash",
    model_ids: list[str] | None = None,
) -> dict[str, Any]:
    return await store.create_batch(
        batch_id=f"batch_{uuid.uuid4().hex}",
        user_id=user,
        key_hash=key_hash,
        endpoint=ENDPOINT,
        model_ids=sorted({i["model_id"] for i in items}) if model_ids is None else model_ids,
        items=items,
        metadata=metadata,
        completion_window_seconds=window,
    )


async def _claim(
    store: BatchStore,
    model: str,
    *,
    worker: str = "worker-1",
    limit: int = 10,
    max_running: int = 100,
    lease: int = 300,
) -> list[dict[str, Any]]:
    return await store.claim_items(
        model_id=model,
        worker_id=worker,
        limit=limit,
        max_running=max_running,
        lease_seconds=lease,
    )


async def _complete(
    store: BatchStore,
    item: dict[str, Any],
    *,
    worker: str = "worker-1",
    attempt: int | None = None,
    error: dict[str, Any] | None = None,
    cost: float | None = None,
) -> bool:
    return await store.complete_item(
        item_id=item["id"],
        worker_id=worker,
        attempt=item["attempts"] if attempt is None else attempt,
        response=None if error else {"id": f"chatcmpl-{item['id']}", "choices": []},
        error=error,
        status_code=error.get("status", 500) if error else 200,
        prompt_tokens=None if error else 10,
        completion_tokens=None if error else 5,
        cost_usd=cost,
        request_id=f"req-{item['id']}",
    )


async def _retry(store: BatchStore, item: dict[str, Any], *, when: datetime | None = None) -> bool:
    return await store.retry_item(
        item_id=item["id"],
        worker_id="worker-1",
        attempt=item["attempts"],
        next_attempt_at=when or datetime.now(UTC) - timedelta(seconds=1),
        error=RATE_LIMITED,
    )


async def _execute(pool: Any, sql: str, *args: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(sql, *args)


async def _rows(pool: Any, batch_id: str) -> list[dict[str, Any]]:
    """The batch's items as stored, in id order, JSON columns still encoded."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, custom_id, status, attempts, claimed_by, lease_expires_at, "
            "next_attempt_at, error FROM batch_items WHERE batch_id = $1 ORDER BY id",
            batch_id,
        )
    return [dict(row) for row in rows]


async def _statuses(pool: Any, batch_id: str) -> list[str]:
    return [row["status"] for row in await _rows(pool, batch_id)]


async def _expire_lease(pool: Any, item_id: int) -> None:
    await _execute(
        pool,
        "UPDATE batch_items SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
        item_id,
    )


class _PauseAfterStatementConnection:
    """Pause after one matching SQL statement while its transaction stays open."""

    def __init__(
        self,
        conn: Any,
        needle: str,
        reached: asyncio.Event,
        resume: asyncio.Event,
        started: asyncio.Event | None = None,
    ) -> None:
        self._conn = conn
        self._needle = needle
        self._reached = reached
        self._resume = resume
        self._started = started
        self._paused = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def _signal_before_match(self, query: str) -> None:
        if not self._paused and self._needle in query and self._started is not None:
            self._started.set()

    async def _pause_after_match(self, query: str) -> None:
        if not self._paused and self._needle in query:
            self._paused = True
            self._reached.set()
            await self._resume.wait()

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._signal_before_match(query)
        result = await self._conn.fetchval(query, *args)
        await self._pause_after_match(query)
        return result

    async def execute(self, query: str, *args: Any) -> Any:
        self._signal_before_match(query)
        result = await self._conn.execute(query, *args)
        await self._pause_after_match(query)
        return result


class _PauseAfterStatementAcquire:
    def __init__(
        self,
        pool: Any,
        needle: str,
        reached: asyncio.Event,
        resume: asyncio.Event,
        started: asyncio.Event | None = None,
    ) -> None:
        self._acquire = pool.acquire()
        self._needle = needle
        self._reached = reached
        self._resume = resume
        self._started = started

    async def __aenter__(self) -> _PauseAfterStatementConnection:
        conn = await self._acquire.__aenter__()
        return _PauseAfterStatementConnection(
            conn,
            self._needle,
            self._reached,
            self._resume,
            self._started,
        )

    async def __aexit__(self, *args: Any) -> Any:
        return await self._acquire.__aexit__(*args)


class _PauseAfterStatementPool:
    def __init__(
        self,
        pool: Any,
        needle: str,
        reached: asyncio.Event,
        resume: asyncio.Event,
        started: asyncio.Event | None = None,
    ) -> None:
        self._pool = pool
        self._needle = needle
        self._reached = reached
        self._resume = resume
        self._started = started

    def acquire(self) -> _PauseAfterStatementAcquire:
        return _PauseAfterStatementAcquire(
            self._pool,
            self._needle,
            self._reached,
            self._resume,
            self._started,
        )


# -- API surface ---------------------------------------------------------------


async def test_a_created_batch_reads_back_as_created(store):
    model = _model()
    created = await _create(store, _items(model, 3), metadata={"project": "eval ✓"}, window=7200)

    assert set(created) == BATCH_KEYS
    assert created["status"] == "validating"
    assert created["user_id"] == "alice"
    assert created["key_hash"] == "key-hash"
    assert created["endpoint"] == ENDPOINT
    assert created["model_ids"] == [model]
    assert created["request_count"] == 3
    assert (created["completed_count"], created["failed_count"]) == (0, 0)
    assert created["metadata"] == {"project": "eval ✓"}
    assert created["error"] is None
    assert created["cost_usd"] == 0.0
    assert isinstance(created["cost_usd"], float)
    assert created["created_at"].tzinfo is not None
    assert created["expires_at"] - created["created_at"] == timedelta(hours=2)
    for key in ("in_progress_at", "completed_at", "failed_at", "expired_at", "paused_until"):
        assert created[key] is None
    assert await store.get_batch(created["id"]) == created


async def test_reads_are_scoped_to_the_owner_when_asked(store):
    created = await _create(store, _items(_model(), 1))

    assert await store.get_batch(created["id"], user_id="bob") is None
    assert (await store.get_batch(created["id"], user_id="alice"))["id"] == created["id"]
    assert await store.get_batch("batch_unknown") is None


async def test_a_users_batches_list_newest_first_in_pages(store):
    model = _model()
    mine = [(await _create(store, _items(model, 1)))["id"] for _ in range(5)]
    theirs = (await _create(store, _items(model, 1), user="bob"))["id"]
    newest_first = list(reversed(mine))

    first = await store.list_batches(user_id="alice", limit=2)
    second = await store.list_batches(user_id="alice", limit=2, after=first[-1]["id"])
    third = await store.list_batches(user_id="alice", limit=2, after=second[-1]["id"])

    assert [b["id"] for b in first + second + third] == newest_first
    assert await store.list_batches(user_id="alice", limit=2, after=third[-1]["id"]) == []
    # A cursor that names no batch of this user ends the listing.
    assert await store.list_batches(user_id="alice", limit=2, after="batch_unknown") == []
    assert await store.list_batches(user_id="alice", limit=2, after=theirs) == []
    assert [b["id"] for b in await store.list_batches(user_id="bob", limit=10)] == [theirs]


async def test_results_page_through_items_in_submission_order(store):
    created = await _create(store, _items(_model(), 5))

    first = await store.list_results(created["id"], limit=2)
    rest = await store.list_results(created["id"], after=first[-1]["id"], limit=10)

    assert [r["custom_id"] for r in first + rest] == [f"req-{i}" for i in range(5)]
    ids = [r["id"] for r in first + rest]
    assert ids == sorted(ids)
    assert first[0] == {
        "id": ids[0],
        "custom_id": "req-0",
        "status": "queued",
        "status_code": None,
        "response": None,
        "error": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
        "request_id": None,
        "completed_at": None,
    }
    assert await store.list_results(created["id"], after=ids[-1]) == []
    assert await store.list_results("batch_unknown") == []


async def test_unstorable_custom_ids_are_refused_and_nothing_is_stored(store):
    model = _model()
    duplicated = [*_items(model, 2), _items(model, 1)[0]]  # req-0 twice
    with_nul = [{"custom_id": "a\x00b", "model_id": model, "request": {}}]

    with pytest.raises(ValueError, match="'req-0' appears more than once"):
        await _create(store, duplicated)
    with pytest.raises(ValueError, match="NUL"):
        await _create(store, with_nul)

    assert await store.list_batches(user_id="alice", limit=10) == []


async def test_authenticated_create_requires_a_locked_active_owner(pool, store):
    model = _model()

    await _execute(pool, "UPDATE users SET status = 'suspended' WHERE id = 'alice'")
    with pytest.raises(BatchOwnerUnavailable):
        await _create(store, _items(model, 1))

    await _execute(
        pool,
        "UPDATE users SET status = 'active', hard_delete_pending = TRUE WHERE id = 'alice'",
    )
    with pytest.raises(BatchOwnerUnavailable):
        await _create(store, _items(model, 1))

    await _execute(pool, "DELETE FROM users WHERE id = 'alice'")
    with pytest.raises(BatchOwnerUnavailable):
        await _create(store, _items(model, 1))

    assert await store.list_batches(user_id="alice", limit=10) == []


async def test_auth_disabled_create_does_not_require_an_account(store):
    created = await _create(
        store,
        _items(_model(), 1),
        user="anonymous-without-account",
        key_hash=None,
    )

    assert created["user_id"] == "anonymous-without-account"
    assert created["key_hash"] is None


async def test_create_waits_for_account_update_and_rechecks_status(pool, store):
    model = _model()
    async with pool.acquire() as blocker, blocker.transaction():
        await blocker.execute("SELECT 1 FROM users WHERE id = 'alice' FOR UPDATE")
        creating = asyncio.create_task(_create(store, _items(model, 1)))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(creating), timeout=0.05)
        await blocker.execute("UPDATE users SET status = 'deleted' WHERE id = 'alice'")

    with pytest.raises(BatchOwnerUnavailable):
        await creating
    assert await store.list_batches(user_id="alice", limit=10) == []


async def test_every_model_an_item_names_is_listed_on_the_batch(store):
    first, second = _model(), _model()
    items = [*_items(first, 1, prefix="a"), *_items(second, 1, prefix="b")]

    created = await _create(store, items, model_ids=[first])

    assert created["model_ids"] == [first, second]
    assert len(await _claim(store, second)) == 1


async def test_a_batch_at_the_request_cap_is_stored_whole_in_order(pool, store):
    model = _model()
    items = [{"custom_id": f"r{i}", "model_id": model, "request": {"i": i}} for i in range(50_000)]

    created = await _create(store, items)

    assert created["request_count"] == 50_000
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT custom_id, (request->>'i')::int AS i FROM batch_items "
            "WHERE batch_id = $1 ORDER BY id",
            created["id"],
        )
    assert [row["i"] for row in rows] == list(range(50_000))
    assert rows[-1]["custom_id"] == "r49999"


async def test_nul_characters_round_trip_through_every_payload_column(pool, store):
    model = _model()
    request = {"content": "a\x00b", "\x00key": {"nested": ["x\x00y"]}}
    metadata = {"note": "x\x00y"}
    items = [
        {"custom_id": "nul-ok", "model_id": model, "request": request},
        {"custom_id": "nul-error", "model_id": model, "request": {"content": "plain"}},
    ]
    created = await _create(store, items, metadata=metadata)
    ok, failed = await _claim(store, model)

    assert ok["request"] == request
    assert failed["request"] == {"content": "plain"}
    assert created["metadata"] == metadata

    response = {"choices": [{"message": {"content": "r\x00s"}}]}
    error = {"code": "upstream_error", "message": "e\x00f"}
    assert await store.complete_item(
        item_id=ok["id"],
        worker_id="worker-1",
        attempt=ok["attempts"],
        response=response,
        error=None,
        status_code=200,
    )
    assert await _complete(store, failed, error=error)

    results = {item["custom_id"]: item for item in await store.list_results(created["id"])}
    assert results["nul-ok"]["response"] == response
    assert results["nul-ok"]["error"] is None
    assert results["nul-error"]["response"] is None
    assert results["nul-error"]["error"] == error

    async with pool.acquire() as conn:
        shapes = await conn.fetchrow(
            """
            SELECT jsonb_typeof(j.metadata) AS metadata,
                   jsonb_typeof(ok.request) AS request,
                   jsonb_typeof(plain.request) AS plain_request,
                   jsonb_typeof(ok.response) AS response,
                   jsonb_typeof(plain.error) AS error
            FROM batch_jobs AS j
            JOIN batch_items AS ok
              ON ok.batch_id = j.id AND ok.custom_id = 'nul-ok'
            JOIN batch_items AS plain
              ON plain.batch_id = j.id AND plain.custom_id = 'nul-error'
            WHERE j.id = $1
            """,
            created["id"],
        )
    assert dict(shapes) == {
        "metadata": "array",
        "request": "array",
        "plain_request": "object",
        "response": "array",
        "error": "array",
    }


async def test_json_envelope_preserves_reserved_shape_and_legacy_string_collisions():
    marker = batch_store._JSON_ENVELOPE_MARKER
    collision = [marker, "eyJsb29rcyI6ICJsaWtlIGFuIGVudmVsb3BlIn0="]
    malformed = [marker, "not-base64!"]
    object_collision = {marker: collision}
    legacy_string = f"{marker}:eyJsb29rcyI6ICJsZWdhY3kifQ=="

    assert batch_store._load_json(batch_store._dump_json(collision)) == collision
    assert batch_store._load_json(batch_store._dump_json(malformed)) == malformed
    assert batch_store._load_json(batch_store._dump_json(object_collision)) == object_collision
    assert batch_store._load_json(json.dumps(legacy_string)) == legacy_string
    assert batch_store._load_json(legacy_string) == legacy_string

    value = {"content": "a\x00b"}
    encoded = batch_store._dump_json(value)
    assert batch_store._load_json(encoded) == value
    assert batch_store._load_json(json.loads(encoded)) == value


async def test_model_ids_that_match_payload_envelope_stay_plain_json(store):
    marker = batch_store._JSON_ENVELOPE_MARKER
    collision = [marker, "eyJsb29rcyI6ICJsaWtlIGFuIGVudmVsb3BlIn0="]
    created = await _create(store, _items(marker, 1), model_ids=collision)

    assert created["model_ids"] == collision
    assert (await store.get_batch(created["id"]))["model_ids"] == collision
    assert await store.delete_batch(created["id"]) is True
    assert await store.get_batch(created["id"]) is None


async def test_active_batches_are_counted_per_user(store):
    model = _model()
    started = await _create(store, _items(model, 1))
    await _claim(store, model, limit=1)
    fresh = await _create(store, _items(model, 1))
    cancelled = await _create(store, _items(model, 1))
    await store.cancel_batch(cancelled["id"])
    await _create(store, _items(_model(), 1), user="bob")

    statuses = [(await store.get_batch(b["id"]))["status"] for b in (started, fresh, cancelled)]
    assert statuses == ["in_progress", "validating", "cancelled"]
    assert set(statuses[:2]) <= ACTIVE_BATCH_STATUSES
    assert statuses[2] in TERMINAL_BATCH_STATUSES
    assert await store.count_active_batches("alice") == 2
    assert await store.count_active_batches("bob") == 1
    assert await store.count_active_batches("nobody") == 0


# -- claiming ------------------------------------------------------------------


async def test_a_claim_hands_the_worker_what_it_needs(pool, store):
    model = _model()
    created = await _create(store, _items(model, 2), key_hash="hash-of-key")

    claimed = await _claim(store, model, worker="worker-a", limit=1, lease=120)

    assert len(claimed) == 1
    item = claimed[0]
    assert set(item) == {
        "id",
        "batch_id",
        "custom_id",
        "model_id",
        "request",
        "attempts",
        "error",
        "user_id",
        "key_hash",
    }
    assert item["batch_id"] == created["id"]
    assert item["custom_id"] == "req-0"
    assert item["model_id"] == model
    assert item["request"] == {"model": model, "messages": [{"role": "user", "content": "q0 ✓"}]}
    assert item["attempts"] == 1
    assert item["error"] is None
    assert (item["user_id"], item["key_hash"]) == ("alice", "hash-of-key")
    stored = (await _rows(pool, created["id"]))[0]
    assert (stored["status"], stored["claimed_by"]) == ("running", "worker-a")
    lease_left = stored["lease_expires_at"] - datetime.now(UTC)
    assert timedelta(seconds=100) < lease_left <= timedelta(seconds=120)
    batch = await store.get_batch(created["id"])
    assert batch["status"] == "in_progress"
    assert batch["in_progress_at"] is not None


async def test_a_claim_never_exceeds_the_models_cap_counting_live_leases(store):
    model = _model()
    await _create(store, _items(model, 10))

    first = await _claim(store, model, max_running=3)
    assert len(first) == 3
    # A second claim, by any worker, sees the three live leases.
    assert await _claim(store, model, worker="worker-2", max_running=3) == []
    assert len(await _claim(store, model, limit=1, max_running=5)) == 1
    assert len(await _claim(store, model, max_running=5)) == 1
    assert await _claim(store, model, max_running=5) == []

    assert await _complete(store, first[0])
    assert len(await _claim(store, model, max_running=5)) == 1
    # The cap lowered below what runs: nothing more until enough finish.
    assert await _claim(store, model, max_running=2) == []


async def test_an_idle_model_is_claimable_while_a_busy_one_is_at_its_cap(store):
    busy, idle = _model(), _model()
    await _create(store, [*_items(busy, 5, prefix="busy"), *_items(idle, 5, prefix="idle")])

    assert len(await _claim(store, busy, max_running=2)) == 2
    assert await _claim(store, busy, max_running=2) == []

    claimed = await _claim(store, idle, max_running=2)
    assert [item["model_id"] for item in claimed] == [idle, idle]


async def test_batches_take_turns(store):
    model = _model()
    older = await _create(store, _items(model, 3, prefix="a"))
    newer = await _create(store, _items(model, 3, prefix="b"))

    def picked(items):
        return [(item["batch_id"], item["custom_id"]) for item in items]

    assert picked(await _claim(store, model, limit=2)) == [
        (older["id"], "a-0"),
        (older["id"], "a-1"),
    ]
    assert picked(await _claim(store, model, limit=2)) == [
        (newer["id"], "b-0"),
        (newer["id"], "b-1"),
    ]
    # The batch claimed from longest ago goes first and, once out of items,
    # the next one fills the room.
    assert picked(await _claim(store, model, limit=3)) == [
        (older["id"], "a-2"),
        (newer["id"], "b-2"),
    ]
    assert await _claim(store, model) == []


async def test_an_expired_lease_is_reclaimed_and_the_attempt_counts(pool, store):
    model = _model()
    created = await _create(store, _items(model, 2))
    (lost,) = await _claim(store, model, worker="worker-1", limit=1)

    await _expire_lease(pool, lost["id"])
    (reclaimed,) = await _claim(store, model, worker="worker-2", limit=1, max_running=1)

    assert reclaimed["id"] == lost["id"]
    assert reclaimed["attempts"] == 2
    assert (await _rows(pool, created["id"]))[0]["claimed_by"] == "worker-2"
    # The reclaimed item is live again and fills the cap.
    assert await _claim(store, model, worker="worker-2", max_running=1) == []
    # The dead worker's late writes change nothing.
    assert await _complete(store, lost, worker="worker-1") is False
    assert await store.heartbeat(worker_id="worker-1", item_ids=[lost["id"]], lease_seconds=60) == 0
    assert await _complete(store, reclaimed, worker="worker-2") is True


async def test_paused_expired_and_cancelled_batches_give_nothing(pool, store):
    model = _model()
    paused = await _create(store, _items(model, 1), user="carol")
    overdue = await _create(store, _items(model, 1), user="dave")
    cancelled = await _create(store, _items(model, 1), user="erin")
    await store.pause_user_batches("carol", datetime.now(UTC) + timedelta(hours=1))
    await _execute(
        pool,
        "UPDATE batch_jobs SET expires_at = now() - interval '1 second' WHERE id = $1",
        overdue["id"],
    )
    await store.cancel_batch(cancelled["id"])

    assert await _claim(store, model) == []
    assert await store.active_models() == []

    await store.pause_user_batches("carol", datetime.now(UTC) - timedelta(seconds=1))
    assert await store.active_models() == [model]
    assert [item["batch_id"] for item in await _claim(store, model)] == [paused["id"]]


async def test_active_models_lists_each_claimable_model_once(store):
    first, second, third = sorted([_model(), _model(), _model()])
    await _create(store, [*_items(first, 1, prefix="a"), *_items(second, 1, prefix="b")])
    await _create(store, _items(second, 1))
    await _create(store, _items(third, 1), user="carol")
    await store.pause_user_batches("carol", datetime.now(UTC) + timedelta(hours=1))

    assert await store.active_models() == [first, second]


async def test_a_retried_item_waits_for_its_next_attempt(pool, store):
    model = _model()
    created = await _create(store, _items(model, 1))
    (item,) = await _claim(store, model)

    assert await _retry(store, item, when=datetime.now(UTC) + timedelta(hours=1))
    stored = (await _rows(pool, created["id"]))[0]
    assert (stored["status"], stored["attempts"], stored["claimed_by"]) == ("queued", 1, None)
    assert await _claim(store, model) == []

    await _execute(
        pool,
        "UPDATE batch_items SET next_attempt_at = now() - interval '1 second' WHERE id = $1",
        item["id"],
    )
    (again,) = await _claim(store, model)
    assert again["id"] == item["id"]
    assert again["attempts"] == 2
    assert again["error"] == RATE_LIMITED


async def test_concurrent_claims_never_exceed_the_cap(store):
    model = _model()
    await _create(store, _items(model, 40))

    for _ in range(4):
        claims = await asyncio.gather(
            *(_claim(store, model, worker=f"worker-{n}", limit=4, max_running=6) for n in range(5))
        )
        claimed = [item for claim in claims for item in claim]
        assert len(claimed) == 6
        assert len({item["id"] for item in claimed}) == 6
        for n, claim in enumerate(claims):
            for item in claim:
                assert await _complete(store, item, worker=f"worker-{n}")


async def test_a_two_connection_race_never_exceeds_the_cap(store):
    model = _model()
    await _create(store, _items(model, 10))

    first, second = await asyncio.gather(
        _claim(store, model, worker="worker-1", limit=3, max_running=4),
        _claim(store, model, worker="worker-2", limit=3, max_running=4),
    )

    assert sorted([len(first), len(second)]) == [1, 3]
    assert not {item["id"] for item in first} & {item["id"] for item in second}


async def test_expired_heartbeat_cannot_race_a_claim_past_the_cap(pool, store):
    model = _model()
    created = await _create(store, _items(model, 2))
    (expired,) = await _claim(store, model, worker="worker-1", limit=1, max_running=1)
    await _expire_lease(pool, expired["id"])

    counted = asyncio.Event()
    resume_claim = asyncio.Event()
    racing_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SELECT count(*) FROM batch_items",
            counted,
            resume_claim,
        )
    )
    claiming = asyncio.create_task(
        _claim(racing_store, model, worker="worker-2", limit=1, max_running=1)
    )
    await asyncio.wait_for(counted.wait(), timeout=5)

    heartbeat_started = asyncio.Event()
    heartbeat_acquired = asyncio.Event()
    no_pause = asyncio.Event()
    no_pause.set()
    heartbeat_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SELECT pg_advisory_xact_lock",
            heartbeat_acquired,
            no_pause,
            heartbeat_started,
        )
    )
    renewing = asyncio.create_task(
        heartbeat_store.heartbeat(
            worker_id="worker-1",
            item_ids=[expired["id"]],
            lease_seconds=3600,
        )
    )
    try:
        await asyncio.wait_for(heartbeat_started.wait(), timeout=5)
        assert not renewing.done()
    finally:
        resume_claim.set()

    (reclaimed,) = await asyncio.wait_for(claiming, timeout=5)
    assert await asyncio.wait_for(renewing, timeout=5) == 0
    assert reclaimed["id"] == expired["id"]
    assert reclaimed["attempts"] == 2
    rows = await _rows(pool, created["id"])
    assert [(row["status"], row["claimed_by"]) for row in rows] == [
        ("running", "worker-2"),
        ("queued", None),
    ]


async def test_uncommitted_live_heartbeat_blocks_a_claim_until_renewed(pool, store):
    model = _model()
    created = await _create(store, _items(model, 2))
    queued, running = await _claim(store, model, worker="worker-1", limit=2, max_running=2)
    assert await store.release_item(
        item_id=queued["id"], worker_id="worker-1", attempt=queued["attempts"]
    )
    await _execute(
        pool,
        "UPDATE batch_items "
        "SET lease_expires_at = clock_timestamp() + interval '2 seconds' WHERE id = $1",
        running["id"],
    )

    updated = asyncio.Event()
    resume_heartbeat = asyncio.Event()
    heartbeat_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SET lease_expires_at = clock_timestamp()",
            updated,
            resume_heartbeat,
        )
    )
    renewing = asyncio.create_task(
        heartbeat_store.heartbeat(
            worker_id="worker-1", item_ids=[running["id"]], lease_seconds=3600
        )
    )
    await asyncio.wait_for(updated.wait(), timeout=5)
    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT GREATEST(EXTRACT(EPOCH FROM "
            "(lease_expires_at - clock_timestamp())), 0) "
            "FROM batch_items WHERE id = $1",
            running["id"],
        )
    await asyncio.sleep(float(remaining) + 0.05)

    claim_started = asyncio.Event()
    claim_acquired = asyncio.Event()
    no_pause = asyncio.Event()
    no_pause.set()
    claim_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SELECT pg_advisory_xact_lock",
            claim_acquired,
            no_pause,
            claim_started,
        )
    )
    claiming = asyncio.create_task(
        _claim(claim_store, model, worker="worker-2", limit=1, max_running=1)
    )
    try:
        await asyncio.wait_for(claim_started.wait(), timeout=5)
        assert not claiming.done()
    finally:
        resume_heartbeat.set()

    assert await asyncio.wait_for(renewing, timeout=5) == 1
    assert await asyncio.wait_for(claiming, timeout=5) == []
    rows = await _rows(pool, created["id"])
    assert [(row["status"], row["claimed_by"]) for row in rows] == [
        ("queued", None),
        ("running", "worker-1"),
    ]


async def test_a_zero_cap_or_limit_claims_nothing(store):
    model = _model()
    await _create(store, _items(model, 2))

    assert await _claim(store, model, max_running=0) == []
    assert await _claim(store, model, limit=0) == []
    assert await _claim(store, "model-without-batches") == []


# -- worker writes -------------------------------------------------------------


async def test_worker_writes_apply_only_under_the_current_claim(pool, store):
    model = _model()
    created = await _create(store, _items(model, 1))
    (item,) = await _claim(store, model, worker="worker-1")

    assert await _complete(store, item, worker="worker-2") is False
    assert await _complete(store, item, attempt=2) is False
    assert (
        await store.retry_item(
            item_id=item["id"],
            worker_id="worker-2",
            attempt=1,
            next_attempt_at=datetime.now(UTC),
            error=RATE_LIMITED,
        )
        is False
    )
    assert await store.release_item(item_id=item["id"], worker_id="worker-1", attempt=0) is False
    assert await _complete(store, {"id": 10**12, "attempts": 1}) is False
    stored = (await _rows(pool, created["id"]))[0]
    assert (stored["status"], stored["attempts"], stored["claimed_by"]) == (
        "running",
        1,
        "worker-1",
    )

    assert await _complete(store, item) is True
    assert await _complete(store, item) is False
    assert await _retry(store, item) is False
    assert await store.release_item(item_id=item["id"], worker_id="worker-1", attempt=1) is False
    batch = await store.get_batch(created["id"])
    assert (batch["completed_count"], batch["failed_count"]) == (1, 0)


async def test_completions_count_cost_and_complete_the_batch(store):
    model = _model()
    created = await _create(store, _items(model, 3))
    first, second, third = await _claim(store, model)
    invalid = {"code": "invalid_request", "message": "bad request", "status": 400}

    assert await _complete(store, first, cost=0.25)
    assert await _complete(store, second, error=invalid)
    midway = await store.get_batch(created["id"])
    assert midway["status"] == "in_progress"
    assert (midway["completed_count"], midway["failed_count"]) == (1, 1)
    assert midway["cost_usd"] == pytest.approx(0.25)

    assert await _complete(store, third, cost=0.5)
    done = await store.get_batch(created["id"])
    assert done["status"] == "completed"
    assert done["completed_at"] is not None
    assert (done["completed_count"], done["failed_count"]) == (2, 1)
    assert done["cost_usd"] == pytest.approx(0.75)

    results = {r["custom_id"]: r for r in await store.list_results(created["id"])}
    ok = results["req-0"]
    assert ok["status"] == "completed"
    assert ok["status_code"] == 200
    assert ok["response"] == {"id": f"chatcmpl-{first['id']}", "choices": []}
    assert ok["error"] is None
    assert (ok["prompt_tokens"], ok["completion_tokens"]) == (10, 5)
    assert ok["cost_usd"] == pytest.approx(0.25)
    assert ok["request_id"] == f"req-{first['id']}"
    assert ok["completed_at"] is not None
    failed = results["req-1"]
    assert failed["status"] == "failed"
    assert failed["status_code"] == 400
    assert failed["response"] is None
    assert failed["error"] == invalid
    assert failed["cost_usd"] is None


async def test_concurrent_completions_lose_no_count_and_finalize_the_batch(store):
    model = _model()
    created = await _create(store, _items(model, 6))
    claimed = await _claim(store, model)

    assert all(await asyncio.gather(*(_complete(store, item, cost=0.1) for item in claimed)))

    batch = await store.get_batch(created["id"])
    assert batch["status"] == "completed"
    assert (batch["completed_count"], batch["failed_count"]) == (6, 0)
    assert batch["cost_usd"] == pytest.approx(0.6)


async def test_a_non_finite_cost_is_not_added_to_the_batch(store):
    model = _model()
    created = await _create(store, _items(model, 2))
    first, second = await _claim(store, model)

    assert await _complete(store, first, cost=float("nan"))
    assert await _complete(store, second, cost=0.5)

    batch = await store.get_batch(created["id"])
    assert batch["cost_usd"] == pytest.approx(0.5)
    assert (await store.list_results(created["id"]))[0]["cost_usd"] is None


async def test_a_retry_keeps_the_attempt_and_a_release_hands_it_back(pool, store):
    model = _model()
    created = await _create(store, _items(model, 1))
    (item,) = await _claim(store, model)

    assert await _retry(store, item)
    stored = (await _rows(pool, created["id"]))[0]
    assert (stored["status"], stored["attempts"]) == ("queued", 1)
    (second,) = await _claim(store, model)
    assert (second["attempts"], second["error"]) == (2, RATE_LIMITED)

    assert await store.release_item(item_id=item["id"], worker_id="worker-1", attempt=2)
    stored = (await _rows(pool, created["id"]))[0]
    assert (stored["status"], stored["attempts"], stored["claimed_by"]) == ("queued", 1, None)
    (third,) = await _claim(store, model)
    assert (third["attempts"], third["error"]) == (2, RATE_LIMITED)

    later = datetime.now(UTC) + timedelta(minutes=5)
    assert await store.release_item(
        item_id=item["id"], worker_id="worker-1", attempt=2, next_attempt_at=later
    )
    assert await _claim(store, model) == []
    assert (await store.get_batch(created["id"]))["status"] == "in_progress"


async def test_heartbeat_extends_only_the_workers_running_leases(pool, store):
    model = _model()
    created = await _create(store, _items(model, 3))
    mine = await _claim(store, model, worker="worker-1", limit=2, lease=60)
    (theirs,) = await _claim(store, model, worker="worker-2", limit=1, lease=60)

    touched = await store.heartbeat(
        worker_id="worker-1",
        item_ids=[item["id"] for item in [*mine, theirs]],
        lease_seconds=3600,
    )

    assert touched == 2
    leases = {row["id"]: row["lease_expires_at"] for row in await _rows(pool, created["id"])}
    soon = datetime.now(UTC) + timedelta(seconds=120)
    assert all(leases[item["id"]] > soon for item in mine)
    assert leases[theirs["id"]] < soon

    assert await _complete(store, mine[0])
    ids = [item["id"] for item in mine]
    assert await store.heartbeat(worker_id="worker-1", item_ids=ids, lease_seconds=60) == 1
    assert await store.heartbeat(worker_id="worker-1", item_ids=[], lease_seconds=60) == 0


async def test_multi_model_heartbeats_take_locks_in_one_order(store):
    first_model, second_model = sorted((_model(), _model()))
    await _create(
        store,
        [
            *_items(first_model, 1, prefix="first"),
            *_items(second_model, 1, prefix="second"),
        ],
    )
    (first_item,) = await _claim(store, first_model, worker="worker-1")
    (second_item,) = await _claim(store, second_model, worker="worker-1")

    touched = await asyncio.wait_for(
        asyncio.gather(
            store.heartbeat(
                worker_id="worker-1",
                item_ids=[first_item["id"], second_item["id"]],
                lease_seconds=3600,
            ),
            store.heartbeat(
                worker_id="worker-1",
                item_ids=[second_item["id"], first_item["id"]],
                lease_seconds=3600,
            ),
        ),
        timeout=2,
    )
    assert touched == [2, 2]


# -- cancel, fail, pause -------------------------------------------------------


async def test_cancelling_with_nothing_running_is_immediate(pool, store):
    model = _model()
    created = await _create(store, _items(model, 3))

    cancelled = await store.cancel_batch(created["id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelling_at"] is not None
    assert cancelled["cancelled_at"] is not None
    assert (cancelled["completed_count"], cancelled["failed_count"]) == (0, 0)
    assert await _statuses(pool, created["id"]) == ["cancelled"] * 3
    assert await _claim(store, model) == []
    assert await store.get_batch(created["id"]) == cancelled


async def test_cancel_while_an_item_runs_waits_for_it(pool, store):
    model = _model()
    created = await _create(store, _items(model, 3))
    (running,) = await _claim(store, model, limit=1)

    cancelling = await store.cancel_batch(created["id"], user_id="alice")

    assert cancelling["status"] == "cancelling"
    assert cancelling["cancelling_at"] is not None
    assert cancelling["cancelled_at"] is None
    assert await _statuses(pool, created["id"]) == ["running", "cancelled", "cancelled"]
    assert await _claim(store, model) == []
    assert await store.count_active_batches("alice") == 1

    assert await _complete(store, running)
    cancelled = await store.get_batch(created["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] is not None
    assert cancelled["completed_count"] == 1


async def test_a_retry_during_cancelling_does_not_resurrect_the_item(pool, store):
    model = _model()
    created = await _create(store, _items(model, 2))
    first, second = await _claim(store, model)
    assert (await store.cancel_batch(created["id"]))["status"] == "cancelling"

    assert await _retry(store, first)
    assert await _statuses(pool, created["id"]) == ["cancelled", "running"]
    assert (await store.get_batch(created["id"]))["status"] == "cancelling"
    assert await _claim(store, model) == []

    assert await store.release_item(item_id=second["id"], worker_id="worker-1", attempt=1)
    assert await _statuses(pool, created["id"]) == ["cancelled", "cancelled"]
    assert (await store.get_batch(created["id"]))["status"] == "cancelled"
    # Late writes of either attempt change nothing.
    assert await _retry(store, first) is False
    assert await _complete(store, second) is False
    assert (await store.get_batch(created["id"]))["status"] == "cancelled"


async def test_a_cancel_racing_a_claim_is_never_lost(pool, store):
    model = _model()
    for _ in range(5):
        created = await _create(store, _items(model, 4))

        claimed, _ = await asyncio.gather(
            _claim(store, model, limit=2), store.cancel_batch(created["id"])
        )

        # Whichever ran first, nothing is left queued, and the claim never
        # turns the batch back to in_progress.
        statuses = await _statuses(pool, created["id"])
        assert "queued" not in statuses
        assert statuses.count("running") == len(claimed)
        expected = "cancelling" if claimed else "cancelled"
        assert (await store.get_batch(created["id"]))["status"] == expected
        for item in claimed:
            assert await _complete(store, item)
        assert (await store.get_batch(created["id"]))["status"] == "cancelled"


async def test_cancel_leaves_other_owners_and_finished_batches_alone(store):
    model = _model()
    created = await _create(store, _items(model, 1))

    assert await store.cancel_batch(created["id"], user_id="bob") is None
    assert (await store.get_batch(created["id"]))["status"] == "validating"
    assert await store.cancel_batch("batch_unknown") is None

    (item,) = await _claim(store, model)
    assert await _complete(store, item)
    finished = await store.get_batch(created["id"])
    assert finished["status"] == "completed"
    assert await store.cancel_batch(created["id"]) == finished


async def test_fail_batch_fails_the_queued_items_and_lets_running_ones_finish(pool, store):
    model = _model()
    created = await _create(store, _items(model, 3))
    first, second = await _claim(store, model, limit=2)
    unauthorized = {"code": "owner_unauthorized", "message": "API key revoked"}

    assert await store.fail_batch(created["id"], unauthorized) is True

    failed = await store.get_batch(created["id"])
    assert failed["status"] == "failed"
    assert failed["failed_at"] is not None
    assert failed["error"] == unauthorized
    assert failed["failed_count"] == 1
    third = (await store.list_results(created["id"]))[2]
    assert third["status"] == "failed"
    assert third["error"]["code"] == "batch_failed"
    assert third["completed_at"] is not None
    assert await _claim(store, model) == []

    assert await _complete(store, first)
    assert await store.release_item(item_id=second["id"], worker_id="worker-1", attempt=1)
    after = await store.get_batch(created["id"])
    assert after["status"] == "failed"
    assert (after["completed_count"], after["failed_count"]) == (1, 2)
    assert await _statuses(pool, created["id"]) == ["completed", "failed", "failed"]

    assert await store.fail_batch(created["id"], unauthorized) is False
    assert await store.fail_batch("batch_unknown", unauthorized) is False
    assert (await store.cancel_batch(created["id"]))["status"] == "failed"


async def test_a_cancel_in_progress_is_not_overridden_by_a_failure(store):
    model = _model()
    created = await _create(store, _items(model, 2))
    await _claim(store, model, limit=1)
    await store.cancel_batch(created["id"])

    assert (
        await store.fail_batch(created["id"], {"code": "owner_unauthorized", "message": "x"})
        is False
    )
    assert (await store.get_batch(created["id"]))["status"] == "cancelling"


async def test_pausing_holds_back_only_that_users_claimable_batches(store):
    model, other = _model(), _model()
    started = await _create(store, _items(model, 2))
    await _claim(store, model, limit=1)
    fresh = await _create(store, _items(model, 2))
    finished = await _create(store, _items(model, 1))
    await store.cancel_batch(finished["id"])
    await _create(store, _items(other, 1), user="bob")
    until = datetime.now(UTC) + timedelta(hours=1)

    assert await store.pause_user_batches("alice", until) == 2

    for batch in (started, fresh):
        assert (await store.get_batch(batch["id"]))["paused_until"] == until
    assert (await store.get_batch(finished["id"]))["paused_until"] is None
    assert await _claim(store, model) == []
    assert len(await _claim(store, other)) == 1


# -- sweep ---------------------------------------------------------------------


async def test_sweep_expires_overdue_batches(pool, store):
    model = _model()
    overdue = await _create(store, _items(model, 3))
    on_time = await _create(store, _items(model, 1), user="bob")
    (running,) = await _claim(store, model, limit=1)
    assert running["batch_id"] == overdue["id"]
    await _execute(
        pool,
        "UPDATE batch_jobs SET expires_at = now() - interval '1 second' WHERE id = $1",
        overdue["id"],
    )

    assert await store.sweep(retention_days=7) == {"expired": 1, "finalized": 0, "purged": 0}

    expired = await store.get_batch(overdue["id"])
    assert expired["status"] == "expired"
    assert expired["expired_at"] is not None
    assert expired["failed_count"] == 2
    assert await _statuses(pool, overdue["id"]) == ["running", "expired", "expired"]
    assert (await store.list_results(overdue["id"]))[1]["error"]["code"] == "batch_expired"
    assert (await store.get_batch(on_time["id"]))["status"] == "validating"

    # The item already running finishes normally; the batch stays expired.
    assert await _complete(store, running)
    after = await store.get_batch(overdue["id"])
    assert after["status"] == "expired"
    assert (after["completed_count"], after["failed_count"]) == (1, 2)
    assert await store.sweep(retention_days=7) == {"expired": 0, "finalized": 0, "purged": 0}


async def test_sweep_finalizes_what_no_worker_will_finish(pool, store):
    model = _model()
    # Cancelling, its last running item abandoned by a worker that died.
    cancelling = await _create(store, _items(model, 2))
    done, abandoned = await _claim(store, model)
    await store.cancel_batch(cancelling["id"])
    assert await _complete(store, done)
    await _expire_lease(pool, abandoned["id"])
    # In progress with nothing left open (as if its last write had been lost).
    drained = await _create(store, _items(model, 2), user="bob")
    await _claim(store, model)
    await _execute(
        pool, "UPDATE batch_items SET status = 'completed' WHERE batch_id = $1", drained["id"]
    )
    # Failed, a running item abandoned: it takes the failure, the batch stays.
    failed = await _create(store, _items(model, 1), user="carol")
    (stuck,) = await _claim(store, model)
    await store.fail_batch(failed["id"], {"code": "owner_unauthorized", "message": "x"})
    await _expire_lease(pool, stuck["id"])

    assert await store.sweep(retention_days=7) == {"expired": 0, "finalized": 2, "purged": 0}

    assert (await store.get_batch(cancelling["id"]))["status"] == "cancelled"
    assert await _statuses(pool, cancelling["id"]) == ["completed", "cancelled"]
    assert (await store.get_batch(drained["id"]))["status"] == "completed"
    failed_now = await store.get_batch(failed["id"])
    assert (failed_now["status"], failed_now["failed_count"]) == ("failed", 1)
    assert await _statuses(pool, failed["id"]) == ["failed"]
    assert await _complete(store, abandoned) is False


async def test_sweep_purges_finished_batches_past_retention(pool, store):
    model = _model()
    old_done = await _create(store, _items(model, 2))
    recent_done = await _create(store, _items(model, 1))
    old_cancelled = await _create(store, _items(model, 1))
    old_active = await _create(store, _items(model, 1), user="bob")
    await store.cancel_batch(old_cancelled["id"])
    for item in await _claim(store, model, limit=10):
        if item["batch_id"] in (old_done["id"], recent_done["id"]):
            assert await _complete(store, item)
    await _execute(
        pool,
        "UPDATE batch_jobs SET completed_at = now() - interval '8 days' WHERE id = $1",
        old_done["id"],
    )
    await _execute(
        pool,
        "UPDATE batch_jobs SET completed_at = now() - interval '6 days' WHERE id = $1",
        recent_done["id"],
    )
    await _execute(
        pool,
        "UPDATE batch_jobs SET cancelled_at = now() - interval '30 days' WHERE id = $1",
        old_cancelled["id"],
    )
    await _execute(
        pool,
        "UPDATE batch_jobs SET created_at = now() - interval '30 days' WHERE id = $1",
        old_active["id"],
    )

    assert await store.sweep(retention_days=7) == {"expired": 0, "finalized": 0, "purged": 2}

    assert await store.get_batch(old_done["id"]) is None
    assert await store.get_batch(old_cancelled["id"]) is None
    assert await _rows(pool, old_done["id"]) == []
    assert (await store.get_batch(recent_done["id"]))["status"] == "completed"
    assert (await store.get_batch(old_active["id"]))["status"] == "in_progress"
    assert (await store.sweep(retention_days=5))["purged"] == 1
    assert await store.get_batch(recent_done["id"]) is None


# -- deletes ---------------------------------------------------------------------


async def test_delete_waits_until_live_items_finish(pool, store):
    model = _model()
    created = await _create(store, _items(model, 3))
    (running,) = await _claim(store, model, limit=1)

    assert await store.delete_batch(created["id"], user_id="bob") is False
    with pytest.raises(BatchDeleteInProgress):
        await store.delete_batch(created["id"], user_id="alice")
    assert await store.get_batch(created["id"]) is not None

    assert await _complete(store, running)
    # The batch is still active with queued items, but no upstream call is live.
    assert (await store.get_batch(created["id"]))["status"] == "in_progress"
    assert await store.delete_batch(created["id"], user_id="alice") is True
    assert await store.get_batch(created["id"]) is None
    assert await _rows(pool, created["id"]) == []
    assert await store.delete_batch(created["id"]) is False


async def test_an_active_queued_batch_can_be_deleted(store):
    created = await _create(store, _items(_model(), 1))

    assert created["status"] == "validating"
    assert await store.delete_batch(created["id"]) is True
    assert await store.get_batch(created["id"]) is None


async def test_a_terminal_batch_with_a_live_item_cannot_be_deleted(store):
    model = _model()
    created = await _create(store, _items(model, 2))
    (running,) = await _claim(store, model, limit=1)
    assert await store.fail_batch(
        created["id"],
        {"code": "owner_unauthorized", "message": "owner key was revoked"},
    )
    assert (await store.get_batch(created["id"]))["status"] == "failed"

    with pytest.raises(BatchDeleteInProgress):
        await store.delete_batch(created["id"])

    assert await _complete(store, running)
    assert await store.delete_batch(created["id"]) is True


async def test_a_running_item_with_an_expired_lease_does_not_block_delete(pool, store):
    model = _model()
    created = await _create(store, _items(model, 1))
    (expired,) = await _claim(store, model)
    await _expire_lease(pool, expired["id"])

    assert await store.delete_batch(created["id"]) is True
    assert (
        await store.heartbeat(worker_id="worker-1", item_ids=[expired["id"]], lease_seconds=60) == 0
    )
    assert await _complete(store, expired) is False


async def test_rejected_delete_cannot_suppress_a_live_heartbeat(pool, store):
    model = _model()
    created = await _create(store, _items(model, 1))
    (running,) = await _claim(store, model, worker="worker-1", lease=60)

    checked = asyncio.Event()
    resume_delete = asyncio.Event()
    delete_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SELECT EXISTS (SELECT 1 FROM batch_items",
            checked,
            resume_delete,
        )
    )
    deleting = asyncio.create_task(delete_store.delete_batch(created["id"]))
    await asyncio.wait_for(checked.wait(), timeout=5)

    heartbeat_started = asyncio.Event()
    heartbeat_acquired = asyncio.Event()
    no_pause = asyncio.Event()
    no_pause.set()
    heartbeat_store = BatchStore(
        _PauseAfterStatementPool(
            pool,
            "SELECT pg_advisory_xact_lock",
            heartbeat_acquired,
            no_pause,
            heartbeat_started,
        )
    )
    renewing = asyncio.create_task(
        heartbeat_store.heartbeat(
            worker_id="worker-1",
            item_ids=[running["id"]],
            lease_seconds=3600,
        )
    )
    try:
        await asyncio.wait_for(heartbeat_started.wait(), timeout=5)
        assert not renewing.done()
    finally:
        resume_delete.set()

    with pytest.raises(BatchDeleteInProgress):
        await asyncio.wait_for(deleting, timeout=5)
    assert await asyncio.wait_for(renewing, timeout=5) == 1
    assert await store.get_batch(created["id"]) is not None


async def test_a_hard_delete_purges_every_batch_of_that_user_only(pool, store):
    model = _model()
    mine = [await _create(store, _items(model, 2)) for _ in range(2)]
    theirs = await _create(store, _items(model, 2), user="bob")
    await _claim(store, model, limit=1)

    assert await store.delete_user_batches("alice") == 2

    for batch in mine:
        assert await store.get_batch(batch["id"]) is None
        assert await _rows(pool, batch["id"]) == []
    assert (await store.get_batch(theirs["id"]))["user_id"] == "bob"
    assert len(await _rows(pool, theirs["id"])) == 2
    assert await store.delete_user_batches("alice") == 0


# -- admin -----------------------------------------------------------------------


async def test_model_limits_are_set_replaced_and_removed(pool, store):
    assert await store.list_model_limits() == {}

    await store.set_model_limit("model-a", 3, "admin-1")
    await store.set_model_limit("model-b", 0, None)
    assert await store.list_model_limits() == {"model-a": 3, "model-b": 0}

    await store.set_model_limit("model-a", 5, "admin-2")
    assert await store.list_model_limits() == {"model-a": 5, "model-b": 0}
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT updated_by FROM batch_model_limits WHERE model_id = 'model-a'"
            )
            == "admin-2"
        )
    with pytest.raises(ValueError):
        await store.set_model_limit("model-a", -1, "admin-1")

    assert await store.delete_model_limit("model-a") is True
    assert await store.delete_model_limit("model-a") is False
    assert await store.list_model_limits() == {"model-b": 0}


async def test_model_stats_count_live_running_and_queued_items(pool, store):
    first, second = _model(), _model()
    await _create(store, [*_items(first, 3, prefix="a"), *_items(second, 2, prefix="b")])
    finished = await _create(store, _items(first, 1))

    claimed = await _claim(store, first, limit=1)
    (done,) = [item for item in await _claim(store, first) if item["batch_id"] == finished["id"]]
    assert await _complete(store, done)

    stats = await store.model_stats()
    assert stats == {
        first: {"running": 1 + 2, "queued": 0},
        second: {"running": 0, "queued": 2},
    }

    await _expire_lease(pool, claimed[0]["id"])
    assert (await store.model_stats())[first] == {"running": 2, "queued": 0}


# -- schema ----------------------------------------------------------------------


async def test_initialize_is_idempotent(pool, store):
    created = await _create(store, _items(_model(), 1))

    await BatchStore(pool).initialize()

    assert await store.get_batch(created["id"]) is not None


async def test_processes_booting_at_once_create_the_schema_once():
    schema = f"batch_boot_{uuid.uuid4().hex[:12]}"
    admin = await postgres.create_pool(min_size=1, max_size=1)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        fresh = await postgres.create_pool(
            min_size=4, max_size=4, server_settings={"search_path": schema}
        )
        try:
            await asyncio.gather(*(BatchStore(fresh).initialize() for _ in range(4)))
            names = await fresh.fetch(
                "SELECT c.relname FROM pg_class AS c JOIN pg_namespace AS n "
                "ON n.oid = c.relnamespace WHERE n.nspname = $1",
                schema,
            )
            store = BatchStore(fresh)
            created = await _create(store, _items("model-x", 2), key_hash=None)
            assert len(await _claim(store, "model-x")) == 2
            assert (await store.get_batch(created["id"]))["status"] == "in_progress"
        finally:
            await fresh.close()
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()

    expected = {name for name, _ in (*batch_store._TABLES, *batch_store._INDEXES)}
    assert expected <= {row["relname"] for row in names}


async def test_a_settled_schema_takes_no_ddl():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])  # nothing missing
    conn.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    await BatchStore(pool).initialize()

    conn.execute.assert_not_awaited()


async def test_a_corrupt_json_column_reads_as_none_not_an_exception():
    assert batch_store._load_json('{"ok": true}') == {"ok": True}
    assert batch_store._load_json(b"[1]") == [1]
    assert batch_store._load_json("{not json") is None
    assert batch_store._load_json({"already": "decoded"}) == {"already": "decoded"}
