"""The admin API-key methods of ``PostgresOperationalStore`` against real SQL.

The admin routes are tested with these methods mocked, so until now none of
their SQL had run against PostgreSQL. What matters most is the history a user
accumulates: revoking a key and creating a new one leaves the revoked row
behind, and every method here must pick the live key out of that history and
never bring a revoked credential back.
"""

from __future__ import annotations

import json
from decimal import Decimal
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


# --- the account quota: one value on every unrevoked key of an account ------


async def _set_quota(pool: Any, name: str, daily: str | None, monthly: str | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET quota_daily_cost_usd = $2::numeric, "
            "quota_monthly_cost_usd = $3::numeric WHERE key_prefix = $1",
            f"{_PREFIX}-{name}",
            daily,
            monthly,
        )


async def _quotas(pool: Any, user_id: str) -> dict[str, tuple[Any, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_prefix, quota_daily_cost_usd, quota_monthly_cost_usd "
            "FROM api_keys WHERE user_id = $1",
            user_id,
        )
    return {
        row["key_prefix"].removeprefix(f"{_PREFIX}-"): (
            None if row["quota_daily_cost_usd"] is None else str(row["quota_daily_cost_usd"]),
            None if row["quota_monthly_cost_usd"] is None else str(row["quota_monthly_cost_usd"]),
        )
        for row in rows
    }


async def _create(store: PostgresOperationalStore, user_id: str, tag: str, **kwargs: Any) -> None:
    """Create key ``{_PREFIX}-{tag}`` through the store; *kwargs* go to ``create_key``."""
    await store.create_key(
        key_hash=f"hash-{tag}", key_prefix=f"{_PREFIX}-{tag}", user_id=user_id, **kwargs
    )


async def test_a_users_new_key_inherits_the_account_quota(keys):
    """A key a user adds takes the account's quota, not the role default it was offered."""
    store, pool = keys
    await _user(store, "u-admkey-inh")
    await _key(pool, "u-admkey-inh", "inh-old", "suspended")
    await _set_quota(pool, "inh-old", "42.0000", "500.0000")

    await _create(
        store, "u-admkey-inh", "inh-new", quota_daily_cost_usd=100.0, inherit_account_quota=True
    )

    assert (await _quotas(pool, "u-admkey-inh"))["inh-new"] == ("42.0000", "500.0000")


async def test_an_inherited_null_quota_stays_null(keys):
    """NULL is the default limit, and a sibling must read the same default -- not the offer."""
    store, pool = keys
    await _user(store, "u-admkey-inhnull")
    await _key(pool, "u-admkey-inhnull", "inhnull-old", "suspended")
    await _set_quota(pool, "inhnull-old", None)

    await _create(
        store,
        "u-admkey-inhnull",
        "inhnull-new",
        quota_daily_cost_usd=100.0,
        inherit_account_quota=True,
    )

    assert (await _quotas(pool, "u-admkey-inhnull"))["inhnull-new"] == (None, None)


async def test_a_first_key_takes_the_offered_quota(keys):
    """Nothing to inherit from: revoked history does not count as the account."""
    store, pool = keys
    await _user(store, "u-admkey-first")
    await _key(pool, "u-admkey-first", "first-old", "revoked")
    await _set_quota(pool, "first-old", "7.0000")

    await _create(
        store, "u-admkey-first", "first-new", quota_daily_cost_usd=100.0, inherit_account_quota=True
    )

    assert (await _quotas(pool, "u-admkey-first"))["first-new"] == ("100.0000", None)


async def test_an_admin_key_sets_the_account_quota_on_every_live_key(keys):
    """Without inheriting, the given quota becomes the account's; revoked rows keep theirs."""
    store, pool = keys
    await _user(store, "u-admkey-set")
    await _key(pool, "u-admkey-set", "set-susp", "suspended")
    await _key(pool, "u-admkey-set", "set-dead", "revoked")
    await _set_quota(pool, "set-susp", "5.0000")
    await _set_quota(pool, "set-dead", "6.0000")

    await _create(
        store,
        "u-admkey-set",
        "set-new",
        quota_daily_cost_usd=250.0,
        quota_monthly_cost_usd=900.0,
    )

    assert await _quotas(pool, "u-admkey-set") == {
        "set-new": ("250.0000", "900.0000"),
        "set-susp": ("250.0000", "900.0000"),
        "set-dead": ("6.0000", None),
    }


async def test_update_writes_the_quota_to_every_live_key(keys):
    store, pool = keys
    await _user(store, "u-admkey-updq")
    await _key(pool, "u-admkey-updq", "updq-live", "active")
    await _key(pool, "u-admkey-updq", "updq-susp", "suspended")
    await _key(pool, "u-admkey-updq", "updq-dead", "revoked")

    await store.update_key("u-admkey-updq", quota_daily_cost_usd=33)

    quotas = await _quotas(pool, "u-admkey-updq")
    assert quotas["updq-live"][0] == quotas["updq-susp"][0] == "33.0000"
    assert quotas["updq-dead"][0] == "1000.0000"


async def test_role_quota_reaches_suspended_keys_and_counts_what_it_writes(keys):
    """A suspended key that missed the bulk write would come back with a stale limit."""
    store, pool = keys
    async with pool.acquire() as conn:
        before_keys, before_users = await store.count_active_keys_for_role("pro")
        await _user(store, "u-admkey-role")
        await conn.execute("UPDATE users SET role = 'pro' WHERE id = 'u-admkey-role'")
    await _key(pool, "u-admkey-role", "role-live", "active")
    await _key(pool, "u-admkey-role", "role-susp", "suspended")
    await _key(pool, "u-admkey-role", "role-dead", "revoked")

    assert await store.count_active_keys_for_role("pro") == (before_keys + 2, before_users + 1)
    updated = await store.apply_role_quota("pro", quota=Decimal("77.00"))

    quotas = await _quotas(pool, "u-admkey-role")
    assert updated >= 2
    assert quotas["role-live"][0] == quotas["role-susp"][0] == "77.0000"
    assert quotas["role-dead"][0] == "1000.0000"


async def test_the_grant_quota_lookup_answers_with_one_row_per_account(keys):
    store, pool = keys
    await _user(store, "u-admkey-grant")
    await _key(pool, "u-admkey-grant", "grant-live", "active")
    await _set_quota(pool, "grant-live", "12.0000")

    rows = await store.get_quota_context_for_user("u-admkey-grant")

    assert [str(row["quota_daily_cost_usd"]) for row in rows] == ["12.0000"]


# --- several keys per account ------------------------------------------------


async def test_startup_drops_the_retired_one_key_indexes(keys):
    """An upgraded database still carries the indexes that held an account to one key."""
    store, pool = keys
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_unique "
            "ON api_keys(user_id) WHERE status = 'active'"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_active_unique "
            "ON api_keys(account_id) WHERE status = 'active' AND account_id IS NOT NULL"
        )

    await store.initialize()

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'api_keys' "
            "AND indexname = ANY($1::text[])",
            ["idx_api_keys_user_unique", "idx_api_keys_account_active_unique"],
        )
    assert remaining == 0


