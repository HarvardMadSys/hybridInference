"""Tests for the admin queue-offload route endpoints (``/admin/routing/offload-routes``)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.offload import OffloadPolicy
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.upstream_limiter import UpstreamConcurrencyLimiter, reset_upstream_limiter
from serving.config.offload_routes import OffloadRouteResolver, encode_offload_policy
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import offload_routes

AUTH = {"Authorization": "Bearer test-admin"}
MODEL = "glm-4.7"
PRIMARY = f"{MODEL}:primary-api"
SIBLING = f"{MODEL}:sibling-api"
RESERVED_ROUTE_ID = "reserved-route"
RESERVED_ENDPOINT = f"{MODEL}:reserved-api"


class _StubAdapter(BaseAdapter):
    """Route entry that is never dispatched: these tests only read its config."""

    def __init__(
        self,
        provider: str,
        *,
        model_id: str = MODEL,
        route_id: str | None = None,
        runtime: bool = False,
    ) -> None:
        metadata: dict[str, Any] = {}
        if route_id:
            metadata["route_id"] = route_id
        if runtime:
            metadata["runtime_candidate"] = True
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider=provider,
                base_url=f"https://api.{provider}.example/v1",
                endpoint_id=f"{model_id}:{provider}-api",
                route_metadata=metadata,
            )
        )

    async def chat_completion(self, messages, **params):  # pragma: no cover - never dispatched
        raise AssertionError("not dispatched in admin tests")

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("not dispatched in admin tests")
        yield


class _Registry:
    """Just enough of ``ModelRouterRegistry`` for the offload endpoints."""

    def __init__(self, shared: FixedRouter) -> None:
        self.shared = shared
        self.strategies: dict[str, str] = {}
        self.routers: dict[str, Any] = {}

    def get_router_name(self, model_id: str) -> str:
        return self.strategies.get(model_id, "fixed")

    def get_router(self, model_id: str) -> Any:
        return self.routers.get(model_id, self.shared)


@pytest.fixture
def limiter():
    installed = UpstreamConcurrencyLimiter(acquire_timeout=30.0)
    reset_upstream_limiter(installed)
    yield installed
    reset_upstream_limiter()


@pytest.fixture
async def offload_client(monkeypatch, limiter):
    op_store = MagicMock()
    op_store.list_settings = AsyncMock(return_value=[])
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()
    op_store.delete_setting = AsyncMock(return_value=True)
    op_store.get_user_by_id = AsyncMock(return_value=None)

    router = FixedRouter()
    router.register_route(
        MODEL,
        [
            (_StubAdapter("primary"), 1.0),
            (_StubAdapter("sibling"), 1.0),
            (_StubAdapter("reserved", route_id=RESERVED_ROUTE_ID, runtime=True), 1.0),
        ],
    )
    router.register_route("solo", [(_StubAdapter("solo", model_id="solo"), 1.0)])
    resolver = OffloadRouteResolver(op_store)
    router.offload_policy_resolver = resolver
    registry = _Registry(router)

    services = AppServices(
        router=router,
        model_router_registry=registry,  # type: ignore[arg-type]
        operational_store=op_store,
        offload_route_resolver=resolver,
    )
    app = FastAPI()
    app.state.services = services
    app.include_router(admin_router.router)

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    audit = AsyncMock()
    monkeypatch.setattr(offload_routes, "log_admin_action", audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.services = services  # type: ignore[attr-defined]
        yield client, op_store, router, registry, resolver, audit


async def _set(client: AsyncClient, route_id: str = RESERVED_ROUTE_ID, wait: float = 5.0):
    return await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": route_id, "wait_seconds": wait},
        headers=AUTH,
    )


# ------------------------------------------------------------------- listing


async def test_list_reports_the_queue_and_no_routes_by_default(offload_client):
    client, *_ = offload_client

    response = await client.get("/admin/routing/offload-routes", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "queue_enabled": True,
        "max_wait_seconds": 30.0,
        "offload_routes": [],
    }


async def test_list_reports_a_disabled_queue(offload_client):
    client, *_ = offload_client
    reset_upstream_limiter(UpstreamConcurrencyLimiter(enabled=False, acquire_timeout=12.0))

    response = await client.get("/admin/routing/offload-routes", headers=AUTH)

    assert response.json()["queue_enabled"] is False
    assert response.json()["max_wait_seconds"] == 12.0


async def test_requests_without_admin_auth_are_refused(offload_client):
    client, *_ = offload_client

    response = await client.get("/admin/routing/offload-routes")

    assert response.status_code == 401


async def test_get_one_model_without_a_policy(offload_client):
    client, *_ = offload_client

    response = await client.get(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"model_id": MODEL, "offload": None}


async def test_get_an_unknown_model_without_a_policy_is_404(offload_client):
    client, *_ = offload_client

    response = await client.get("/admin/routing/offload-routes/nope", headers=AUTH)

    assert response.status_code == 404


# ------------------------------------------------------------------- setting


async def test_put_stores_the_policy_and_routing_applies_it_at_once(offload_client):
    client, op_store, router, _registry, resolver, audit = offload_client

    response = await _set(client, wait=4.5)

    assert response.status_code == 200, response.text
    offload = response.json()["offload"]
    assert offload["route_id"] == RESERVED_ROUTE_ID
    assert offload["endpoint_id"] == RESERVED_ENDPOINT
    assert offload["wait_seconds"] == 4.5
    assert offload["active"] is True
    assert offload["inactive_reason"] is None

    key, value, value_type, updated_by = op_store.set_setting.await_args.args
    # The snapshot carries the same author the row was written with.
    assert offload["updated_by"] == updated_by
    assert key == f"model_offload_route:{MODEL}"
    assert json.loads(value) == {"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.5}
    assert value_type == "json"
    assert resolver.get_offload_policy(MODEL) == OffloadPolicy(RESERVED_ROUTE_ID, 4.5)
    # The reserved route has left ordinary selection.
    picks = {router.select_adapter(MODEL).config.provider for _ in range(200)}
    assert picks <= {"primary", "sibling"}

    action = audit.await_args.args[2]
    details = audit.await_args.args[4]
    assert action == "routing.offload_routes.update"
    assert details["route_id"] == RESERVED_ROUTE_ID
    assert details["old_route_id"] is None


async def test_put_replaces_an_existing_policy_and_audits_the_old_one(offload_client):
    client, _op_store, _router, _registry, _resolver, audit = offload_client
    await _set(client, route_id=RESERVED_ROUTE_ID, wait=4.0)

    response = await _set(client, route_id=SIBLING, wait=2.0)

    assert response.status_code == 200
    assert response.json()["offload"]["route_id"] == SIBLING
    details = audit.await_args.args[4]
    assert details["old_route_id"] == RESERVED_ROUTE_ID
    assert details["old_wait_seconds"] == 4.0


async def test_put_accepts_an_untouched_yaml_route_by_its_endpoint_id(offload_client):
    client, *_ = offload_client

    response = await _set(client, route_id=PRIMARY)

    assert response.status_code == 200
    assert response.json()["offload"]["endpoint_id"] == PRIMARY


async def test_put_for_an_unknown_model_is_404(offload_client):
    client, op_store, *_ = offload_client

    response = await client.put(
        "/admin/routing/offload-routes/nope",
        json={"route_id": "x", "wait_seconds": 1.0},
        headers=AUTH,
    )

    assert response.status_code == 404
    op_store.set_setting.assert_not_awaited()


async def test_put_for_an_unknown_route_is_400(offload_client):
    client, op_store, *_ = offload_client

    response = await _set(client, route_id="no-such-route")

    assert response.status_code == 400
    op_store.set_setting.assert_not_awaited()


@pytest.mark.parametrize("wait", [0, -1])
async def test_put_rejects_a_wait_routing_could_not_honor(offload_client, wait):
    client, op_store, *_ = offload_client

    response = await _set(client, wait=wait)

    assert response.status_code == 422
    op_store.set_setting.assert_not_awaited()


async def test_put_accepts_a_wait_past_the_acquire_timeout(offload_client):
    """The queue still ends its wait at 30s; the rest bounds the engine's first token."""
    client, _op_store, _router, _registry, resolver, _audit = offload_client

    response = await _set(client, wait=90.0)

    assert response.status_code == 200
    assert response.json()["offload"]["wait_seconds"] == 90.0
    assert resolver.get_offload_policy(MODEL).wait_seconds == 90.0


