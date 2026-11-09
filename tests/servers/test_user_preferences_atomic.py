"""Preference writes against Postgres: one key at a time, newest choice wins.

Run with: make test-db
"""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from serving.exceptions import PreferencesTooLargeError
from serving.storage.base import USER_PREFERENCES_MAX_BYTES

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _choice(value: str, at: datetime) -> dict:
    return {
        "value": value,
        "chosen_at": at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }


async def _stored(pool, user_id: str) -> dict:
    raw = await pool.fetchval("SELECT preferences FROM users WHERE id = $1", user_id)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.asyncio
async def test_an_older_choice_waiting_on_the_row_does_not_land_over_a_newer_one(
    auth_backend, test_user
):
    """The overlap that lost the newer choice: older request finishes last."""
    store, _, db_logger, _ = auth_backend
    pool = db_logger.pool
    newer = _choice("zh-CN", T0 + timedelta(minutes=5))

    async with pool.acquire() as holder:
        tx = holder.transaction()
        await tx.start()
        # The newer choice is written and holds the row, uncommitted...
        await holder.execute(
            "UPDATE users SET preferences = jsonb_set(COALESCE(preferences, '{}'::jsonb), "
            "'{locale}', $2::jsonb) WHERE id = $1",
            test_user["id"],
            json.dumps(newer),
        )
        # ...while the older one arrives and has to wait for it.
        older = asyncio.create_task(
            store.set_user_preference_if_newer(test_user["id"], "locale", _choice("en", T0), T0)
        )
        await asyncio.sleep(0.3)
        assert not older.done()
        await tx.commit()

    kept = await older
    assert kept == newer
    assert (await _stored(pool, test_user["id"]))["locale"] == newer


@pytest.mark.asyncio
async def test_concurrent_choices_leave_the_most_recent(auth_backend, test_user):
    store, _, db_logger, _ = auth_backend
    moments = [T0 + timedelta(seconds=i) for i in range(20)]
    random.Random(7).shuffle(moments)

    await asyncio.gather(
        *(
            store.set_user_preference_if_newer(
                test_user["id"], "locale", _choice("en" if i % 2 else "zh-CN", at), at
            )
            for i, at in enumerate(moments)
        )
    )

    stored = (await _stored(db_logger.pool, test_user["id"]))["locale"]
    assert stored["chosen_at"] == _choice("en", max(moments))["chosen_at"]


@pytest.mark.asyncio
async def test_writes_to_different_keys_do_not_lose_each_other(auth_backend, test_user):
    """The other lost update: a whole-column write from a stale read."""
    store, _, db_logger, _ = auth_backend
    layout = {"direct_models": ["m"], "direct_providers": {}, "e2e_models": []}

    for round_ in range(10):
        at = T0 + timedelta(minutes=round_)
        await asyncio.gather(
            store.set_user_preference_if_newer(test_user["id"], "locale", _choice("zh-CN", at), at),
            store.set_user_preference(test_user["id"], "llm_prober_layout", layout),
            store.set_user_preference(test_user["id"], "disabled_models", [f"m{round_}"]),
        )
        stored = await _stored(db_logger.pool, test_user["id"])
        assert stored["locale"]["value"] == "zh-CN"
        assert stored["llm_prober_layout"] == layout
        assert stored["disabled_models"] == [f"m{round_}"]

    assert await store.delete_user_preference(test_user["id"], "llm_prober_layout")
    stored = await _stored(db_logger.pool, test_user["id"])
    assert "llm_prober_layout" not in stored
    assert stored["locale"]["value"] == "zh-CN"


@pytest.mark.asyncio
async def test_a_row_without_an_object_and_a_missing_user(auth_backend, test_user):
    store, _, db_logger, _ = auth_backend
    await db_logger.pool.execute(
        "UPDATE users SET preferences = 'null'::jsonb WHERE id = $1", test_user["id"]
    )

    assert await store.set_user_preference(test_user["id"], "disabled_models", ["a"])
    assert (await _stored(db_logger.pool, test_user["id"])) == {"disabled_models": ["a"]}

    # A stored time that is not a timestamp counts as no choice, without an error.
    await store.set_user_preference(test_user["id"], "locale", {"value": "en", "chosen_at": "x"})
    kept = await store.set_user_preference_if_newer(
        test_user["id"], "locale", _choice("zh-CN", T0), T0
    )
    assert kept == _choice("zh-CN", T0)

    assert not await store.set_user_preference("no-such-user", "k", 1)
    assert not await store.delete_user_preference("no-such-user", "k")
    assert await store.set_user_preference_if_newer("no-such-user", "locale", {}, T0) is None


