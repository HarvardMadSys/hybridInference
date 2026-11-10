"""``provider_route_batch_policies`` against PostgreSQL: which routes batch work may use."""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.config.batch_routes import BatchRouteResolver
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    op_store = PostgresOperationalStore(pool)

    async def wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM provider_route_batch_policies WHERE model_id LIKE 'itest-%'"
            )

    await wipe()
    try:
        yield op_store
    finally:
        await wipe()
        await pool.close()


def _ours(rows: list[dict]) -> dict[tuple[str, str], bool]:
    return {
        (row["model_id"], row["route_id"]): row["batch_allowed"]
        for row in rows
        if row["model_id"].startswith("itest-")
    }


async def test_a_choice_is_stored_replaced_and_cleared(store):
    await store.set_provider_route_batch_policy(
        "itest-m", "r1", batch_allowed=True, updated_by="admin-1"
    )
    await store.set_provider_route_batch_policy(
        "itest-m", "r2", batch_allowed=False, updated_by="admin-1"
    )
    assert _ours(await store.list_all_provider_route_batch_policies()) == {
        ("itest-m", "r1"): True,
        ("itest-m", "r2"): False,
    }

    await store.set_provider_route_batch_policy(
        "itest-m", "r1", batch_allowed=False, updated_by="admin-2"
    )
    await store.set_provider_route_batch_policy(
        "itest-m", "r2", batch_allowed=None, updated_by="admin-2"
    )
    rows = await store.list_all_provider_route_batch_policies()
    assert _ours(rows) == {("itest-m", "r1"): False}
    (row,) = [r for r in rows if r["model_id"] == "itest-m"]
    assert row["updated_by"] == "admin-2"

    resolver = BatchRouteResolver(store)
    await resolver.load_all()
    assert resolver.choice("itest-m", "r1") is False
    assert resolver.choice("itest-m", "r2") is None


async def test_deleting_a_route_or_a_model_takes_its_choices_with_it(store):
    for route_id in ("r1", "r2"):
        await store.set_provider_route_batch_policy(
            "itest-gone", route_id, batch_allowed=True, updated_by=None
        )

    await store.delete_provider_route_candidate_with_config("itest-gone", "r1")
    assert _ours(await store.list_all_provider_route_batch_policies()) == {
        ("itest-gone", "r2"): True
    }

    await store.delete_runtime_model_state("itest-gone", ())
    assert _ours(await store.list_all_provider_route_batch_policies()) == {}