async def test_put_stores_an_engine_queue_limit_and_routing_applies_it(offload_client):
    client, op_store, _router, _registry, resolver, audit = offload_client

    response = await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "engine_queue_limit": 3},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["offload"]["engine_queue_limit"] == 3
    _key, value, _value_type, _updated_by = op_store.set_setting.await_args.args
    assert json.loads(value) == {
        "engine_queue_limit": 3,
        "route_id": RESERVED_ROUTE_ID,
        "wait_seconds": 4.0,
    }
    assert resolver.get_offload_policy(MODEL) == OffloadPolicy(RESERVED_ROUTE_ID, 4.0, 3)
    details = audit.await_args.args[4]
    assert details["engine_queue_limit"] == 3
    assert details["old_engine_queue_limit"] is None


async def test_put_without_an_engine_queue_limit_clears_it(offload_client):
    """The policy is replaced whole, so leaving the limit out turns the hold off."""
    client, op_store, _router, _registry, resolver, audit = offload_client
    await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "engine_queue_limit": 3},
        headers=AUTH,
    )

    response = await _set(client, wait=4.0)

    assert response.json()["offload"]["engine_queue_limit"] is None
    _key, value, _value_type, _updated_by = op_store.set_setting.await_args.args
    assert "engine_queue_limit" not in json.loads(value)
    assert resolver.get_offload_policy(MODEL).engine_queue_limit is None
    assert audit.await_args.args[4]["old_engine_queue_limit"] == 3


