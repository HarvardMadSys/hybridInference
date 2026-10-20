"""The admin API-key methods of ``PostgresOperationalStore`` against real SQL.

The admin routes are tested with these methods mocked, so until now none of
their SQL had run against PostgreSQL. What matters most is the history a user
accumulates: revoking a key and creating a new one leaves the revoked row
behind, and every method here must pick the live key out of that history and
never bring a revoked credential back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_PREFIX = "sk-admkey"


@pytest_asyncio.fixture
async def keys():
    """A store and its pool, with this file's users and keys wiped around each test."""
    pool = await postgres.create_pool(min_size=1, max_size=5)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(f"DELETE FROM api_keys WHERE key_prefix LIKE '{_PREFIX}%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-admkey-%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _user(store: PostgresOperationalStore, user_id: str) -> None:
    await store.create_user(user_id=user_id, email=f"{user_id}@example.com", password_hash="x")


async def _key(pool: Any, user_id: str, name: str, status: str, metadata: str | None = None):
    """Insert a key row directly, so a test controls its status and history."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, status, metadata) "
            "VALUES ($1, $2, $3, $3, $4, $5::jsonb)",
            f"hash-{name}",
            f"{_PREFIX}-{name}",
            user_id,
            status,
            metadata,
        )


async def _rows(pool: Any, user_id: str) -> dict[str, dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_prefix, key_hash, status, notes, metadata FROM api_keys WHERE user_id = $1",
            user_id,
        )
    return {row["key_prefix"]: dict(row) for row in rows}


# --- get_key_detail: the live key out of a user's history -------------------


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        (["revoked", "active"], "active"),
        (["active", "revoked"], "active"),  # the newer revoked row does not win
        (["revoked", "suspended"], "suspended"),
        (["suspended", "revoked"], "suspended"),
        (["revoked", "revoked"], "revoked"),
    ],
)
async def test_detail_prefers_the_live_key_over_newer_history(keys, history, expected):
    store, pool = keys
    await _user(store, "u-admkey-hist")
    for index, status in enumerate(history):
        await _key(pool, "u-admkey-hist", f"hist{index}-{status}", status)

    detail = await store.get_key_detail("u-admkey-hist")

    assert detail is not None and detail["status"] == expected
    if history.count(expected) == 2:
        # Within one status, the newest row.
        assert detail["key_prefix"] == f"{_PREFIX}-hist1-{expected}"


async def test_detail_of_a_user_without_keys_is_none(keys):
    store, _pool = keys
    await _user(store, "u-admkey-none")

    assert await store.get_key_detail("u-admkey-none") is None


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{"team": "ml", "tier": 2}', {"team": "ml", "tier": 2}),
        ('["not", "an", "object"]', None),  # the response model expects a mapping
        (None, None),
    ],
)
async def test_detail_decodes_metadata_to_a_mapping(keys, stored, expected):
    store, pool = keys
    await _user(store, "u-admkey-meta")
    await _key(pool, "u-admkey-meta", "meta", "active", stored)

    detail = await store.get_key_detail("u-admkey-meta")

    assert detail is not None and detail["metadata"] == expected


# --- list_keys: filter, count and page with the same placeholders ----------


async def test_list_counts_and_pages_within_a_status(keys):
    """The status filter shifts every placeholder; count and page must agree."""
    store, pool = keys
    before, _ = await store.list_keys(status="suspended", limit=1)
    for index in range(3):
        await _user(store, f"u-admkey-list{index}")
        await _key(pool, f"u-admkey-list{index}", f"list{index}", "suspended")
    await _user(store, "u-admkey-listactive")
    await _key(pool, "u-admkey-listactive", "listactive", "active")

    total, first = await store.list_keys(status="suspended", limit=1, offset=0)
    _, second = await store.list_keys(status="suspended", limit=1, offset=1)

    assert total == before + 3
    # Newest first; this test's rows are the newest in the table.
    assert [row["key_prefix"] for row in first + second] == [
        f"{_PREFIX}-list2",
        f"{_PREFIX}-list1",
    ]
    assert {row["status"] for row in first + second} == {"suspended"}


async def test_list_without_a_filter_counts_every_status(keys):
    store, pool = keys
    before, _ = await store.list_keys(limit=1)
    await _user(store, "u-admkey-all")
    await _key(pool, "u-admkey-all", "all-revoked", "revoked")
    await _key(pool, "u-admkey-all", "all-active", "active")

    total, rows = await store.list_keys(limit=2)

    assert total == before + 2
    assert [row["key_prefix"] for row in rows] == [
        f"{_PREFIX}-all-active",
        f"{_PREFIX}-all-revoked",
    ]
    # The list never carries the credential itself.
    assert all("key_hash" not in row for row in rows)


# --- update_key / revoke_key / regenerate_key: never resurrect a revoked key --


async def test_update_leaves_revoked_rows_alone(keys):
    store, pool = keys
    await _user(store, "u-admkey-upd")
    await _key(pool, "u-admkey-upd", "upd-old", "revoked")
    await _key(pool, "u-admkey-upd", "upd-live", "active")

    await store.update_key("u-admkey-upd", notes="rotated", metadata={"owner": "ops"})

    rows = await _rows(pool, "u-admkey-upd")
    assert rows[f"{_PREFIX}-upd-live"]["notes"] == "rotated"
    assert json.loads(rows[f"{_PREFIX}-upd-live"]["metadata"]) == {"owner": "ops"}
    assert rows[f"{_PREFIX}-upd-old"]["notes"] is None
    assert rows[f"{_PREFIX}-upd-old"]["status"] == "revoked"


async def test_update_refuses_a_column_outside_the_allow_list(keys):
    store, pool = keys
    await _user(store, "u-admkey-col")
    await _key(pool, "u-admkey-col", "col", "active")

    with pytest.raises(ValueError, match="key_hash"):
        await store.update_key("u-admkey-col", key_hash="attacker-chosen")

    assert (await _rows(pool, "u-admkey-col"))[f"{_PREFIX}-col"]["key_hash"] == "hash-col"


async def test_revoke_soft_keeps_the_rows_and_hard_deletes_them(keys):
    store, pool = keys
    for user in ("u-admkey-soft", "u-admkey-hard"):
        await _user(store, user)
        await _key(pool, user, f"{user[9:]}-a", "active")
        await _key(pool, user, f"{user[9:]}-b", "suspended")

    await store.revoke_key("u-admkey-soft")
    await store.revoke_key("u-admkey-hard", hard_delete=True)

    assert {row["status"] for row in (await _rows(pool, "u-admkey-soft")).values()} == {"revoked"}
    assert await _rows(pool, "u-admkey-hard") == {}
    assert await store.get_key_detail("u-admkey-hard") is None


async def test_regenerate_rewrites_only_the_active_key(keys):
    store, pool = keys
    await _user(store, "u-admkey-regen")
    await _key(pool, "u-admkey-regen", "regen-old", "revoked")
    await _key(pool, "u-admkey-regen", "regen-live", "active")

    old_prefix = await store.regenerate_key(
        "u-admkey-regen", new_key_hash="hash-regen-new", new_key_prefix=f"{_PREFIX}-regen-new"
    )

    assert old_prefix == f"{_PREFIX}-regen-live"
    rows = await _rows(pool, "u-admkey-regen")
    assert rows[f"{_PREFIX}-regen-new"]["key_hash"] == "hash-regen-new"
    assert rows[f"{_PREFIX}-regen-new"]["status"] == "active"
    assert rows[f"{_PREFIX}-regen-old"] == {
        "key_prefix": f"{_PREFIX}-regen-old",
        "key_hash": "hash-regen-old",
        "status": "revoked",
        "notes": None,
        "metadata": None,
    }


@pytest.mark.parametrize("history", [[], ["revoked"], ["suspended"]])
async def test_regenerate_without_an_active_key_changes_nothing(keys, history):
    """A suspended or revoked key must not be handed a fresh credential."""
    store, pool = keys
    await _user(store, "u-admkey-noact")
    for index, status in enumerate(history):
        await _key(pool, "u-admkey-noact", f"noact{index}", status)
    before = await _rows(pool, "u-admkey-noact")

    with pytest.raises(ValueError, match="No active key"):
        await store.regenerate_key(
            "u-admkey-noact", new_key_hash="hash-noact-new", new_key_prefix=f"{_PREFIX}-noact-new"
        )

    assert await _rows(pool, "u-admkey-noact") == before
