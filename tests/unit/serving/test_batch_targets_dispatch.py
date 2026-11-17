"""dispatch_item on every target, through the real protocol executions.

Each target runs the execution its online route calls; only the upstream
adapters are stand-ins. What is held here: no target builds a ``Request`` or
calls a route handler, each target's own billing point charges exactly once and
the attempt observes exactly that charge, an error an execution answers with
(rather than raises) is read by its status, a Messages item never reaches an
adapter that would drop one of its parameters, and an embeddings item is held
to the model's access gate (role, visibility override, the owner's denylist, a
missing gate) at admission and at dispatch.

The trailing block repeats the through-the-real-dispatch checks under a
synthetic CNY accounting policy at a deliberately non-1 rate: each target's
execution charges the ledger once in native currency, the attempt observer sees
the true USD equivalent of exactly that charge, and the USD-only ledger function
is never used.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from routing.routers import FixedRouter
from serving import accounting
from serving.accounting import AccountingPolicy
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch import dispatch
from serving.batch.dispatch import dispatch_item
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
# A free model: a real, resolvable price of zero (distinct from unknown).
ZERO_PRICING = dict.fromkeys(PRICING, "0")
# 100 prompt + 50 completion tokens at $1/$2 per million.
CHAT_COST = 100 * 1.00 / 1_000_000 + 50 * 2.00 / 1_000_000
# 1000 prompt tokens at $1 per million.
EMBED_COST = 1000 * 1.00 / 1_000_000
# Synthetic, deliberately non-1 rate: these tests exist to prove the two
# units are distinct and that the batch observer sees USD.
CNY_RATE = Decimal("7.25")
TARGETS = ["chat", "embeddings", "messages", "responses"]


class _Upstream(BaseAdapter):
    def __init__(self, model: str = MODEL) -> None:
        super().__init__(
            ModelConfig(
                id=model,
                name=model,
                provider="openai",
                base_url="https://upstream.test",
                # The gateway's own server, so batch work may use it.
                deployment_type="self_hosted",
                pricing=PRICING,
            )
        )
        self.calls: list[dict[str, Any]] = []
        self.usage = UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150)

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls.append({"messages": messages, "params": params})
        return self.format_response(content="answer", model=self.config.id, usage=self.usage)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("a batch item must never stream")
        yield


class _Embedder:
    def __init__(self) -> None:
        self.config = ModelConfig(
            id=EMBED,
            name=EMBED,
            provider="openai",
            base_url="https://emb.test",
            deployment_type="self_hosted",
            pricing=PRICING,
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
    # The native ledger function the accounting-aware billing points call on a
    # non-USD deployment. Configured for every setup so a CNY test never awaits
    # an unconfigured MagicMock; the USD tests still assert only the USD call.
    op_store.increment_user_cost_amount = AsyncMock()
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


@pytest.mark.parametrize("target", ["chat", "messages", "responses"])
async def test_a_prompt_cache_hit_reaches_the_batch_as_cached_input(target):
    # Each target reports the hit in its own shape; the batch reads it from any.
    app, services, upstream, _embedder = _setup()
    upstream.usage = UsageInfo(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cache_read_tokens=40,
        cache_read_reported=True,
    )
    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="b",
        target=target,
    )
    assert result.error is None, result
    assert (result.prompt_tokens, result.cached_tokens) == (100, 40)


async def test_usage_silent_about_caching_leaves_cached_input_unknown():
    app, services, _upstream, _embedder = _setup()
    result = await dispatch_item(
        app=app, services=services, user_ctx=_owner(), body=_BODIES["chat"], batch_id="b"
    )
    assert result.error is None, result
    assert result.cached_tokens is None


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
    from serving.servers.routers import anthropic_messages

    app, services, _upstream, _embedder = _setup()
    monkeypatch.setattr(
        anthropic_messages,
        "execute_messages",
        AsyncMock(
            return_value=anthropic_messages.MessagesError(
                status=529, message="busy", error_type="overloaded_error"
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


def test_each_target_has_an_executor():
    assert set(dispatch._EXECUTORS) == {"chat", "embeddings", "messages", "responses"}


@pytest.mark.parametrize("target", sorted(dispatch._EXECUTORS))
async def test_a_target_runs_without_a_request_or_a_handler(monkeypatch, target):
    """No synthetic HTTP request and no route handler on the batch execution path."""
    from starlette import requests

    from serving.servers.routers import anthropic_messages, completions, embeddings, responses

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a batch item built a Request or called a route handler")

    monkeypatch.setattr(requests.Request, "__init__", refuse)
    for module, handler in (
        (completions, "chat_completions"),
        (embeddings, "create_embeddings"),
        (anthropic_messages, "anthropic_messages"),
        (responses, "create_response"),
    ):
        monkeypatch.setattr(module, handler, refuse)
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


# -- CNY accounting: the observer sees USD, the ledger sees native ----------------
#
# These tests bind a synthetic CNY policy at a deliberately non-1 rate and run
# the real dispatch path -- the shared protocol executions every online route
# also calls -- for every target. They pin the contract a native ledger
# establishes: the quota ledger is charged once in the configured currency,
# while the batch attempt's ``cost_usd`` is the true USD equivalent of exactly
# that charge. The USD tests above stay untouched and run under the USD default.


@pytest.fixture
def cny_policy():
    """Bind a synthetic CNY policy for one test and restore the previous one.

    ``bind_accounting_policy`` is the production binding function, so this uses
    the same immutable-policy path the gateway uses at startup rather than
    poking module state. Restoration is asserted in the teardown.
    """
    previous = accounting.get_accounting_policy()
    policy = AccountingPolicy(
        currency="CNY",
        usd_to_currency_rate=CNY_RATE,
        rate_date="2026-10-24",
        rate_source="https://example.test/synthetic-rate",
    )
    accounting.bind_accounting_policy(policy)
    try:
        yield policy
    finally:
        accounting.bind_accounting_policy(previous)
        # Fail loudly if the process-wide binding was not restored: a leaked
        # CNY policy would silently change every later USD test.
        assert accounting.get_accounting_policy() == previous


#: The modules that hold each target's billing point. Responses delegates to
#: the chat ``CostTracker``, so its charge is observed through completions_cost.
_BILLING_MODULES = (
    "serving.servers.routers.completions_cost",
    "serving.servers.routers.embeddings",
    "serving.servers.routers.anthropic_messages",
)


@pytest.fixture
def observed_charges(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every value a billing point hands to the batch attempt observer.

    ``observe_billing`` is imported into each billing module by name, so the
    recorder is installed on every loaded module that holds the real function.
    The real observer still runs, so dispatch's own ``cost_usd``/status
    bookkeeping is unaffected.
    """
    import importlib

    from serving.batch import attempts as batch_attempts

    # Import every execution module before scanning, so none keeps an
    # unrecorded reference that would make its charge invisible.
    for name in (
        *_BILLING_MODULES,
        "serving.servers.routers.completions",
        "serving.servers.routers.responses",
    ):
        importlib.import_module(name)

    seen: list[Any] = []
    real = batch_attempts.observe_billing

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["cost_usd"] if "cost_usd" in kwargs else args[0])
        return real(*args, **kwargs)

    for module in list(sys.modules.values()):
        if module is None or module is batch_attempts:
            continue
        if getattr(module, "observe_billing", None) is real:
            monkeypatch.setattr(module, "observe_billing", recording)
    # Every billing point must be among them; a rename there is a real
    # regression, not a silent skip.
    for name in _BILLING_MODULES:
        assert sys.modules[name].observe_billing is recording, name
    return seen


