"""Integration tests for the provider-key methods of the operational store (Postgres).

Beyond CRUD these carry access control: a key's ``min_role`` reserves upstream
capacity for a tier, and the pool is built from what these queries return. The
admin routes are tested with the store mocked, so this is where their SQL runs.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a clean PostgresOperationalStore for provider_api_keys tests."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM provider_api_keys")
            await conn.execute(
                "DELETE FROM provider_env_key_min_roles WHERE provider LIKE 'itest-%'"
            )
            await conn.execute(
                "DELETE FROM disabled_provider_env_keys WHERE provider LIKE 'itest-%'"
            )

    await _wipe()
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


async def test_provider_keys_round_trip(postgres_op_store: PostgresOperationalStore):
    """add → list → list_full → delete cycle behaves as documented."""
    raw_key = "sk-zai-integration-1234567890abcdef"
    key_id = await postgres_op_store.add_provider_key(
        provider="zai",
        api_key=raw_key,
        label="ops-test",
        created_by=None,
    )
    assert key_id

    rows = await postgres_op_store.list_provider_keys("zai")
    assert len(rows) == 1
    row = rows[0]
    assert row.id == key_id
    assert row.provider == "zai"
    assert row.label == "ops-test"
    assert row.status == "active"
    # key_prefix is masked — the raw secret must not appear.
    assert raw_key not in row.key_prefix

    raws = await postgres_op_store.list_provider_keys_full("zai")
    assert raws == [raw_key]

    deleted = await postgres_op_store.delete_provider_key(key_id)
    assert deleted is True

    assert await postgres_op_store.list_provider_keys("zai") == []
    assert await postgres_op_store.list_provider_keys_full("zai") == []
    assert await postgres_op_store.delete_provider_key(key_id) is False


async def _add(store: PostgresOperationalStore, api_key: str, **kwargs) -> str:
    return await store.add_provider_key(
        provider=kwargs.pop("provider", "itest-p"),
        api_key=api_key,
        label=None,
        created_by=None,
        **kwargs,
    )


async def test_a_shared_credential_takes_its_strictest_reservation(
    postgres_op_store: PostgresOperationalStore,
):
    """Two rows may hold the same raw key; the pool can enforce only one tier."""
    store = postgres_op_store
    await _add(store, "sk-shared", min_role="pro")
    await _add(store, "sk-shared", min_role="internal")
    await _add(store, "sk-shared", min_role="free")
    await _add(store, "sk-open")

    assert await store.list_provider_key_min_roles("itest-p") == {
        "sk-shared": "internal",
        "sk-open": "free",
    }


async def test_reservations_leave_out_disabled_and_excluded_rows(
    postgres_op_store: PostgresOperationalStore,
):
    store = postgres_op_store
    strict = await _add(store, "sk-shared", min_role="admin")
    await _add(store, "sk-shared", min_role="pro")
    disabled = await _add(store, "sk-disabled", min_role="internal")
    await _add(store, "sk-other-provider", provider="itest-q", min_role="admin")
    assert await store.set_provider_key_status(disabled, "disabled") is True

    # A disabled row neither serves nor reserves.
    assert await store.list_provider_key_min_roles("itest-p") == {"sk-shared": "admin"}
    assert await store.list_provider_keys_full("itest-p") == ["sk-shared", "sk-shared"]
    # Excluding the strict row (a reload replacing it) leaves the other's tier.
    assert await store.list_provider_key_min_roles("itest-p", exclude_ids={strict}) == {
        "sk-shared": "pro"
    }
    assert await store.list_provider_keys_full("itest-p", exclude_ids={strict}) == ["sk-shared"]
    # The admin list still shows the disabled row, so it can be re-enabled.
    statuses = {row.id: row.status for row in await store.list_provider_keys("itest-p")}
    assert statuses[disabled] == "disabled"


async def test_min_role_round_trips_and_refuses_an_unknown_tier(
    postgres_op_store: PostgresOperationalStore,
):
    """An unknown tier would reserve the key for nobody, so it is never written."""
    store = postgres_op_store
    key_id = await _add(store, "sk-tiered")

    assert await store.get_provider_key_min_role(key_id) == "free"
    assert await store.set_provider_key_min_role(key_id, "pro") is True
    assert await store.get_provider_key_min_role(key_id) == "pro"
    with pytest.raises(ValueError, match="min_role"):
        await store.set_provider_key_min_role(key_id, "enterprise")
    assert await store.get_provider_key_min_role(key_id) == "pro"

    assert await store.set_provider_key_min_role("no-such-id", "pro") is False
    assert await store.get_provider_key_min_role("no-such-id") is None


async def test_status_accepts_only_active_and_disabled(
    postgres_op_store: PostgresOperationalStore,
):
    store = postgres_op_store
    key_id = await _add(store, "sk-status")

    with pytest.raises(ValueError, match="status"):
        await store.set_provider_key_status(key_id, "revoked")
    assert await store.set_provider_key_status("no-such-id", "disabled") is False
    assert await store.set_provider_key_status(key_id, "disabled") is True
    assert await store.list_provider_keys_full("itest-p") == []
    assert await store.set_provider_key_status(key_id, "active") is True
    assert await store.list_provider_keys_full("itest-p") == ["sk-status"]


async def test_raw_values_are_read_by_row(postgres_op_store: PostgresOperationalStore):
    store = postgres_op_store
    first = await _add(store, "sk-raw-1")
    second = await _add(store, "sk-raw-2")
    await store.set_provider_key_status(second, "disabled")

    assert await store.get_provider_key_full(first) == ("itest-p", "sk-raw-1")
    assert await store.get_provider_key_full("no-such-id") is None
    # Every row, disabled included: the admin reveal works on either.
    assert await store.list_provider_key_values("itest-p") == {
        first: "sk-raw-1",
        second: "sk-raw-2",
    }


async def test_an_env_key_reservation_is_upserted_and_free_removes_it(
    postgres_op_store: PostgresOperationalStore,
):
    """Unreserved is the absence of a row, so the table lists only held-back keys."""
    store = postgres_op_store

    async def reserve(min_role: str, prefix: str = "sk-env…1") -> None:
        await store.set_provider_env_key_min_role(
            provider="itest-p",
            key_hash="h-env-1",
            key_prefix=prefix,
            min_role=min_role,
            updated_by="admin@example.com",
        )

    await reserve("pro")
    await reserve("internal", prefix="sk-env…2")
    assert await store.list_provider_env_key_reservations("itest-p") == [
        ("h-env-1", "sk-env…2", "internal")
    ]
    assert await store.list_provider_env_key_min_roles("itest-p") == {"h-env-1": "internal"}

    with pytest.raises(ValueError, match="min_role"):
        await reserve("enterprise")
    assert await store.list_provider_env_key_min_roles("itest-p") == {"h-env-1": "internal"}

    await reserve("free")
    assert await store.list_provider_env_key_reservations("itest-p") == []
    await reserve("free")  # clearing an absent reservation is not an error


async def test_an_env_key_tombstone_is_upserted_listed_newest_first_and_lifted(
    postgres_op_store: PostgresOperationalStore,
):
    store = postgres_op_store
    for key_hash, prefix in (("h-old", "sk-old"), ("h-new", "sk-new"), ("h-old", "sk-old2")):
        await store.disable_provider_env_key(
            provider="itest-p", key_hash=key_hash, key_prefix=prefix, disabled_by=None
        )
    await store.disable_provider_env_key(
        provider="itest-q", key_hash="h-old", key_prefix="sk-q", disabled_by=None
    )

    # Re-disabling h-old refreshed it: one row, newest prefix, now the latest.
    assert await store.list_disabled_provider_env_keys("itest-p") == [
        ("h-old", "sk-old2"),
        ("h-new", "sk-new"),
    ]
    assert await store.enable_provider_env_key("itest-p", "h-old") is True
    assert await store.enable_provider_env_key("itest-p", "h-old") is False
    assert await store.list_disabled_provider_env_key_hashes("itest-p") == {"h-new"}
    # Per provider: the same hash elsewhere stays disabled.
    assert await store.list_disabled_provider_env_key_hashes("itest-q") == {"h-old"}
