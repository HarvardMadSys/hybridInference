"""Persist route ownership independently of the provider and resource type."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from serving.storage.cache import CachedOperationalStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_route_deployment_type_round_trip_and_legacy_defaults():
    pool = await postgres.create_pool(min_size=1, max_size=2)
    store = PostgresOperationalStore(pool)
    await store.initialize()
    store = CachedOperationalStore(store, MagicMock())
    model_id = f"deployment-type-{uuid4().hex}"
    try:
        await store.upsert_provider_route_config(
            model_id,
            "yaml-route",
            "engine",
            None,
            "http://10.0.0.5:8000/v1",
            None,
            "served-model",
            None,
            None,
            None,
            deployment_type="self_hosted",
        )
        await store.upsert_provider_route_candidate(
            model_id,
            "runtime-route",
            "on_demand",
            "engine",
            None,
            "http://10.0.0.5:8000/v1",
            None,
            "served-model",
            None,
            None,
            1.0,
            None,
            None,
            deployment_type="provider_api",
        )
        # A new store instance reads persisted declarations through both list APIs.
        reloaded = PostgresOperationalStore(pool)
        overrides = await reloaded.list_provider_route_configs_for_model(model_id)
        candidates = await reloaded.list_provider_route_candidates_for_model(model_id)
        assert overrides[0]["deployment_type"] == "self_hosted"
        assert candidates[0]["deployment_type"] == "provider_api"
        assert (
            next(
                r
                for r in await reloaded.list_all_provider_route_configs()
                if r["model_id"] == model_id
            )["deployment_type"]
            == "self_hosted"
        )
        assert (
            next(
                r
                for r in await reloaded.list_all_provider_route_candidates()
                if r["model_id"] == model_id
            )["deployment_type"]
            == "provider_api"
        )

        # Old writers/rows leave overrides unspecified, while new candidates are auto.
        await store.upsert_provider_route_config(
            model_id,
            "yaml-route",
            "engine",
            None,
            "http://10.0.0.5:8000/v1",
            None,
            "served-model",
            None,
            None,
            None,
        )
        await store.upsert_provider_route_candidate(
            model_id,
            "runtime-route",
            "on_demand",
            "engine",
            None,
            "http://10.0.0.5:8000/v1",
            None,
            "served-model",
            None,
            None,
            1.0,
            None,
            None,
        )
        assert (await store.list_provider_route_configs_for_model(model_id))[0][
            "deployment_type"
        ] is None
        assert (await store.list_provider_route_candidates_for_model(model_id))[0][
            "deployment_type"
        ] == "auto"
    finally:
        await store.delete_provider_route_config(model_id, "yaml-route")
        await store.delete_provider_route_candidate(model_id, "runtime-route")
        await pool.close()