# ---------------------------------------------------------------------------
# Bounded writes, and reads that never carry a whole document per request
# ---------------------------------------------------------------------------


def _layout(names: int, length: int = 40) -> dict:
    return {
        "direct_models": [f"{i:0{length}d}" for i in range(names)],
        "direct_providers": {},
        "e2e_models": [],
    }


@pytest.mark.asyncio
async def test_a_write_that_would_overfill_the_document_is_refused_and_changes_nothing(
    auth_backend, test_user
):
    store, _, db_logger, _ = auth_backend
    user_id = test_user["id"]
    await store.set_user_preference_if_newer(user_id, "locale", _choice("en", T0), T0)
    small = _layout(3)
    assert await store.set_user_preference(
        user_id, "llm_prober_layout", small, max_total_bytes=USER_PREFERENCES_MAX_BYTES
    )

    # Fits on its own, but not beside what the document already holds.
    alone = len(json.dumps(_layout(160)))
    assert alone < 8000
    with pytest.raises(PreferencesTooLargeError):
        await store.set_user_preference(
            user_id, "llm_prober_layout", _layout(160), max_total_bytes=alone
        )

    stored = await _stored(db_logger.pool, user_id)
    assert stored["llm_prober_layout"] == small
    assert stored["locale"]["value"] == "en"
    # A missing user is still "no such user", not "too large".
    assert not await store.set_user_preference(
        "no-such-user", "llm_prober_layout", small, max_total_bytes=USER_PREFERENCES_MAX_BYTES
    )


@pytest.mark.asyncio
async def test_lookups_carry_only_the_entries_a_request_reads(auth_backend, test_user_with_key):
    from serving.servers.auth import hash_api_key
    from serving.storage.postgres_operational import PostgresOperationalStore

    _, _, db_logger, _ = auth_backend
    # The statements themselves, without the cache in front of them.
    store = PostgresOperationalStore(db_logger.pool)
    user_id = test_user_with_key["id"]
    key_hash = hash_api_key(test_user_with_key["api_key"])
    await store.set_user_preference(user_id, "disabled_models", ["m-off"])
    await store.set_user_preference_if_newer(user_id, "locale", _choice("zh-CN", T0), T0)
    await store.set_user_preference(user_id, "llm_prober_layout", _layout(5))

    for lookup in (
        store.get_auth_context_by_key_hash,
        store.get_auth_context_with_spend,
        store.get_auth_context_lightweight,
    ):
        row = await lookup(key_hash)
        assert row["preferences"] == {"disabled_models": ["m-off"]}, lookup.__name__

    expected = {"disabled_models": ["m-off"], "locale": _choice("zh-CN", T0)}
    assert (await store.get_user_by_id(user_id))["preferences"] == expected
    assert (await store.get_user_by_email(test_user_with_key["email"]))["preferences"] == expected
    # The whole document is still there for the one reader that asks for it.
    assert (await store.get_user_preferences(user_id))["llm_prober_layout"] == _layout(5)

    # An account with none of these entries reads as an empty object, as before.
    await db_logger.pool.execute(
        "UPDATE users SET preferences = '\"x\"'::jsonb WHERE id = $1", user_id
    )
    assert (await store.get_auth_context_with_spend(key_hash))["preferences"] == {}
    assert (await store.get_user_by_id(user_id))["preferences"] == {}


@pytest.mark.asyncio
async def test_an_oversized_document_stored_earlier_reads_as_its_request_entries(
    auth_backend, test_user
):
    """A layout saved before writes were bounded: no migration trims it, so reads must."""
    store, _, db_logger, _ = auth_backend
    user_id = test_user["id"]
    await store.set_user_preference(user_id, "disabled_models", ["m-off"])
    await store.set_user_preference(user_id, "llm_prober_layout", _layout(20_000))
    assert len(json.dumps(await _stored(db_logger.pool, user_id))) > 800_000

    assert await store.get_user_preferences(user_id) == {"disabled_models": ["m-off"]}

    # The account holder's next save replaces it: the bound is on the result.
    assert await store.set_user_preference(
        user_id, "llm_prober_layout", _layout(2), max_total_bytes=USER_PREFERENCES_MAX_BYTES
    )
    assert (await store.get_user_preferences(user_id))["llm_prober_layout"] == _layout(2)