async def test_the_cap_counts_only_active_keys(keys):
    from serving.exceptions import APIKeyLimitReachedError

    store, pool = keys
    await _user(store, "u-admkey-cap")
    await _key(pool, "u-admkey-cap", "cap-a", "active")
    await _key(pool, "u-admkey-cap", "cap-dead", "revoked")

    await _create(store, "u-admkey-cap", "cap-b", max_active_keys=2)
    with pytest.raises(APIKeyLimitReachedError) as refused:
        await _create(store, "u-admkey-cap", "cap-c", max_active_keys=2)

    assert refused.value.limit == 2
    assert await store.count_active_keys("u-admkey-cap") == 2


async def test_listing_puts_live_keys_first_and_carries_names(keys):
    store, pool = keys
    await _user(store, "u-admkey-lst")
    await _key(pool, "u-admkey-lst", "lst-old", "active")
    await _key(pool, "u-admkey-lst", "lst-dead", "revoked")
    await _create(store, "u-admkey-lst", "lst-new", name="deploy")

    rows = await store.list_account_keys("u-admkey-lst")

    assert [(row["key_prefix"].removeprefix(f"{_PREFIX}-"), row["name"]) for row in rows] == [
        ("lst-new", "deploy"),
        ("lst-old", None),
        ("lst-dead", None),
    ]
    assert all("key_hash" not in row for row in rows)


