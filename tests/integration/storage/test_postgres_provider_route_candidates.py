"""Integration tests for runtime route rows of the operational store (Postgres).

The admin routes are tested with the store mocked, so this is where the
``provider_route_candidates`` SQL runs: a runtime model's declared capabilities
must come back from the database exactly as the restore path reads them.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.servers.routers.admin.provider_routes import _normalize_runtime_model_capabilities
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

CAPABILITIES = {
    "context_length": 131072,
    "max_output_length": 65536,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
}
PRICING = {"prompt": "0.14", "completion": "0.28"}


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a PostgresOperationalStore with no itest runtime routes."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM provider_route_candidates WHERE model_id LIKE 'itest-%'"
            )
            await conn.execute(
                "DELETE FROM provider_weight_overrides WHERE model_id LIKE 'itest-%'"
            )
            await conn.execute("DELETE FROM provider_route_states WHERE model_id LIKE 'itest-%'")

    await _wipe()
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


async def _upsert(store: PostgresOperationalStore, model_id: str, capabilities) -> None:
    await store.upsert_provider_route_candidate(
        model_id,
        f"{model_id}:openrouter-api",
        "on_demand",
        "openrouter",
        None,
        "https://openrouter.ai/api/v1",
        None,
        "deepseek/deepseek-v4-flash",
        None,
        None,
        1.0,
        PRICING,
        capabilities,
        "itest",
    )


async def _row(store: PostgresOperationalStore, model_id: str) -> dict:
    (row,) = await store.list_provider_route_candidates_for_model(model_id)
    return row


async def test_declared_capabilities_round_trip(postgres_op_store: PostgresOperationalStore):
    await _upsert(postgres_op_store, "itest-declared", CAPABILITIES)

    row = await _row(postgres_op_store, "itest-declared")
    assert _normalize_runtime_model_capabilities(row["capabilities"]) == CAPABILITIES
    (listed,) = [
        candidate
        for candidate in await postgres_op_store.list_all_provider_route_candidates()
        if candidate["model_id"] == "itest-declared"
    ]
    assert _normalize_runtime_model_capabilities(listed["capabilities"]) == CAPABILITIES


async def test_undeclared_capabilities_stay_null(postgres_op_store: PostgresOperationalStore):
    await _upsert(postgres_op_store, "itest-legacy", None)

    row = await _row(postgres_op_store, "itest-legacy")
    assert row["capabilities"] is None
    assert _normalize_runtime_model_capabilities(row["capabilities"]) is None


async def test_rewriting_a_row_replaces_its_capabilities(
    postgres_op_store: PostgresOperationalStore,
):
    await _upsert(postgres_op_store, "itest-rewrite", CAPABILITIES)
    narrowed = {**CAPABILITIES, "input_modalities": ["text"], "supports_tools": False}

    await _upsert(postgres_op_store, "itest-rewrite", narrowed)

    row = await _row(postgres_op_store, "itest-rewrite")
    assert _normalize_runtime_model_capabilities(row["capabilities"]) == narrowed


async def test_initialize_adds_the_column_to_an_existing_table(
    postgres_op_store: PostgresOperationalStore,
):
    """A database created before the column existed gains it, keeping old rows NULL."""
    pool = postgres_op_store._pool
    try:
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE provider_route_candidates DROP COLUMN capabilities")
            await conn.execute(
                "INSERT INTO provider_route_candidates "
                "(model_id, route_id, route_type, provider, base_url, provider_model_id, pricing) "
                "VALUES ('itest-old', 'itest-old:openrouter-api', 'on_demand', 'openrouter', "
                "'https://openrouter.ai/api/v1', 'deepseek/deepseek-v4-flash', $1::jsonb)",
                '{"prompt": "0.14", "completion": "0.28"}',
            )

        await postgres_op_store.initialize()

        row = await _row(postgres_op_store, "itest-old")
        assert row["capabilities"] is None
    finally:
        # The worker's database outlives this test; a failure above must not
        # leave the table without the column for the tests that follow.
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE provider_route_candidates ADD COLUMN IF NOT EXISTS capabilities JSONB"
            )


async def test_a_write_without_a_declaration_keeps_the_stored_one(
    postgres_op_store: PostgresOperationalStore,
):
    """A NULL write (a route restored undeclared, then edited) never clears a declaration."""
    await _upsert(postgres_op_store, "itest-keep", CAPABILITIES)

    await _upsert(postgres_op_store, "itest-keep", None)

    row = await _row(postgres_op_store, "itest-keep")
    assert _normalize_runtime_model_capabilities(row["capabilities"]) == CAPABILITIES


async def _edit(
    store: PostgresOperationalStore,
    model_id: str,
    *,
    expected_updated_at,
    endpoint_id: str | None,
    previous_endpoint_id: str,
    provider: str = "openrouter[parasail]",
):
    return await store.update_provider_route_candidate(
        model_id,
        f"{model_id}:openrouter-api",
        expected_updated_at=expected_updated_at,
        provider=provider,
        openrouter_sort=None,
        base_url="https://openrouter.ai/api/v1",
        api_key_id="db-openrouter",
        provider_model_id="deepseek/deepseek-v4-pro",
        quota_limit=None,
        concurrency_limit=None,
        pricing=PRICING,
        deployment_type="provider_api",
        endpoint_id=endpoint_id,
        previous_endpoint_id=previous_endpoint_id,
        updated_by="itest-editor",
    )


async def test_edit_updates_the_row_in_place(postgres_op_store: PostgresOperationalStore):
    """An edit rewrites the target fields and keeps identity, weight and declaration."""
    await _upsert(postgres_op_store, "itest-edit", CAPABILITIES)
    before = await _row(postgres_op_store, "itest-edit")

    updated_at = await _edit(
        postgres_op_store,
        "itest-edit",
        expected_updated_at=before["updated_at"],
        endpoint_id="itest-edit:openrouter[parasail]-api",
        previous_endpoint_id="itest-edit:openrouter-api",
    )

    after = await _row(postgres_op_store, "itest-edit")
    assert updated_at == after["updated_at"]
    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]
    assert after["route_id"] == "itest-edit:openrouter-api"
    assert after["endpoint_id"] == "itest-edit:openrouter[parasail]-api"
    assert after["provider"] == "openrouter[parasail]"
    assert after["provider_model_id"] == "deepseek/deepseek-v4-pro"
    assert after["api_key_id"] == "db-openrouter"
    assert after["deployment_type"] == "provider_api"
    assert after["updated_by"] == "itest-editor"
    assert after["route_type"] == "on_demand"
    assert after["weight"] == 1.0
    assert _normalize_runtime_model_capabilities(after["capabilities"]) == CAPABILITIES


async def test_edit_from_stale_state_matches_nothing(postgres_op_store: PostgresOperationalStore):
    """A second edit that read the row before the first one landed changes nothing."""
    await _upsert(postgres_op_store, "itest-stale", None)
    before = await _row(postgres_op_store, "itest-stale")
    assert await _edit(
        postgres_op_store,
        "itest-stale",
        expected_updated_at=before["updated_at"],
        endpoint_id=None,
        previous_endpoint_id="itest-stale:openrouter-api",
    )
    first = await _row(postgres_op_store, "itest-stale")

    stale = await _edit(
        postgres_op_store,
        "itest-stale",
        expected_updated_at=before["updated_at"],
        endpoint_id=None,
        previous_endpoint_id="itest-stale:openrouter-api",
        provider="openrouter",
    )

    assert stale is None
    assert await _row(postgres_op_store, "itest-stale") == first


async def test_edit_of_a_missing_row_matches_nothing(postgres_op_store: PostgresOperationalStore):
    await _upsert(postgres_op_store, "itest-gone", None)
    before = await _row(postgres_op_store, "itest-gone")
    await postgres_op_store.delete_provider_route_candidate(
        "itest-gone", "itest-gone:openrouter-api"
    )

    assert (
        await _edit(
            postgres_op_store,
            "itest-gone",
            expected_updated_at=before["updated_at"],
            endpoint_id=None,
            previous_endpoint_id="itest-gone:openrouter-api",
        )
        is None
    )
    assert await postgres_op_store.list_provider_route_candidates_for_model("itest-gone") == []


async def _weights(store: PostgresOperationalStore, model_id: str) -> dict[str, float]:
    rows = await store.list_weight_overrides_for_model(model_id)
    return {row["endpoint_id"]: row["weight"] for row in rows}


async def test_moving_the_endpoint_carries_its_weight_override(
    postgres_op_store: PostgresOperationalStore,
):
    """The override follows the route; a leftover row at the new id is replaced, not kept."""
    await _upsert(postgres_op_store, "itest-move", None)
    before = await _row(postgres_op_store, "itest-move")
    await postgres_op_store.upsert_weight_override(
        "itest-move", "itest-move:openrouter-api", 3.0, "itest"
    )
    await postgres_op_store.upsert_weight_override(
        "itest-move", "itest-move:openrouter[parasail]-api", 9.0, "itest"
    )
    await postgres_op_store.upsert_weight_override("itest-move", "itest-move:other", 2.0, "itest")

    await _edit(
        postgres_op_store,
        "itest-move",
        expected_updated_at=before["updated_at"],
        endpoint_id="itest-move:openrouter[parasail]-api",
        previous_endpoint_id="itest-move:openrouter-api",
    )

    assert await _weights(postgres_op_store, "itest-move") == {
        "itest-move:openrouter[parasail]-api": 3.0,
        "itest-move:other": 2.0,
    }

    # Moving back (endpoint id = route id again) stores NULL and carries it back.
    moved = await _row(postgres_op_store, "itest-move")
    await _edit(
        postgres_op_store,
        "itest-move",
        expected_updated_at=moved["updated_at"],
        endpoint_id=None,
        previous_endpoint_id="itest-move:openrouter[parasail]-api",
        provider="openrouter",
    )

    assert (await _row(postgres_op_store, "itest-move"))["endpoint_id"] is None
    assert await _weights(postgres_op_store, "itest-move") == {
        "itest-move:openrouter-api": 3.0,
        "itest-move:other": 2.0,
    }


async def test_a_refused_edit_moves_no_weight(postgres_op_store: PostgresOperationalStore):
    await _upsert(postgres_op_store, "itest-keepweight", None)
    await postgres_op_store.upsert_weight_override(
        "itest-keepweight", "itest-keepweight:openrouter-api", 3.0, "itest"
    )
    before = await _row(postgres_op_store, "itest-keepweight")
    stale = before["updated_at"].replace(year=2001)

    assert (
        await _edit(
            postgres_op_store,
            "itest-keepweight",
            expected_updated_at=stale,
            endpoint_id="itest-keepweight:openrouter[parasail]-api",
            previous_endpoint_id="itest-keepweight:openrouter-api",
        )
        is None
    )
    assert await _weights(postgres_op_store, "itest-keepweight") == {
        "itest-keepweight:openrouter-api": 3.0
    }


async def test_upsert_round_trips_a_moved_endpoint_id(postgres_op_store: PostgresOperationalStore):
    """Restoring a deleted row (the delete rollback) puts its moved endpoint id back."""
    await postgres_op_store.upsert_provider_route_candidate(
        "itest-restore",
        "itest-restore:openrouter-api",
        "on_demand",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        None,
        "deepseek/deepseek-v4-flash",
        None,
        None,
        1.0,
        PRICING,
        None,
        "itest",
        endpoint_id="itest-restore:openrouter[parasail]-api",
    )

    row = await _row(postgres_op_store, "itest-restore")
    assert row["endpoint_id"] == "itest-restore:openrouter[parasail]-api"
    (listed,) = [
        candidate
        for candidate in await postgres_op_store.list_all_provider_route_candidates()
        if candidate["model_id"] == "itest-restore"
    ]
    assert listed["endpoint_id"] == "itest-restore:openrouter[parasail]-api"


async def test_initialize_adds_the_endpoint_id_column(
    postgres_op_store: PostgresOperationalStore,
):
    """A database from before edits gains the column, and its rows read as unmoved."""
    pool = postgres_op_store._pool
    try:
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE provider_route_candidates DROP COLUMN endpoint_id")
            await conn.execute(
                "INSERT INTO provider_route_candidates "
                "(model_id, route_id, route_type, provider, base_url, provider_model_id) "
                "VALUES ('itest-oldep', 'itest-oldep:openrouter-api', 'on_demand', "
                "'openrouter', 'https://openrouter.ai/api/v1', 'deepseek/deepseek-v4-flash')"
            )

        await postgres_op_store.initialize()

        assert (await _row(postgres_op_store, "itest-oldep"))["endpoint_id"] is None
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE provider_route_candidates ADD COLUMN IF NOT EXISTS endpoint_id TEXT"
            )


async def _states(store: PostgresOperationalStore, model_id: str) -> dict[str, tuple[bool, bool]]:
    return {
        row["route_id"]: (row["disabled"], row["removed"])
        for row in await store.list_all_provider_route_states()
        if row["model_id"] == model_id
    }


async def test_route_state_round_trip(postgres_op_store: PostgresOperationalStore):
    """Disable, remove, restore and enable; a route with neither flag has no row."""
    store = postgres_op_store
    await store.set_provider_route_state(
        "itest-state", "a", disabled=True, removed=False, updated_by="x"
    )
    await store.set_provider_route_state(
        "itest-state", "b", disabled=False, removed=True, updated_by="x"
    )
    assert await _states(store, "itest-state") == {"a": (True, False), "b": (False, True)}

    await store.set_provider_route_state(
        "itest-state", "a", disabled=True, removed=True, updated_by="y"
    )
    assert (await _states(store, "itest-state"))["a"] == (True, True)

    await store.set_provider_route_state(
        "itest-state", "a", disabled=False, removed=False, updated_by="z"
    )
    await store.set_provider_route_state(
        "itest-state", "b", disabled=False, removed=False, updated_by="z"
    )
    assert await _states(store, "itest-state") == {}


async def test_deleting_a_runtime_route_drops_its_state(
    postgres_op_store: PostgresOperationalStore,
):
    store = postgres_op_store
    await _upsert(store, "itest-drop", None)
    route_id = "itest-drop:openrouter-api"
    await store.set_provider_route_state(
        "itest-drop", route_id, disabled=True, removed=False, updated_by="x"
    )
    await store.set_provider_route_state(
        "itest-drop", "other", disabled=True, removed=False, updated_by="x"
    )

    assert await store.delete_provider_route_candidate_with_config("itest-drop", route_id)

    assert await _states(store, "itest-drop") == {"other": (True, False)}


async def test_deleting_a_runtime_model_drops_every_route_state(
    postgres_op_store: PostgresOperationalStore,
):
    store = postgres_op_store
    await _upsert(store, "itest-model", None)
    await store.set_provider_route_state(
        "itest-model", "a", disabled=True, removed=False, updated_by="x"
    )

    await store.delete_runtime_model_state("itest-model", ())

    assert await _states(store, "itest-model") == {}
