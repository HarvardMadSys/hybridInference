"""Which routes batch work may use (``serving/config/batch_routes.py``).

By default a batch runs only on the gateway's own servers; an administrator can
allow or forbid any route. A batch request is narrowed to its model's
batch-allowed routes through the routers' per-request range, never touching
interactive traffic or the owner's affinity pin; a model with none refuses the
batch at submission, and a queued item fails with the same reason.
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.batch import targets
from serving.batch.dispatch import dispatch_item
from serving.config.batch_routes import (
    BATCH_ROUTE_UNAVAILABLE,
    BatchRouteResolver,
    batch_embedding_adapter,
    batch_endpoint_scope,
    batch_route_available,
    batch_route_refusal,
    default_batch_allowed,
)
from serving.servers.deps import AppServices
from serving.servers.embedding_fallback import FallbackEmbeddingAdapter
from serving.utils import context as req_ctx

MODEL = "mixed-model"
PRICING = {"prompt": "1.00", "completion": "2.00"}


LOCAL_ID = "mixed-model:local-8010"
CLOUD_ID = "mixed-model:cloud-api"


def _config(
    name: str, base_url: str, deployment_type: str = "auto", *, endpoint_id: str | None = None
) -> ModelConfig:
    return ModelConfig(
        id=MODEL,
        name=name,
        provider=name,
        base_url=base_url,
        deployment_type=deployment_type,
        endpoint_id=endpoint_id,
        pricing=PRICING,
    )


class _Route(BaseAdapter):
    """An upstream that answers every chat request and records it."""

    def __init__(
        self,
        name: str,
        base_url: str,
        deployment_type: str = "auto",
        *,
        endpoint_id: str | None = None,
    ) -> None:
        super().__init__(_config(name, base_url, deployment_type, endpoint_id=endpoint_id))
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls += 1
        return self.format_response(content=self.config.name, model=MODEL)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("a batch item must never stream")
        yield


def _local() -> _Route:
    return _Route("local", "http://host.docker.internal:8010", endpoint_id=LOCAL_ID)


def _cloud() -> _Route:
    return _Route("cloud", "https://cloud.example.test/v1", endpoint_id=CLOUD_ID)


def _router(*adapters: tuple[BaseAdapter, float], resolver: Any = None) -> FixedRouter:
    router = FixedRouter()
    router.register_route(MODEL, list(adapters), aliases=["mixed-alias"])
    if resolver is not None:
        router.batch_route_resolver = resolver
    return router


def _resolver(choices: dict[tuple[str, str], bool] | None = None) -> BatchRouteResolver:
    resolver = BatchRouteResolver(store=None)
    for (model_id, route_id), allowed in (choices or {}).items():
        resolver.set_choice(model_id, route_id, allowed)
    return resolver


# -- the default: the gateway's own servers ----------------------------------------


@pytest.mark.parametrize(
    ("base_url", "deployment_type", "allowed"),
    [
        ("http://host.docker.internal:8010", "auto", True),
        ("http://localhost:8000/v1", "auto", True),
        ("http://127.0.0.1:12003", "auto", True),
        ("https://api.openai.com/v1", "auto", False),
        ("http://10.0.0.5:8000", "auto", False),
        # An explicit declaration wins over the address either way.
        ("http://10.0.0.5:8000", "self_hosted", True),
        ("http://localhost:8317", "provider_api", False),
    ],
)
def test_a_route_without_a_choice_follows_its_locality(base_url, deployment_type, allowed):
    adapter = _Route("r", base_url, deployment_type)
    assert default_batch_allowed(adapter) is allowed


def test_a_choice_overrides_the_locality_and_clearing_it_restores_it():
    local, cloud = _local(), _cloud()
    resolver = _resolver()
    assert (resolver.allows(MODEL, local), resolver.allows(MODEL, cloud)) == (True, False)

    resolver.set_choice(MODEL, CLOUD_ID, True)
    resolver.set_choice(MODEL, LOCAL_ID, False)
    assert (resolver.allows(MODEL, local), resolver.allows(MODEL, cloud)) == (False, True)
    # Keyed by model: another model's route of the same id is untouched.
    assert resolver.choice("other", CLOUD_ID) is None

    resolver.clear_model(MODEL)
    assert (resolver.allows(MODEL, local), resolver.allows(MODEL, cloud)) == (True, False)


async def test_the_resolver_loads_the_stored_choices_and_says_when_it_cannot():
    store = MagicMock()
    store.list_all_provider_route_batch_policies = AsyncMock(
        return_value=[{"model_id": MODEL, "route_id": "r1", "batch_allowed": True}]
    )
    resolver = BatchRouteResolver(store)
    assert await resolver.load_all() is True
    assert resolver.choice(MODEL, "r1") is True
    assert await resolver.load_all() is False, "an unchanged snapshot is not a change"

    failing = MagicMock()
    failing.list_all_provider_route_batch_policies = AsyncMock(side_effect=RuntimeError("down"))
    unread = BatchRouteResolver(failing)
    with pytest.raises(RuntimeError):
        await unread.load_all()
    # Unknown is not empty: admin writes wait for a successful read.
    assert unread.ready is False
    assert resolver.ready is True


def test_the_batch_range_is_the_models_batch_allowed_endpoints_by_its_canonical_id():
    local, cloud = _local(), _cloud()
    router = _router((local, 0.01), (cloud, 1.0))
    assert batch_endpoint_scope(router, MODEL) == frozenset({LOCAL_ID})

    router.batch_route_resolver = _resolver({(MODEL, CLOUD_ID): True})
    # An alias reads the canonical model's choices.
    assert batch_endpoint_scope(router, "mixed-alias") == frozenset({LOCAL_ID, CLOUD_ID})
    # A model the route table does not know gets no range at all.
    assert batch_endpoint_scope(router, "unknown") is None
    assert batch_endpoint_scope(None, MODEL) is None


def test_whether_a_queued_item_still_has_a_route():
    router = _router((_cloud(), 1.0))
    services = types.SimpleNamespace(router=router, embedding_adapters={})
    assert batch_route_available(services, MODEL) is False
    # A model gone from the gateway fails as unavailable at dispatch instead.
    assert batch_route_available(services, "gone") is True

    router.batch_route_resolver = _resolver({(MODEL, CLOUD_ID): True})
    assert batch_route_available(services, MODEL) is True


def _embedder(name: str, base_url: str) -> Any:
    config = types.SimpleNamespace(
        id="emb", name=name, provider=name, base_url=base_url, deployment_type="auto"
    )
    return types.SimpleNamespace(config=config)


def test_a_batch_embedding_request_keeps_the_allowed_backends_of_a_chain():
    local, cloud = _embedder("local", "http://localhost:9000"), _embedder("cloud", "https://e.test")
    chain = FallbackEmbeddingAdapter([cloud, local])

    narrowed = batch_embedding_adapter(chain, "emb", router=None)
    assert narrowed is local
    assert batch_embedding_adapter(local, "emb", router=None) is local
    assert batch_embedding_adapter(cloud, "emb", router=None) is None

    services = types.SimpleNamespace(router=None, embedding_adapters={"emb": cloud})
    assert batch_route_available(services, "emb", embeddings=True) is False
    services.embedding_adapters = {"emb": chain}
    assert batch_route_available(services, "emb", embeddings=True) is True


# -- dispatch: a batch item runs on the batch-allowed routes only ---------------------


def _services(router: FixedRouter) -> tuple[FastAPI, AppServices]:
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()
    services = AppServices(router=router, log_store=log_store, operational_store=op_store)
    app = FastAPI()
    app.state.services = services
    return app, services


_OWNER = {
    "user_id": "owner-1",
    "role": "internal",
    "authenticated": True,
    "is_admin": False,
    "disabled_models": [],
    "max_concurrent_requests": None,
    "auth_key_hash": "owner-key-hash",
}

_BODIES = {
    "chat": {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    "responses": {"model": MODEL, "input": "hi", "store": False},
}


async def _dispatch(app: FastAPI, services: AppServices, target: str, item_id: int = 1):
    return await dispatch_item(
        app=app,
        services=services,
        user_ctx=_OWNER,
        body=_BODIES[target],
        batch_id="b",
        target=target,
        item_id=item_id,
        attempt=1,
    )


@pytest.mark.parametrize("target", ["chat", "responses"])
async def test_a_batch_item_never_reaches_a_cloud_route_however_it_is_weighted(target):
    local, cloud = _local(), _cloud()
    app, services = _services(_router((local, 0.001), (cloud, 1000.0)))

    for item_id in range(1, 21):
        result = await _dispatch(app, services, target, item_id)
        assert result.error is None, result

    assert (local.calls, cloud.calls) == (20, 0)


async def test_an_allowed_cloud_route_takes_batch_work_and_a_forbidden_local_one_does_not():
    local, cloud = _local(), _cloud()
    resolver = _resolver({(MODEL, CLOUD_ID): True, (MODEL, LOCAL_ID): False})
    app, services = _services(_router((local, 1000.0), (cloud, 0.001), resolver=resolver))

    for item_id in range(1, 11):
        assert (await _dispatch(app, services, "chat", item_id)).error is None

    assert (local.calls, cloud.calls) == (0, 10)


async def test_a_batch_item_for_a_model_with_no_batch_route_fails_for_good():
    cloud = _cloud()
    app, services = _services(_router((cloud, 1.0)))

    result = await _dispatch(app, services, "chat")

    assert cloud.calls == 0
    assert result.retryable is False
    assert result.error["message"] == batch_route_refusal(MODEL)


async def test_the_router_gets_the_batch_range_and_leaves_the_owners_pin_alone(monkeypatch):
    local, cloud = _local(), _cloud()
    router = _router((local, 1.0), (cloud, 1.0))
    app, services = _services(router)
    seen: list[RoutingRequestOptions] = []
    original = router.chat_completion

    async def spy(model: str, messages: Any, **params: Any) -> Any:
        seen.append(params.get("routing_options"))
        return await original(model, messages, **params)

    monkeypatch.setattr(router, "chat_completion", spy)

    assert (await _dispatch(app, services, "chat")).error is None

    (options,) = seen
    assert options.endpoint_scope == frozenset({LOCAL_ID})
    assert options.affinity is False


# -- the affinity pin: batch work neither follows nor moves it -------------------------


async def test_batch_selection_neither_follows_nor_moves_the_callers_affinity_pin():
    local, cloud = _local(), _cloud()
    router = _router((local, 1.0), (cloud, 1.0))
    with req_ctx.push(affinity_key="owner-key-hash"):
        # The owner's interactive traffic is pinned to the cloud route.
        pinned = router._select_adapter(MODEL, endpoint_scope=frozenset({CLOUD_ID}))
        assert pinned is cloud
        pin = router._affinity[("owner-key-hash", MODEL)]
        assert pin.endpoint_id == CLOUD_ID

        # A batch selection, narrowed to the local route, leaves the pin as it was.
        chosen = router._select_adapter(
            MODEL, endpoint_scope=frozenset({LOCAL_ID}), use_affinity=False
        )
        assert chosen is local
        assert router._affinity[("owner-key-hash", MODEL)] is pin
        assert pin.endpoint_id == CLOUD_ID

        # An ordinary selection still follows the pin.
        assert router._select_adapter(MODEL) is cloud
        # With the option on, a scoped selection would have dropped and re-pinned it.
        router._select_adapter(MODEL, endpoint_scope=frozenset({LOCAL_ID}))
        assert router._affinity[("owner-key-hash", MODEL)].endpoint_id == LOCAL_ID


async def test_a_failed_batch_attempt_does_not_drop_the_owners_pin():
    class _Failing(_Route):
        async def chat_completion(self, messages, **params):
            raise RuntimeError("upstream broke")

    broken = _Failing("local", "http://localhost:1", endpoint_id="m:local-1")
    healthy = _Route("other", "http://localhost:2", endpoint_id="m:local-2")
    router = _router((broken, 1000.0), (healthy, 0.001))
    with req_ctx.push(affinity_key="owner-key-hash"):
        router._affinity[("owner-key-hash", MODEL)] = types.SimpleNamespace(
            endpoint_id="kept", expires_at=float("inf"), created_at=0.0
        )
        options = RoutingRequestOptions(affinity=False)
        for _ in range(5):
            await router.chat_completion(
                MODEL, [{"role": "user", "content": "hi"}], routing_options=options
            )
        assert router._affinity[("owner-key-hash", MODEL)].endpoint_id == "kept"


# -- submission ---------------------------------------------------------------------


def _admission(router: FixedRouter, embedding_adapters: dict[str, Any] | None = None):
    return targets.AdmissionContext(
        user_ctx={"user_id": "u", "role": "internal"},
        routes=router.routes,
        services=None,
        visibility_resolver=None,
        embedding_adapters=embedding_adapters,
        embedding_routes=router.embedding_routes,
        batch_routes=getattr(router, "batch_route_resolver", None),
    )


async def test_a_batch_for_a_model_with_no_batch_route_is_refused_with_the_same_reason():
    router = _router((_cloud(), 1.0))

    problem = (await targets.check_item("chat", _BODIES["chat"], _admission(router)))[1]

    assert problem is not None
    assert (problem.code, problem.status, problem.param) == (BATCH_ROUTE_UNAVAILABLE, 400, "model")
    assert problem.message == batch_route_refusal(MODEL)

    router.batch_route_resolver = _resolver({(MODEL, CLOUD_ID): True})
    model, problem = await targets.check_item("chat", _BODIES["chat"], _admission(router))
    assert (model, problem) == (MODEL, None)


async def test_an_embedding_batch_needs_a_backend_batch_work_may_use():
    router = FixedRouter()
    cloud = _embedder("cloud", "https://e.test")
    router.register_embedding_route("emb", [(cloud, 1.0)], required_role="free")

    _model, problem = await targets.check_item(
        "embeddings", {"model": "emb", "input": "x"}, _admission(router, {"emb": cloud})
    )

    assert problem is not None and problem.code == BATCH_ROUTE_UNAVAILABLE


# -- the hybrid composition's policy and the Messages surface honor the range ---------


def test_the_hybrid_policy_draws_and_falls_back_inside_the_batch_range():
    from routing.policies import FixedPolicy

    local, cloud = _local(), _cloud()
    router = _router((local, 0.001), (cloud, 1000.0))
    policy = FixedPolicy(compute=router, local_scope=frozenset({LOCAL_ID}))
    options = RoutingRequestOptions(endpoint_scope=frozenset({LOCAL_ID}), affinity=False)
    messages = [{"role": "user", "content": "hi"}]

    for _ in range(20):
        decision = policy.select_backend(MODEL, messages, routing_options=options)
        assert decision.target.endpoint_id == LOCAL_ID
    # Nothing outside the range is planned as a fallback either.
    assert policy.fallback_attempts(MODEL, messages, decision, routing_options=options) == ()
    # An interactive request still draws over every route.
    picks = {policy.select_backend(MODEL, messages).target.endpoint_id for _ in range(50)}
    assert CLOUD_ID in picks


def test_a_messages_batch_item_is_sent_only_to_a_batch_allowed_route():
    from fastapi import HTTPException

    from serving.servers.routers.anthropic_messages import _pick_dispatch_adapter

    local, cloud = _local(), _cloud()
    router = _router((cloud, 1000.0), (local, 0.001))

    # Route order puts the cloud route first; a batch item skips it.
    assert _pick_dispatch_adapter(MODEL, MODEL, router, "internal") is cloud
    assert _pick_dispatch_adapter(MODEL, MODEL, router, "internal", batch_routes_only=True) is local

    only_cloud = _router((_cloud(), 1.0))
    with pytest.raises(HTTPException) as refused:
        _pick_dispatch_adapter(MODEL, MODEL, only_cloud, "internal", batch_routes_only=True)
    assert refused.value.status_code == 400
    assert refused.value.detail == batch_route_refusal(MODEL)