@pytest.mark.parametrize("limit", [0, -2, 1.5, True, "3"])
async def test_put_rejects_an_engine_queue_limit_that_is_not_a_whole_number_of_one_or_more(
    offload_client, limit
):
    client, op_store, *_ = offload_client

    response = await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "engine_queue_limit": limit},
        headers=AUTH,
    )

    assert response.status_code == 422
    op_store.set_setting.assert_not_awaited()


async def test_put_stores_a_max_input_and_routing_applies_it(offload_client):
    client, op_store, _router, _registry, resolver, audit = offload_client

    response = await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "max_input_tokens": 32000},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    offload = response.json()["offload"]
    assert offload["max_input_tokens"] == 32000
    assert offload["engine_queue_limit"] is None
    _key, value, _value_type, _updated_by = op_store.set_setting.await_args.args
    assert json.loads(value) == {
        "max_input_tokens": 32000,
        "route_id": RESERVED_ROUTE_ID,
        "wait_seconds": 4.0,
    }
    assert resolver.get_offload_policy(MODEL) == OffloadPolicy(
        RESERVED_ROUTE_ID, 4.0, max_input_tokens=32000
    )
    details = audit.await_args.args[4]
    assert details["max_input_tokens"] == 32000
    assert details["old_max_input_tokens"] is None

    listed = await client.get("/admin/routing/offload-routes", headers=AUTH)
    assert listed.json()["offload_routes"][0]["max_input_tokens"] == 32000