async def _native_increments(services: AppServices) -> list[Any]:
    """Settle background billing, then return the native ledger charges."""
    ledger = services.operational_store.increment_user_cost_amount
    for _ in range(50):
        if ledger.await_count:
            break
        await asyncio.sleep(0.001)
    # Give a hypothetical second increment time to land before asserting.
    for _ in range(20):
        await asyncio.sleep(0.001)
    charges = [call.args[1] for call in ledger.await_args_list]
    # The spend ledger writes each charge as the exact amount the native
    # counters store (twelve places), never a binary float.
    assert all(isinstance(charge, Decimal) for charge in charges), charges
    assert all(charge == charge.quantize(Decimal("1e-12")) for charge in charges), charges
    return [float(charge) for charge in charges]


def _set_pricing(
    *, upstream: _Upstream, embedder: _Embedder, pricing: dict[str, str] | None
) -> None:
    """Set the price on both adapters' configs, as a models.yaml edit would."""
    upstream.config.pricing = pricing
    embedder.config.pricing = pricing


@pytest.mark.parametrize("target", TARGETS)
async def test_cny_dispatch_charges_native_once_and_reports_the_usd_equivalent(
    target, cny_policy, observed_charges
):
    app, services, upstream, embedder = _setup()
    expected_usd = EMBED_COST if target == "embeddings" else CHAT_COST

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="batch-cny",
        target=target,
        item_id=11,
        attempt=1,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )

    assert result.error is None, result
    assert len(embedder.calls if target == "embeddings" else upstream.calls) == 1
    native = await _native_increments(services)
    assert len(native) == 1, f"{target} was charged natively {len(native)} times"
    # The charged amount is the USD price converted at the configured rate;
    # it is not the USD amount itself.
    assert native[0] == pytest.approx(expected_usd * float(CNY_RATE))
    assert native[0] != pytest.approx(expected_usd)
    # The USD-only ledger function must never be used on a CNY deployment.
    services.operational_store.increment_user_cost.assert_not_awaited()
    # The billing point handed the observer the USD equivalent of exactly the
    # native charge above -- once, not the native amount, not a re-priced value.
    assert observed_charges == [pytest.approx(expected_usd)]
    assert result.cost_status == "billed"
    assert result.cost_usd == pytest.approx(float(native[0]) / float(CNY_RATE))
    assert result.cost_usd == pytest.approx(expected_usd)

    row = await _logged(services)
    # The api_logs pricing snapshot stays in the ledger currency (native CNY),
    # while the batch attempt's ``cost_usd`` above is the USD compatibility value.
    if row.get("pricing") and "prompt" in row["pricing"]:
        assert Decimal(row["pricing"]["prompt"]) == Decimal("1.00") * CNY_RATE
    assert row["metadata"]["batch_job_id"] == "batch-cny"
    assert (row["metadata"]["batch_item_id"], row["metadata"]["batch_attempt"]) == (11, 1)
    assert result.request_id == row["request_id"]


