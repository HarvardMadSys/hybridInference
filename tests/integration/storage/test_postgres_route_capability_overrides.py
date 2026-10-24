"""Integration tests for admin route capability overrides in the operational store (Postgres).

The admin routes are tested with the store faked, so this is where the
``provider_route_capabilities`` SQL runs: an override must come back exactly as
the sync reads it, and go away with the runtime model that owned it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.servers.routers.admin.provider_routes import _normalize_runtime_model_capabilities
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

WIDE = {
    "context_length": 131072,
    "max_output_length": 65536,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
}
NARROW = {
    "context_length": 32768,
    "max_output_length": 8192,
    "input_modalities": ["text"],
    "supports_tools": False,
}


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a PostgresOperationalStore with no itest capability overrides."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM provider_route_capabilities WHERE model_id LIKE 'itest-%'"
            )

    await _wipe()
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


def _capabilities(rows: list[dict]) -> dict[str, dict]:
    return {
        str(row["route_id"]): _normalize_runtime_model_capabilities(row["capabilities"])
        for row in rows
    }


async def test_override_round_trip_and_replace(postgres_op_store: PostgresOperationalStore):
    await postgres_op_store.upsert_route_capability_override(
        "itest-model", "itest-model:a-api", WIDE, "admin"
    )
    await postgres_op_store.upsert_route_capability_override(
        "itest-model", "itest-model:b-api", NARROW, "admin"
    )

    rows = await postgres_op_store.list_route_capability_overrides_for_model("itest-model")
    assert _capabilities(rows) == {"itest-model:a-api": WIDE, "itest-model:b-api": NARROW}
    assert [row["route_id"] for row in rows] == ["itest-model:a-api", "itest-model:b-api"]
    assert rows[0]["updated_by"] == "admin"

    await postgres_op_store.upsert_route_capability_override(
        "itest-model", "itest-model:a-api", NARROW, "other-admin"
    )
    rows = await postgres_op_store.list_route_capability_overrides_for_model("itest-model")
    assert _capabilities(rows)["itest-model:a-api"] == NARROW
    assert rows[0]["updated_by"] == "other-admin"


async def test_list_all_and_delete(postgres_op_store: PostgresOperationalStore):
    await postgres_op_store.upsert_route_capability_override("itest-b", "itest-b:x-api", WIDE, None)
    await postgres_op_store.upsert_route_capability_override(
        "itest-a", "itest-a:x-api", NARROW, None
    )

    listed = [
        (row["model_id"], row["route_id"])
        for row in await postgres_op_store.list_all_route_capability_overrides()
        if str(row["model_id"]).startswith("itest-")
    ]
    assert listed == [("itest-a", "itest-a:x-api"), ("itest-b", "itest-b:x-api")]

    assert await postgres_op_store.delete_route_capability_override("itest-a", "itest-a:x-api")
    assert not await postgres_op_store.delete_route_capability_override("itest-a", "itest-a:x-api")
    assert await postgres_op_store.list_route_capability_overrides_for_model("itest-a") == []


async def test_deleting_a_runtime_model_deletes_its_overrides(
    postgres_op_store: PostgresOperationalStore,
):
    await postgres_op_store.upsert_route_capability_override(
        "itest-gone", "itest-gone:x-api", WIDE, None
    )
    await postgres_op_store.upsert_route_capability_override(
        "itest-kept", "itest-kept:x-api", WIDE, None
    )

    await postgres_op_store.delete_runtime_model_state("itest-gone", ())

    assert await postgres_op_store.list_route_capability_overrides_for_model("itest-gone") == []
    assert len(await postgres_op_store.list_route_capability_overrides_for_model("itest-kept")) == 1
