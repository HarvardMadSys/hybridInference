"""dispatch_item on every target, through the real handlers.

Each target runs the handler behind its endpoint; only the upstream adapters
are stand-ins. What is held here: every argument FastAPI would inject reaches
the handler explicitly (never a ``Depends`` marker), each target's own billing
point charges exactly once and the attempt observes exactly that charge, an
error a handler answers with (rather than raises) is read by its status, a
Messages item never reaches an adapter that would drop one of its parameters,
and an embeddings item is held to the model's access gate (role, visibility
override, the owner's denylist, a missing gate) at admission and at dispatch.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, params as fastapi_params

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch import dispatch
from serving.batch.dispatch import HANDLERS, call_handler, dispatch_item
from serving.servers.deps import AppServices

MODEL = "target-model"
EMBED = "embed-model"
EMBED_ALIAS = "embed-alias"
PRICING = {
    "prompt": "1.00",
    "completion": "2.00",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
    "image": "0",
    "request": "0",
}
# 100 prompt + 50 completion tokens at $1/$2 per million.
CHAT_COST = 100 * 1.00 / 1_000_000 + 50 * 2.00 / 1_000_000


class _Upstream(BaseAdapter):
    def __init__(self, model: str = MODEL) -> None:
        super().__init__(
            ModelConfig(
                id=model,
                name=model,
                provider="openai",
                base_url="https://upstream.test",
                pricing=PRICING,
            )
        )
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls.append({"messages": messages, "params": params})
        return self.format_response(
            content="answer",
            model=self.config.id,
            usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("a batch item must never stream")
        yield


class _Embedder:
    def __init__(self) -> None:
        self.config = ModelConfig(
            id=EMBED, name=EMBED, provider="openai", base_url="https://emb.test", pricing=PRICING
        )
        self.calls: list[Any] = []

    async def embeddings(self, inputs: Any, **params: Any) -> dict[str, Any]:
        self.calls.append(inputs)
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "model": EMBED,
            "usage": {"prompt_tokens": 1000, "total_tokens": 1000},
        }


def _setup(
    *, embed_role: str | None = "free", visibility_resolver: Any = None
) -> tuple[FastAPI, AppServices, _Upstream, _Embedder]:
    """The services as the registry leaves them.

    An embedding model is registered the way ``register_from_models_yaml``
    registers one: its adapter under the canonical id and each alias, and its
    access gate on the router under the same names. ``embed_role=None`` leaves
    the gate out, the inconsistency /v1/embeddings must refuse.
    """
    upstream, embedder = _Upstream(), _Embedder()
    router = FixedRouter()
    router.register_route(MODEL, [(upstream, 1.0)])
    if embed_role is not None:
        router.register_embedding_route(
            EMBED, [(embedder, 1.0)], aliases=[EMBED_ALIAS], required_role=embed_role
        )
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()
    services = AppServices(
        router=router,
        log_store=log_store,
        operational_store=op_store,
        embedding_adapters={EMBED: embedder, EMBED_ALIAS: embedder},
        model_visibility_resolver=visibility_resolver,
    )
    app = FastAPI()
    app.state.services = services
    return app, services, upstream, embedder


def _owner() -> dict[str, Any]:
    return {
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
    "embeddings": {"model": EMBED, "input": "hello"},
    "messages": {"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    "responses": {"model": MODEL, "input": "hi", "store": False},
}


async def _increments(services: AppServices) -> list[float]:
    for _ in range(50):
        await asyncio.sleep(0)
    return [call.args[1] for call in services.operational_store.increment_user_cost.await_args_list]


async def _logged(services: AppServices) -> dict[str, Any]:
    for _ in range(200):
        if services.log_store.log_request.await_count:
            return services.log_store.log_request.await_args.kwargs
        await asyncio.sleep(0.001)
    raise AssertionError("no request log row was written")


@pytest.mark.parametrize("target", ["chat", "embeddings", "messages", "responses"])
async def test_each_target_is_billed_once_and_the_attempt_sees_that_charge(target):
    app, services, upstream, embedder = _setup()

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="batch-t",
        target=target,
        item_id=7,
        attempt=2,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )

    assert result.error is None, result
    increments = await _increments(services)
    assert len(increments) == 1, f"{target} was charged {len(increments)} times"
    assert result.cost_status == "billed"
    assert result.cost_usd == pytest.approx(increments[0])
    if target == "embeddings":
        assert result.cost_usd == pytest.approx(1000 * 1.00 / 1_000_000)
        assert len(embedder.calls) == 1
    else:
        assert result.cost_usd == pytest.approx(CHAT_COST)
        assert len(upstream.calls) == 1
    row = await _logged(services)
    assert row["metadata"]["batch_job_id"] == "batch-t"
    assert (row["metadata"]["batch_item_id"], row["metadata"]["batch_attempt"]) == (7, 2)
    assert result.request_id == row["request_id"]


async def test_the_targets_answer_in_their_own_shapes():
    app, services, _upstream, _embedder = _setup()

    async def run(target: str) -> dict[str, Any]:
        result = await dispatch_item(
            app=app,
            services=services,
            user_ctx=_owner(),
            body=_BODIES[target],
            batch_id="b",
            target=target,
        )
        assert result.error is None, result
        return result.response

    assert (await run("chat"))["object"] == "chat.completion"
    assert (await run("embeddings"))["data"][0]["embedding"] == [0.1, 0.2]
    message = await run("messages")
    assert (message["type"], message["role"]) == ("message", "assistant")
    response = await run("responses")
    assert (response["object"], response["store"]) == ("response", False)


@pytest.mark.parametrize("target", sorted(HANDLERS))
async def test_every_injected_argument_reaches_the_handler_explicitly(monkeypatch, target):
    """No handler parameter may arrive as a FastAPI marker (a truthy ``Depends``)."""
    module_name, function_name = HANDLERS[target]
    module = importlib.import_module(module_name)
    real = getattr(module, function_name)
    seen: dict[str, Any] = {}

    @functools.wraps(real)
    async def spy(*args: Any, **kwargs: Any) -> Any:
        bound = inspect.signature(real).bind(*args, **kwargs)
        seen.update(bound.arguments)
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, function_name, spy)
    app, services, _upstream, _embedder = _setup()
    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="b",
        target=target,
    )
    assert result.error is None, result
    parameters = inspect.signature(real).parameters
    injected = [
        name
        for name, parameter in parameters.items()
        if isinstance(parameter.default, (fastapi_params.Depends, fastapi_params.Param))
    ]
    assert injected, f"{function_name} has no injected parameters to check"
    for name in injected:
        assert name in seen, f"{function_name}: {name} was left to its default"
        assert not isinstance(seen[name], (fastapi_params.Depends, fastapi_params.Param))


# -- embeddings: the access gate holds on the batch path --------------------------


class _Visibility:
    """A runtime visibility override for the canonical embedding model."""

    def __init__(self, role: str) -> None:
        self._role = role
        self.asked: list[str] = []

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        self.asked.append(model_id)
        return self._role if model_id == EMBED else default_role


async def _embed(services: AppServices, app: FastAPI, *, model: str = EMBED, **owner: Any):
    return await dispatch_item(
        app=app,
        services=services,
        user_ctx={**_owner(), **owner},
        body={"model": model, "input": "hello"},
        batch_id="b",
        target="embeddings",
    )


async def _assert_refused_before_upstream(result, services: AppServices, embedder: _Embedder):
    # Refused exactly as an unknown model, and nothing ran or was charged.
    assert (result.status_code, result.retryable) == (404, False), result
    assert result.error["code"] == "model_not_found"
    assert result.response is None
    assert result.cost_status == "not_billed"
    assert embedder.calls == []
    assert await _increments(services) == []


@pytest.mark.parametrize("model", [EMBED, EMBED_ALIAS])
async def test_an_allowed_embedding_item_runs_by_canonical_id_or_alias(model):
    app, services, _upstream, embedder = _setup(embed_role="pro")

    result = await _embed(services, app, model=model, role="pro")

    assert result.error is None, result
    assert len(embedder.calls) == 1
    assert result.cost_status == "billed"


async def test_an_embedding_item_below_the_models_role_never_reaches_upstream():
    app, services, _upstream, embedder = _setup(embed_role="admin")

    result = await _embed(services, app, role="internal")

    await _assert_refused_before_upstream(result, services, embedder)


async def test_a_visibility_override_closes_an_embedding_model_for_the_batch():
    visibility = _Visibility("admin")
    app, services, _upstream, embedder = _setup(embed_role="free", visibility_resolver=visibility)

    result = await _embed(services, app, model=EMBED_ALIAS, role="internal")

    await _assert_refused_before_upstream(result, services, embedder)
    # The override is keyed by the canonical id, whichever name the item used.
    assert visibility.asked == [EMBED]


async def test_a_visibility_override_opens_an_embedding_model_for_the_batch():
    # The resolver the batch path passes is the application's own: without it
    # the pro baseline would refuse this free owner.
    visibility = _Visibility("free")
    app, services, _upstream, embedder = _setup(embed_role="pro", visibility_resolver=visibility)

    result = await _embed(services, app, role="free")

    assert result.error is None, result
    assert len(embedder.calls) == 1


@pytest.mark.parametrize("model", [EMBED, EMBED_ALIAS])
async def test_an_owner_denylisted_embedding_model_never_reaches_upstream(model):
    # The denylist holds canonical ids; an alias is held to its model's entry.
    app, services, _upstream, embedder = _setup(embed_role="free")

    result = await _embed(services, app, model=model, disabled_models=[EMBED])

    await _assert_refused_before_upstream(result, services, embedder)


async def test_an_embedding_adapter_without_an_access_gate_is_refused():
    app, services, _upstream, embedder = _setup(embed_role=None)
    assert services.router.embedding_routes == {}

    result = await _embed(services, app)

    await _assert_refused_before_upstream(result, services, embedder)


async def _admit_embedding(services: AppServices, *, model: str = EMBED, **owner: Any):
    """Admit one embeddings item through the context /v1/batches builds."""
    from serving.batch.targets import check_item
    from serving.servers.routers.batches import _admission_context

    ctx = _admission_context(
        {**_owner(), **owner},
        services=services,
        router_exec=services.router,
        visibility_resolver=services.model_visibility_resolver,
    )
    return await check_item("embeddings", {"model": model, "input": "hello"}, ctx)


@pytest.mark.parametrize("model", [EMBED, EMBED_ALIAS])
async def test_admission_admits_an_allowed_embedding_model_under_its_canonical_id(model):
    _app, services, _upstream, _embedder = _setup(embed_role="pro")

    assert await _admit_embedding(services, model=model, role="pro") == (EMBED, None)


@pytest.mark.parametrize(
    ("embed_role", "visibility", "owner"),
    [
        pytest.param("admin", None, {"role": "internal"}, id="role"),
        pytest.param("free", _Visibility("admin"), {"role": "internal"}, id="visibility"),
        pytest.param("free", None, {"disabled_models": [EMBED]}, id="denylist"),
        pytest.param(None, None, {}, id="missing-gate"),
    ],
)
@pytest.mark.parametrize("model", [EMBED, EMBED_ALIAS])
async def test_admission_refuses_an_embedding_model_the_owner_may_not_use(
    embed_role, visibility, owner, model
):
    # Refused at upload exactly like an unknown model, as chat items are, so a
    # batch neither queues work that cannot run nor reveals the model exists.
    _app, services, _upstream, _embedder = _setup(
        embed_role=embed_role, visibility_resolver=visibility
    )

    canonical, problem = await _admit_embedding(services, model=model, **owner)

    assert canonical is None
    assert (problem.code, problem.status, problem.param) == ("model_not_found", 404, "model")
    unknown_canonical, unknown = await _admit_embedding(services, model="no-such-embedding")
    assert unknown is not None and unknown_canonical is None
    assert problem.message == unknown.message.replace("no-such-embedding", model)


def test_call_handler_refuses_a_missing_injected_argument():
    from fastapi import Depends, Header

    async def handler(request, user=Depends(lambda: None), key=Header(None), plain=1):
        return None

    with pytest.raises(TypeError, match="user"):
        call_handler(handler, object(), key=None)
    with pytest.raises(TypeError, match="key"):
        call_handler(handler, object(), user={})
    call_handler(handler, object(), user={}, key=None).close()


async def test_a_messages_item_never_reaches_an_adapter_that_would_drop_a_parameter():
    app, services, upstream, _embedder = _setup()
    body = {**_BODIES["messages"], "thinking": {"type": "enabled", "budget_tokens": 1024}}

    result = await dispatch_item(
        app=app, services=services, user_ctx=_owner(), body=body, batch_id="b", target="messages"
    )

    assert upstream.calls == []
    assert (result.status_code, result.retryable) == (400, False)
    assert result.cost_status == "not_billed"
    assert await _increments(services) == []


async def test_a_native_error_response_is_read_by_its_status(monkeypatch):
    from fastapi.responses import JSONResponse

    from serving.servers.routers import anthropic_messages

    app, services, _upstream, _embedder = _setup()
    monkeypatch.setattr(
        anthropic_messages,
        "anthropic_messages",
        AsyncMock(
            return_value=JSONResponse(
                {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
                status_code=529,
            )
        ),
    )
    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES["messages"],
        batch_id="b",
        target="messages",
    )
    assert (result.status_code, result.retryable) == (529, True)
    assert result.error["anthropic"] == {"type": "overloaded_error", "message": "busy"}
    assert result.response is None


async def test_a_success_its_billing_point_could_not_price_is_unknown(monkeypatch):
    app, services, upstream, _embedder = _setup()
    upstream.config.pricing = PRICING
    from serving.servers.routers import completions_cost

    original = completions_cost.CostTracker.schedule_increment

    async def unpriced(self, **kwargs):
        from serving.batch.attempts import observe_billing

        observe_billing(cost_usd=None)
        return kwargs["routing"]

    monkeypatch.setattr(completions_cost.CostTracker, "schedule_increment", unpriced)
    try:
        result = await dispatch_item(
            app=app,
            services=services,
            user_ctx=_owner(),
            body=_BODIES["chat"],
            batch_id="b",
            target="chat",
        )
    finally:
        monkeypatch.setattr(completions_cost.CostTracker, "schedule_increment", original)
    assert result.error is None
    assert (result.cost_status, result.cost_usd) == ("unknown", None)


def test_each_target_has_exactly_one_executor():
    core, handler = set(dispatch._CORE_EXECUTORS), set(dispatch._HANDLER_EXECUTORS)

    assert core | handler == {"chat", "embeddings", "messages", "responses"}
    assert not core & handler
    assert set(HANDLERS) == handler


@pytest.mark.parametrize("target", sorted(dispatch._CORE_EXECUTORS))
async def test_a_core_target_runs_without_a_request_or_a_handler(monkeypatch, target):
    """No synthetic HTTP request and no route handler on the batch execution path."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a core target built a Request or called a handler")

    monkeypatch.setattr(dispatch, "_request", refuse)
    monkeypatch.setattr(dispatch, "call_handler", refuse)
    app, services, upstream, embedder = _setup()

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="b",
        target=target,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )

    assert result.error is None, result
    assert result.cost_status == "billed"
    assert len(embedder.calls if target == "embeddings" else upstream.calls) == 1
