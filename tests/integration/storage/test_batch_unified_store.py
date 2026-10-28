"""The unified batch API's storage contracts, against real PostgreSQL.

Messages runs through ``/v1/batches`` as two new kinds -- ``(openai_inline,
messages)`` and ``(openai_file, messages)`` -- that this version claims with
their frozen ``anthropic_version`` and that a worker of the previous release
(whose kind list lacks them) never matches. Batches the retired Message
Batches API stored keep draining. The status a listing filters on is the one
it shows, for every kind and phase, and every batch that ends now announces
itself as ``batch.<status>``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, get_args

import pytest
import pytest_asyncio

from serving.batch.renderers import display_status
from serving.batch.schemas import BatchStatus
from serving.storage.batch_store import SUPPORTED_JOB_KINDS, BatchStore, _supported_kinds_sql
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: ``SUPPORTED_JOB_KINDS`` as the release before this one (dev ``df40a9a82``)
#: shipped it: what an old worker still running in a rolling upgrade matches.
PREVIOUS_RELEASE_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("openai_inline", "chat"),
        ("openai_inline", "embeddings"),
        ("openai_inline", "responses"),
        ("openai_file", "chat"),
        ("openai_file", "embeddings"),
        ("openai_file", "responses"),
        ("anthropic", "messages"),
    }
)


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=8)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    store = BatchStore(pool)
    users: list[str] = []
    store.test_users = users  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        for user in users:
            await store.delete_user_batches(user)


def _user(store: BatchStore) -> str:
    user = f"uni-{uuid.uuid4().hex[:10]}"
    store.test_users.append(user)  # type: ignore[attr-defined]
    return user


def _model() -> str:
    return f"m-{uuid.uuid4().hex[:8]}"


def _messages_items(model: str, n: int, prefix: str = "m") -> list[dict[str, Any]]:
    return [
        {
            "custom_id": f"{prefix}-{i}",
            "model_id": model,
            "request": {
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": f"q{i}"}],
            },
        }
        for i in range(n)
    ]


async def _create(store: BatchStore, user: str, items: list[dict[str, Any]], **kwargs: Any):
    defaults = {
        "batch_id": f"batch_{uuid.uuid4().hex}",
        "user_id": user,
        "key_hash": None,
        "endpoint": "/v1/messages",
        "model_ids": sorted({item["model_id"] for item in items}),
        "items": items,
        "metadata": None,
        "completion_window_seconds": 3600,
        "retention_seconds": 7 * 86400,
        "target": "messages",
    }
    defaults.update(kwargs)
    return await store.create_batch(**defaults)


async def _claim(store: BatchStore, model: str) -> list[dict[str, Any]]:
    return await store.claim_items(
        model_id=model, worker_id="w1", limit=10, max_running=100, lease_seconds=300
    )


async def _complete(store: BatchStore, item: dict[str, Any]) -> bool:
    return await store.complete_item(
        item_id=item["id"],
        worker_id="w1",
        attempt=item["attempt_seq"],
        response={"type": "message", "id": item["custom_id"]},
        error=None,
        status_code=200,
    )


async def test_this_version_adds_exactly_the_two_messages_kinds():
    added = SUPPORTED_JOB_KINDS - PREVIOUS_RELEASE_KINDS
    assert added == {("openai_inline", "messages"), ("openai_file", "messages")}
    assert PREVIOUS_RELEASE_KINDS <= SUPPORTED_JOB_KINDS, "old batches must still drain"


@pytest.mark.parametrize("surface", ["openai_inline", "openai_file"])
async def test_a_v1_batches_messages_batch_is_claimed_with_its_version(store, surface):
    user, model = _user(store), _model()
    await _create(
        store, user, _messages_items(model, 2), api_surface=surface, protocol_profile="2026-10-21"
    )

    claimed = await _claim(store, model)

    assert sorted(item["custom_id"] for item in claimed) == ["m-0", "m-1"]
    assert {(item["api_surface"], item["target"]) for item in claimed} == {(surface, "messages")}
    assert {item["protocol_profile"] for item in claimed} == {"2026-10-21"}


async def test_a_previous_release_worker_matches_no_new_messages_batch(pool, store):
    """The predicate every claim, sweep, validation and seal step shares, as the old worker has it."""
    user, model = _user(store), _model()
    inline = await _create(
        store, user, _messages_items(model, 1), api_surface="openai_inline",
        protocol_profile="2023-06-01",
    )  # fmt: skip
    from_file = await _create(
        store, user, [], api_surface="openai_file", phase="validating",
        input_file_id=None, protocol_profile="2023-06-01", model_ids=[model],
    )  # fmt: skip
    legacy = await _create(
        store, user, _messages_items(model, 1, "l"), api_surface="anthropic",
        protocol_profile="2023-06-01",
    )  # fmt: skip
    chat = await _create(
        store, user, [{"custom_id": "c", "model_id": model, "request": {"model": model}}],
        api_surface="openai_inline", target="chat", endpoint="/v1/chat/completions",
    )  # fmt: skip
    ids = [inline["id"], from_file["id"], legacy["id"], chat["id"]]

    async def matching(kinds: frozenset[tuple[str, str]]) -> set[str]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT j.id FROM batch_jobs AS j "
                f"WHERE j.id = ANY($1::text[]) AND {_supported_kinds_sql('j', kinds)}",
                ids,
            )
        return {row["id"] for row in rows}

    assert await matching(PREVIOUS_RELEASE_KINDS) == {legacy["id"], chat["id"]}
    assert await matching(SUPPORTED_JOB_KINDS) == set(ids)


async def test_a_legacy_message_batch_still_runs_seals_and_ends_as_batch_completed(pool, store):
    """Drained by this version; its end announces it the unified way."""
    user, model = _user(store), _model()
    await BatchWebhookStore(pool).set_webhook(
        user_id=user, url="https://hooks.example.test/h", secret_encrypted="x", enabled=True
    )
    # An event the previous release wrote and did not deliver yet.
    legacy_payload = {
        "id": "evt_legacy",
        "object": "event",
        "type": "message_batch.ended",
        "data": {"id": "msgbatch_old"},
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO batch_webhook_events (id, user_id, batch_id, event_type, payload) "
            "VALUES ('evt_legacy_' || $2, $1, 'msgbatch_old', 'message_batch.ended', $3::jsonb)",
            user,
            uuid.uuid4().hex[:8],
            json.dumps(legacy_payload),
        )
    batch = await _create(
        store,
        user,
        _messages_items(model, 1),
        batch_id=f"msgbatch_{uuid.uuid4().hex}",
        api_surface="anthropic",
        protocol_profile="2023-06-01",
    )
    (item,) = await _claim(store, model)
    assert await _complete(store, item)

    row = await store.get_batch(batch["id"])
    assert (row["phase"], row["status"], display_status(row)) == (
        "sealed",
        "completed",
        "completed",
    )
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT batch_id, event_type, payload FROM batch_webhook_events WHERE user_id = $1",
            user,
        )
    by_batch = {event["batch_id"]: event for event in events}
    assert by_batch[batch["id"]]["event_type"] == "batch.completed"
    assert json.loads(by_batch[batch["id"]]["payload"])["data"] == {"id": batch["id"]}
    # The pending legacy event is delivered as it was written.
    assert by_batch["msgbatch_old"]["event_type"] == "message_batch.ended"
    assert json.loads(by_batch["msgbatch_old"]["payload"]) == legacy_payload


async def test_filtering_by_status_matches_exactly_the_status_each_batch_shows(pool, store):
    """``display_status_sql`` and ``display_status`` agree for every surface, phase and status."""
    user = _user(store)
    statuses = ("validating", "in_progress", "cancelling", "completed", "failed", "expired")
    combos = [
        (surface, phase, status, pending)
        for surface in ("openai_inline", "openai_file", "anthropic")
        for phase in ("validating", "runnable", "sealing", "sealed")
        for status in (*statuses, "cancelled")
        for pending in ((None, "completed", "cancelled") if phase == "sealing" else (None,))
    ]
    for surface, phase, status, pending in combos:
        # Created validating (nothing settles it), then put in the state under test.
        created = await _create(store, user, [], api_surface=surface, phase="validating")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE batch_jobs SET phase = $2, status = $3, pending_terminal_status = $4 "
                "WHERE id = $1",
                created["id"],
                phase,
                status,
                pending,
            )
    rows = await store.list_batches(user_id=user, limit=1000)
    assert len(rows) == len(combos)
    shown = {row["id"]: display_status(row) for row in rows}
    for status in get_args(BatchStatus):
        listed = await store.list_batches(user_id=user, limit=1000, status=status)
        expected = {batch_id for batch_id, value in shown.items() if value == status}
        assert {row["id"] for row in listed} == expected, status
    # Every displayed value is one the API documents.
    assert set(shown.values()) <= set(get_args(BatchStatus))
    # "ended" is never a status: a legacy batch shows its real outcome.
    assert "ended" not in shown.values()


async def test_a_new_messages_batch_and_a_legacy_one_are_read_and_listed_alike(store):
    user, other, model = _user(store), _user(store), _model()
    new = await _create(
        store, user, _messages_items(model, 1), api_surface="openai_inline",
        protocol_profile="2026-10-21",
    )  # fmt: skip
    legacy = await _create(
        store, user, _messages_items(model, 1, "l"), batch_id=f"msgbatch_{uuid.uuid4().hex}",
        api_surface="anthropic", protocol_profile="2023-06-01",
    )  # fmt: skip

    listed = [row["id"] for row in await store.list_batches(user_id=user, limit=10)]
    assert set(listed) == {new["id"], legacy["id"]}
    assert await store.list_batches(user_id=other, limit=10) == []
    assert await store.get_batch(legacy["id"], user_id=other) is None
    assert await store.cancel_batch(legacy["id"], user_id=other) is None
    assert not await store.delete_batch(legacy["id"], user_id=other)
    cancelled = await store.cancel_batch(legacy["id"], user_id=user)
    assert (cancelled["status"], cancelled["phase"], cancelled["cancelled_count"]) == (
        "cancelled",
        "sealed",
        1,
    )
    # The other one is untouched and still claimable with its version.
    (item,) = await _claim(store, model)
    assert (item["batch_id"], item["protocol_profile"]) == (new["id"], "2026-10-21")
