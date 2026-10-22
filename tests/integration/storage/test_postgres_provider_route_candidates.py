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