async def test_rotation_replaces_one_key_and_keeps_its_settings(keys):
    store, pool = keys
    await _user(store, "u-admkey-rot")
    await _key(pool, "u-admkey-rot", "rot-other", "active")
    await _key(pool, "u-admkey-rot", "rot-old", "active", '{"team": "ml"}')
    await _set_quota(pool, "rot-old", "9.0000", "90.0000")
    await store.rename_key("u-admkey-rot", f"{_PREFIX}-rot-old", "eval")

    rotated = await store.rotate_key(
        "u-admkey-rot",
        f"{_PREFIX}-rot-old",
        new_key_hash="hash-rot-new",
        new_key_prefix=f"{_PREFIX}-rot-new",
        api_key_encrypted="cipher",
    )

    assert rotated is not None and rotated["old_key_prefix"] == f"{_PREFIX}-rot-old"
    rows = await _rows(pool, "u-admkey-rot")
    assert rows[f"{_PREFIX}-rot-old"]["status"] == "revoked"
    assert rows[f"{_PREFIX}-rot-other"]["status"] == "active"
    assert rows[f"{_PREFIX}-rot-new"]["status"] == "active"
    assert json.loads(rows[f"{_PREFIX}-rot-new"]["metadata"]) == {"team": "ml"}
    assert (await _quotas(pool, "u-admkey-rot"))["rot-new"] == ("9.0000", "90.0000")
    listed = {row["key_prefix"]: row for row in await store.list_account_keys("u-admkey-rot")}
    assert listed[f"{_PREFIX}-rot-new"]["name"] == "eval"


async def test_rotation_of_a_key_that_is_not_active_changes_nothing(keys):
    store, pool = keys
    await _user(store, "u-admkey-rotno")
    await _key(pool, "u-admkey-rotno", "rotno-dead", "revoked")
    before = await _rows(pool, "u-admkey-rotno")

    for prefix in (f"{_PREFIX}-rotno-dead", f"{_PREFIX}-missing"):
        assert (
            await store.rotate_key(
                "u-admkey-rotno",
                prefix,
                new_key_hash="hash-rotno-new",
                new_key_prefix=f"{_PREFIX}-rotno-new",
                api_key_encrypted=None,
            )
            is None
        )
    assert await _rows(pool, "u-admkey-rotno") == before


async def test_another_accounts_key_cannot_be_renamed_or_rotated(keys):
    store, pool = keys
    await _user(store, "u-admkey-own")
    await _user(store, "u-admkey-thief")
    await _key(pool, "u-admkey-own", "own", "active")

    assert not await store.rename_key("u-admkey-thief", f"{_PREFIX}-own", "mine")
    assert (
        await store.rotate_key(
            "u-admkey-thief",
            f"{_PREFIX}-own",
            new_key_hash="hash-own-new",
            new_key_prefix=f"{_PREFIX}-own-new",
            api_key_encrypted=None,
        )
        is None
    )
    assert (await _rows(pool, "u-admkey-own"))[f"{_PREFIX}-own"]["status"] == "active"


async def test_keys_read_unnamed_and_cannot_be_named_while_the_column_is_pending(keys):
    """The ``name`` migration may wait behind a lock; the key routes keep working meanwhile."""
    from serving.exceptions import AccountSchemaPending

    store, pool = keys
    await _user(store, "u-admkey-pend")
    await _key(pool, "u-admkey-pend", "pend-a", "active")
    # As the store reads a catalog without the column, rechecked no sooner than usual.
    store._api_key_names = False
    store._account_schema_read_at = float("inf")

    assert [row["name"] for row in await store.list_account_keys("u-admkey-pend")] == [None]
    with pytest.raises(AccountSchemaPending):
        await store.rename_key("u-admkey-pend", f"{_PREFIX}-pend-a", "x")
    with pytest.raises(AccountSchemaPending):
        await _create(store, "u-admkey-pend", "pend-named", name="x")
    await _create(store, "u-admkey-pend", "pend-b")
    assert await store.count_active_keys("u-admkey-pend") == 2