async def test_put_without_a_max_input_clears_it(offload_client):
    """The policy is replaced whole, so leaving the max input out lifts it."""
    client, op_store, _router, _registry, resolver, audit = offload_client
    await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "max_input_tokens": 8000},
        headers=AUTH,
    )

    response = await _set(client, wait=4.0)

    assert response.json()["offload"]["max_input_tokens"] is None
    _key, value, _value_type, _updated_by = op_store.set_setting.await_args.args
    assert "max_input_tokens" not in json.loads(value)
    assert resolver.get_offload_policy(MODEL).max_input_tokens is None
    assert audit.await_args.args[4]["old_max_input_tokens"] == 8000


@pytest.mark.parametrize("max_input", [0, -2, 1.5, True, "3"])
async def test_put_rejects_a_max_input_that_is_not_a_whole_number_of_one_or_more(
    offload_client, max_input
):
    client, op_store, *_ = offload_client

    response = await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "max_input_tokens": max_input},
        headers=AUTH,
    )

    assert response.status_code == 422
    op_store.set_setting.assert_not_awaited()


async def test_put_on_a_routewise_model_is_refused(offload_client):
    client, op_store, _router, registry, *_ = offload_client
    registry.strategies[MODEL] = "routewise"

    response = await _set(client)

    assert response.status_code == 422
    assert "fixed routing" in response.json()["detail"]
    op_store.set_setting.assert_not_awaited()


async def test_put_on_a_hybrid_composition_model_is_refused(offload_client):
    client, op_store, _router, registry, *_ = offload_client
    registry.routers[MODEL] = object()

    response = await _set(client)

    assert response.status_code == 422
    assert "hybrid composition" in response.json()["detail"]
    op_store.set_setting.assert_not_awaited()