@pytest.mark.parametrize("target", TARGETS)
async def test_cny_zero_price_is_a_known_zero_and_never_charges(
    target, cny_policy, observed_charges
):
    app, services, upstream, embedder = _setup()
    _set_pricing(upstream=upstream, embedder=embedder, pricing=ZERO_PRICING)

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="batch-cny-zero",
        target=target,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )

    assert result.error is None, result
    assert await _native_increments(services) == []
    services.operational_store.increment_user_cost.assert_not_awaited()
    # A resolvable price of zero is a known zero, never the unknown marker.
    assert observed_charges == [0.0]
    assert (result.cost_status, result.cost_usd) == ("billed", 0.0)


@pytest.mark.parametrize("target", TARGETS)
async def test_cny_unknown_pricing_is_reported_unknown_and_never_charges(
    target, cny_policy, observed_charges
):
    app, services, upstream, embedder = _setup()
    _set_pricing(upstream=upstream, embedder=embedder, pricing=None)

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="batch-cny-unknown",
        target=target,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )

    assert result.error is None, result
    assert await _native_increments(services) == []
    services.operational_store.increment_user_cost.assert_not_awaited()
    # Unknown pricing is reported as unknown, not coerced to a zero charge.
    assert observed_charges == [None]
    assert (result.cost_status, result.cost_usd) == ("unknown", None)


