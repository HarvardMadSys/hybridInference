"""The Responses API store against Postgres.

The router tests use an in-memory stand-in, so this is where the SQL runs. It
carries two promises the router relies on: a delete scoped to an owner removes
nothing of anyone else's, and a hard delete purges every stored conversation of
that user and no other. Privacy mode stops writes but must leave reads and both
deletes working, or stored rows could no longer be erased.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.storage import responses_store
from serving.storage.responses_store import ResponseStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def users(pool):
    """Two user ids unique to this test; their rows are removed afterwards."""
    tag = uuid.uuid4().hex[:8]
    return f"rs-alice-{tag}", f"rs-bob-{tag}"


@pytest_asyncio.fixture
async def store(pool, users):
    store = ResponseStore(pool)
    try:
        yield store
    finally:
        for user in users:
            await store.delete_user_responses(user)


def _rid() -> str:
    return f"resp_{uuid.uuid4().hex}"


async def _save(store: ResponseStore, user: str, rid: str | None = None, **overrides) -> str:
    rid = rid or _rid()
    fields = {
        "response_id": rid,
        "user_id": user,
        "response": {"id": rid, "status": "completed", "output": [{"text": "héllo ✓"}]},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "héllo ✓", "reasoning_content": "thought"},
        ],
        "previous_response_id": None,
        "model": "glm-4.7",
    }
    fields.update(overrides)
    await store.save(**fields)
    return rid


async def test_a_saved_response_reads_back_as_saved(store, users):
    alice, _ = users
    first = await _save(store, alice)
    second = await _save(store, alice, previous_response_id=first, model="other-model")

    row = await store.get(second)
    async with store._pool.acquire() as conn:
        stored_bytes = await conn.fetchval(
            "SELECT octet_length(messages::text) FROM openai_responses WHERE id = $1", second
        )

    assert row == {
        "id": second,
        "user_id": alice,
        "response": {"id": second, "status": "completed", "output": [{"text": "héllo ✓"}]},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "héllo ✓", "reasoning_content": "thought"},
        ],
        "previous_response_id": first,
        "model": "other-model",
        # The stored conversation's size, as PostgreSQL renders it, in UTF-8 bytes.
        "messages_bytes": stored_bytes,
    }
    assert await store.get(_rid()) is None


async def test_saving_an_id_again_replaces_the_turn_but_never_the_owner(store, users):
    """An aborted stream is saved, then re-saved; the row must stay its owner's."""
    alice, bob = users
    rid = await _save(store, alice, response={"status": "incomplete"})

    await _save(store, bob, rid, response={"status": "completed"}, messages=[], model="m2")

    row = await store.get(rid)
    assert row["user_id"] == alice
    assert row["response"] == {"status": "completed"}
    assert row["messages"] == []
    assert row["model"] == "m2"


async def test_a_delete_scoped_to_another_user_removes_nothing(store, users):
    alice, bob = users
    rid = await _save(store, alice)

    assert await store.delete(rid, user_id=bob) is False
    assert await store.get(rid) is not None

    assert await store.delete(rid, user_id=alice) is True
    assert await store.get(rid) is None
    assert await store.delete(rid, user_id=alice) is False


async def test_an_unscoped_delete_removes_any_owners_row(store, users):
    alice, _ = users
    rid = await _save(store, alice)

    assert await store.delete(rid) is True
    assert await store.get(rid) is None


async def test_a_hard_delete_purges_every_response_of_that_user_only(store, users):
    alice, bob = users
    alice_rows = [await _save(store, alice) for _ in range(3)]
    bob_row = await _save(store, bob)

    assert await store.delete_user_responses(alice) == 3

    for rid in alice_rows:
        assert await store.get(rid) is None
    assert (await store.get(bob_row))["user_id"] == bob
    assert await store.delete_user_responses(alice) == 0


async def test_privacy_mode_stops_writes_but_not_reads_or_erasure(pool, store, users):
    alice, _ = users
    kept = await _save(store, alice)
    erased = await _save(store, alice)
    private = ResponseStore(pool, persist_enabled=False)

    refused = await _save(private, alice)
    await _save(private, alice, kept, response={"status": "overwritten"})

    assert await private.get(refused) is None
    assert (await private.get(kept))["response"]["status"] == "completed"
    assert await private.delete(erased, user_id=alice) is True
    assert await private.delete_user_responses(alice) == 1


async def test_initialize_is_idempotent(pool, store, users):
    alice, _ = users
    rid = await _save(store, alice)

    assert await store.get(rid) is not None


async def test_a_corrupt_json_column_reads_as_none_not_an_exception():
    assert responses_store._load_json('{"ok": true}') == {"ok": True}
    assert responses_store._load_json(b"[1]") == [1]
    assert responses_store._load_json("{not json") is None
    assert responses_store._load_json({"already": "decoded"}) == {"already": "decoded"}


async def _age(pool, rid: str, days: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE openai_responses SET created_at = now() - make_interval(secs => $2) "
            "WHERE id = $1",
            rid,
            days * 86400,
        )


async def _exists(store: ResponseStore, rid: str) -> bool:
    return await store.get(rid) is not None


async def test_the_retention_purge_removes_responses_older_than_30_days_only(pool, store, users):
    alice, bob = users
    expired = [await _save(store, alice) for _ in range(3)] + [await _save(store, bob)]
    kept = [await _save(store, alice), await _save(store, bob)]
    for rid in expired:
        await _age(pool, rid, 30.01)
    await _age(pool, kept[0], 29.99)

    # Two at a time: the purge keeps going until a statement finds fewer.
    assert await responses_store.purge_expired(pool, batch=2) >= len(expired)

    assert [await _exists(store, rid) for rid in expired] == [False] * len(expired)
    assert [await _exists(store, rid) for rid in kept] == [True, True]
    assert await responses_store.purge_expired(pool) == 0


async def test_a_chain_older_than_the_retention_continues_from_its_newest_turn(pool, store, users):
    """Each turn carries the whole conversation, so purging the old turns loses nothing of it."""
    alice, _ = users
    first = await _save(store, alice, messages=[{"role": "user", "content": "turn 1"}])
    conversation = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "turn 2"},
    ]
    latest = await _save(store, alice, previous_response_id=first, messages=conversation)
    await _age(pool, first, 31)

    await responses_store.purge_expired(pool)

    assert not await _exists(store, first)
    assert (await store.get(latest))["messages"] == conversation


async def test_the_retention_purge_reads_the_owner_index_not_the_table(pool, users):
    """Many owners and rows: the plan walks idx_openai_responses_user and the primary key."""
    tag = users[0]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO openai_responses (id, user_id, response, messages, created_at) "
            "SELECT $1 || '-' || g, $1 || '-' || (g % 40), '{}', "
            "jsonb_build_array(jsonb_build_object('content', repeat('x', 500))), "
            "now() - make_interval(days => g % 60) "
            "FROM generate_series(1, 4000) AS g",
            tag,
        )
        try:
            await conn.execute("ANALYZE openai_responses")
            plan = "\n".join(
                r[0] for r in await conn.fetch("EXPLAIN " + responses_store._PURGE_SQL, 30, 1000)
            )
        finally:
            await conn.execute("DELETE FROM openai_responses WHERE user_id LIKE $1 || '-%'", tag)

    assert "Seq Scan on openai_responses" not in plan, plan
    assert "idx_openai_responses_user" in plan
    assert "openai_responses_pkey" in plan