async def test_put_on_a_single_route_model_is_refused(offload_client):
    client, op_store, *_ = offload_client

    response = await client.put(
        "/admin/routing/offload-routes/solo",
        json={"route_id": "solo:solo-api", "wait_seconds": 1.0},
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "at least one other route" in response.json()["detail"]
    op_store.set_setting.assert_not_awaited()


async def test_a_failed_write_routes_without_the_policy(offload_client):
    client, op_store, _router, _registry, resolver, _audit = offload_client
    await _set(client, wait=3.0)
    op_store.set_setting.side_effect = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        await _set(client, route_id=SIBLING, wait=2.0)

    # The write may or may not have committed; route on neither guess until
    # the next reload reads the durable value.
    assert resolver.get_offload_policy(MODEL) is None


# ------------------------------------------------------------------ clearing


async def test_delete_clears_the_policy_and_is_idempotent(offload_client):
    client, op_store, _router, _registry, resolver, audit = offload_client
    await _set(client)
    audit.reset_mock()

    response = await client.delete(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"model_id": MODEL, "offload": None}
    op_store.delete_setting.assert_awaited_with(f"model_offload_route:{MODEL}")
    assert resolver.get_offload_policy(MODEL) is None
    assert audit.await_args.args[2] == "routing.offload_routes.clear"

    audit.reset_mock()
    op_store.delete_setting.return_value = False
    again = await client.delete(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)
    assert again.status_code == 200
    audit.assert_not_awaited()


async def test_delete_audits_the_engine_queue_limit_it_cleared(offload_client):
    client, _op_store, _router, _registry, _resolver, audit = offload_client
    await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "engine_queue_limit": 2},
        headers=AUTH,
    )

    response = await client.delete(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    assert response.status_code == 200
    assert audit.await_args.args[2] == "routing.offload_routes.clear"
    assert audit.await_args.args[4]["old_engine_queue_limit"] == 2


async def test_delete_audits_the_max_input_it_cleared(offload_client):
    client, _op_store, _router, _registry, _resolver, audit = offload_client
    await client.put(
        f"/admin/routing/offload-routes/{MODEL}",
        json={"route_id": RESERVED_ROUTE_ID, "wait_seconds": 4.0, "max_input_tokens": 16000},
        headers=AUTH,
    )

    await client.delete(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    assert audit.await_args.args[2] == "routing.offload_routes.clear"
    assert audit.await_args.args[4]["old_max_input_tokens"] == 16000


async def test_delete_works_for_a_model_that_no_longer_exists(offload_client):
    client, op_store, _router, _registry, resolver, _audit = offload_client
    resolver.set_policy("retired-model", OffloadPolicy("r", 2.0))

    response = await client.delete("/admin/routing/offload-routes/retired-model", headers=AUTH)

    assert response.status_code == 200
    op_store.delete_setting.assert_awaited_with("model_offload_route:retired-model")
    assert resolver.get_offload_policy("retired-model") is None


# ------------------------------------------------------------ inactive states


async def test_a_policy_for_a_removed_model_is_listed_as_inactive(offload_client):
    client, _op_store, _router, _registry, resolver, _audit = offload_client
    resolver.set_policy("retired-model", OffloadPolicy("r", 2.0))

    response = await client.get("/admin/routing/offload-routes/retired-model", headers=AUTH)

    offload = response.json()["offload"]
    assert offload["active"] is False
    assert offload["inactive_reason"] == "The model no longer exists"


async def test_a_policy_naming_a_removed_route_is_inactive(offload_client):
    client, _op_store, _router, _registry, resolver, _audit = offload_client
    resolver.set_policy(MODEL, OffloadPolicy("gone-route", 2.0))

    response = await client.get("/admin/routing/offload-routes", headers=AUTH)

    (offload,) = response.json()["offload_routes"]
    assert offload["active"] is False
    assert offload["inactive_reason"] == "The route no longer exists"
    assert offload["endpoint_id"] is None


async def test_a_policy_on_a_model_switched_to_routewise_is_inactive(offload_client):
    client, _op_store, _router, registry, resolver, _audit = offload_client
    resolver.set_policy(MODEL, OffloadPolicy(RESERVED_ROUTE_ID, 2.0))
    registry.strategies[MODEL] = "routewise"

    response = await client.get(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    offload = response.json()["offload"]
    assert offload["active"] is False
    assert "routewise" in offload["inactive_reason"]


async def test_a_route_weighted_to_zero_is_inactive(offload_client):
    client, _op_store, _router, _registry, resolver, _audit = offload_client
    resolver.set_policy(MODEL, OffloadPolicy(RESERVED_ROUTE_ID, 2.0))
    weights = MagicMock()
    weights.get_snapshot_for_model.return_value = {RESERVED_ENDPOINT: 0.0}
    client.services.weight_override_resolver = weights  # type: ignore[attr-defined]

    response = await client.get(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    offload = response.json()["offload"]
    assert offload["active"] is False
    assert "effective weight is 0" in offload["inactive_reason"]


async def test_a_route_whose_provider_is_disabled_is_inactive(offload_client):
    client, _op_store, _router, _registry, resolver, _audit = offload_client
    resolver.set_policy(MODEL, OffloadPolicy(RESERVED_ROUTE_ID, 2.0))
    disabled = MagicMock()
    disabled.is_disabled.side_effect = lambda provider: provider == "reserved"
    client.services.disabled_provider_resolver = disabled  # type: ignore[attr-defined]

    response = await client.get(f"/admin/routing/offload-routes/{MODEL}", headers=AUTH)

    assert response.json()["offload"]["active"] is False


# ------------------------------------------- interplay with provider routes


async def test_switching_to_routewise_is_refused_while_an_offload_route_is_set(offload_client):
    client, *_ = offload_client
    await _set(client)

    response = await client.patch(
        f"/admin/routing/provider-route-strategies/{MODEL}",
        json={"strategy": "routewise"},
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "clear the model's offload route" in response.json()["detail"]


async def test_deleting_the_offload_route_is_refused(offload_client):
    client, _op_store, router, *_ = offload_client
    await _set(client)

    response = await client.delete(
        f"/admin/routing/provider-route-candidates/{MODEL}/{RESERVED_ROUTE_ID}",
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "offload route" in response.json()["detail"]
    assert len(router.routes[MODEL].raw_adapters) == 3


# ------------------------------------------------------------ the resolver


async def test_resolver_loads_valid_rows_and_skips_the_rest():
    store = MagicMock()
    store.list_settings = AsyncMock(
        return_value=[
            {
                "key": f"model_offload_route:{MODEL}",
                "value": encode_offload_policy(OffloadPolicy(RESERVED_ROUTE_ID, 3.0)),
                "updated_by": "admin@example.com",
            },
            {"key": "model_offload_route:broken", "value": "{not json"},
            {
                "key": "model_offload_route:negative",
                "value": '{"route_id": "r", "wait_seconds": -1}',
            },
            {"key": "model_router_strategy:other", "value": "fixed"},
        ]
    )
    resolver = OffloadRouteResolver(store)

    assert await resolver.load_all() is True
    assert await resolver.load_all() is False

    assert set(resolver.list_records()) == {MODEL}
    record = resolver.get_record(MODEL)
    assert record is not None
    assert record.policy == OffloadPolicy(RESERVED_ROUTE_ID, 3.0)
    assert record.updated_by == "admin@example.com"


async def test_resolver_reads_an_engine_queue_limit_and_skips_a_bad_one():
    store = MagicMock()
    store.list_settings = AsyncMock(
        return_value=[
            {
                "key": f"model_offload_route:{MODEL}",
                "value": encode_offload_policy(OffloadPolicy(RESERVED_ROUTE_ID, 3.0, 2)),
            },
            {
                "key": "model_offload_route:before-the-limit",
                "value": '{"route_id": "r", "wait_seconds": 1}',
            },
            {
                "key": "model_offload_route:zero",
                "value": '{"route_id": "r", "wait_seconds": 1, "engine_queue_limit": 0}',
            },
        ]
    )
    resolver = OffloadRouteResolver(store)

    await resolver.load_all()

    assert resolver.get_offload_policy(MODEL) == OffloadPolicy(RESERVED_ROUTE_ID, 3.0, 2)
    assert resolver.get_offload_policy("before-the-limit") == OffloadPolicy("r", 1.0)
    assert resolver.get_offload_policy("zero") is None


async def test_resolver_reads_a_max_input_and_skips_a_bad_one():
    both = OffloadPolicy(RESERVED_ROUTE_ID, 3.0, engine_queue_limit=2, max_input_tokens=64000)
    store = MagicMock()
    store.list_settings = AsyncMock(
        return_value=[
            {"key": f"model_offload_route:{MODEL}", "value": encode_offload_policy(both)},
            {
                "key": "model_offload_route:zero",
                "value": '{"route_id": "r", "wait_seconds": 1, "max_input_tokens": 0}',
            },
            {
                "key": "model_offload_route:fraction",
                "value": '{"route_id": "r", "wait_seconds": 1, "max_input_tokens": 1.5}',
            },
        ]
    )
    resolver = OffloadRouteResolver(store)

    await resolver.load_all()

    assert resolver.get_offload_policy(MODEL) == both
    assert resolver.get_offload_policy("zero") is None
    assert resolver.get_offload_policy("fraction") is None


async def test_a_reload_that_started_before_an_admin_write_cannot_undo_it():
    store = MagicMock()
    resolver = OffloadRouteResolver(store)

    async def slow_list_settings():
        # An admin write lands while this read is in flight, and the read
        # still returns the row as it was before that write.
        resolver.set_policy(MODEL, OffloadPolicy("new", 1.0))
        return [
            {
                "key": f"model_offload_route:{MODEL}",
                "value": encode_offload_policy(OffloadPolicy("old", 9.0)),
            }
        ]

    store.list_settings = slow_list_settings

    assert await resolver.load_all() is False
    assert resolver.get_offload_policy(MODEL) == OffloadPolicy("new", 1.0)