async def test_cny_message_error_answer_is_not_billed(monkeypatch, cny_policy, observed_charges):
    from serving.servers.routers import anthropic_messages

    app, services, _upstream, _embedder = _setup()
    monkeypatch.setattr(
        anthropic_messages,
        "execute_messages",
        AsyncMock(
            return_value=anthropic_messages.MessagesError(
                status=529, message="busy", error_type="overloaded_error"
            )
        ),
    )
    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_BODIES["messages"],
        batch_id="batch-cny-error",
        target="messages",
    )

    assert (result.status_code, result.retryable) == (529, True)
    assert result.cost_status == "not_billed"
    assert await _native_increments(services) == []
    services.operational_store.increment_user_cost.assert_not_awaited()
    # Error answers are never billed: the billing point is never reached.
    assert observed_charges == []


async def test_cny_batch_owner_context_carries_native_quota_through_dispatch(
    cny_policy, observed_charges
):
    """A worker context with native Decimal quota amounts runs unchanged.

    ``authenticate_stored_key`` adds the native ``quota_daily_cost`` /
    ``spent_today`` amounts on a CNY deployment; the shared execution receives
    the context as a read-only snapshot and must bill natively regardless.
    """
    app, services, _upstream, _embedder = _setup()
    owner = {
        **_owner(),
        "quota_daily_cost_usd": float(Decimal("72.5") / CNY_RATE),
        "spent_today_usd": 0.0,
        "quota_daily_cost": Decimal("72.5"),
        "spent_today": Decimal("0"),
    }

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=owner,
        body=_BODIES["chat"],
        batch_id="b",
        target="chat",
    )

    assert result.error is None, result
    assert await _native_increments(services) == [pytest.approx(CHAT_COST * float(CNY_RATE))]
    assert result.cost_usd == pytest.approx(CHAT_COST)


_ONLINE_PATHS = {
    "chat": "/v1/chat/completions",
    "embeddings": "/v1/embeddings",
    "messages": "/v1/messages",
    "responses": "/v1/responses",
}


@pytest.mark.parametrize("target", TARGETS)
async def test_cny_online_route_and_batch_item_charge_the_same_native_amount(
    target, cny_policy, observed_charges
):
    """One shared execution: the online route and a batch item bill identically.

    The same body is served once through its HTTP route and once as a batch
    item on fresh services. Both must charge the native ledger once with the
    same amount and report the same USD equivalent to the observer (which only
    the batch attempt listens to).
    """
    from httpx import ASGITransport, AsyncClient

    from serving.servers.auth import verify_api_key
    from serving.servers.middleware.error import install_error_handlers
    from serving.servers.routers import anthropic_messages, completions, embeddings, responses

    app, services, _upstream, _embedder = _setup()
    install_error_handlers(app)
    for module in (completions, embeddings, anthropic_messages, responses):
        app.include_router(module.router)
    app.dependency_overrides[verify_api_key] = _owner
    headers = {"anthropic-version": "2023-06-01"} if target == "messages" else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_ONLINE_PATHS[target], json=_BODIES[target], headers=headers)
    assert response.status_code == 200, response.text
    online = await _native_increments(services)

    batch_app, batch_services, _u, _e = _setup()
    result = await dispatch_item(
        app=batch_app,
        services=batch_services,
        user_ctx=_owner(),
        body=_BODIES[target],
        batch_id="batch-parity",
        target=target,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )
    assert result.error is None, result
    batch = await _native_increments(batch_services)

    expected_usd = EMBED_COST if target == "embeddings" else CHAT_COST
    assert online == batch == [pytest.approx(expected_usd * float(CNY_RATE))]
    services.operational_store.increment_user_cost.assert_not_awaited()
    batch_services.operational_store.increment_user_cost.assert_not_awaited()
    # The online billing point reports too (nothing listens outside a batch);
    # both reports are the USD equivalent of the identical native charge.
    assert observed_charges == [pytest.approx(expected_usd)] * 2
    assert result.cost_usd == pytest.approx(expected_usd)
