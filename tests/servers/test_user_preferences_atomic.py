"""Preference writes against Postgres: one key at a time, newest choice wins.

Run with: make test-db
"""

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


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
