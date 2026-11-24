"""The greedy router (``router: greedy``): a model's own engine first, the cloud when full.

A real shared ``FixedRouter`` holds one local engine and one cloud route. The
greedy router registers the engine's queue when it starts, so the queue counts
every request the engine is sent, and each request's first token is released by
the test, so the engine is exactly as busy as the test says.
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from routing.greedy import (
    OFFLOAD_SPILLOVER,
    GreedyRouter,
    GreedyRoutingError,
    cutoff_deadline,
)
from routing.model_router_registry import ModelRouterRegistry
from routing.offload import OFFLOAD_LAST_RESORT, OFFLOAD_QUEUE_WAIT
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, FixedRouter
from routing.strategies import build_router, validate_router_config
from routing.strategies.greedy import CutoffParams
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import (
    EngineQueueConflict,
    UpstreamConcurrencyLimiter,
    UpstreamSaturated,
    get_upstream_limiter,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.exceptions import UpstreamRequestTimedOut
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
#: Estimated at about 2500 prompt tokens.
LONG_MESSAGES = [{"role": "user", "content": "x" * 10_000}]
KEY = "EMPTY"
ENGINE_URL = "http://localhost:18003/v1"
ENGINE_ADDRESS = "localhost:18003"
ENGINE_ID = f"{MODEL}:local-18003"
CLOUD_ID = f"{MODEL}:cloud-api"
CLOUD_URL = "https://cloud.example/v1"

#: A Cutoff whose deadline is 0.1 s after arrival for any prompt.
SHORT_CUTOFF = {
    "ttft_budget": {"fixed_ms": 1100, "per_token_ms": 0},
    "cloud_ttft": {"fixed_ms": 1000, "per_input_token_ms": 0},
    "margin_ms": 0,
}


class _Engine(BaseAdapter):
    """A local engine whose every request waits for the test to release its first token."""

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        port: int = 18003,
        provider: str = "engine",
        context_length: int = 8192,
        endpoint_id: str | None = None,
        path: str = "/v1",
    ) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=provider,
                base_url=f"http://localhost:{port}{path}",
                endpoint_id=endpoint_id or f"{MODEL}:local-{port}",
                context_length=context_length,
            )
        )
        self.fail_with = fail_with
        self.answers: list[asyncio.Event] = []
        self.deadlines: list[Any] = []
        self.chat_sent = 0
        self.cancelled = 0

    @property
    def sent_count(self) -> int:
        return len(self.answers)

    async def wait_sent(self, count: int) -> None:
        async with asyncio.timeout(5):
            while self.sent_count < count:
                await asyncio.sleep(0.005)

    def answer(self, index: int) -> None:
        self.answers[index].set()

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.deadlines.append(req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE))
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            self.chat_sent += 1
            if self.fail_with is not None:
                raise self.fail_with
            return self.format_response(content="engine", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.deadlines.append(req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE))
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url) as slot:
            answered = asyncio.Event()
            self.answers.append(answered)
            if self.fail_with is not None:
                raise self.fail_with
            yield _chunk({"role": "assistant"})
            try:
                await answered.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            report_first_token(slot)
            yield _chunk({"content": "engine"})
            yield _chunk({}, finish_reason="stop")


class _Cloud(BaseAdapter):
    """The model's cloud route, which takes an outbound slot as an adapter does.

    ``fail_with`` fails a request once it has left the gateway, before any
    output; ``unsent_with`` fails it before, as a full outbound limit does.
    """

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        unsent_with: BaseException | None = None,
        provider: str = "cloud",
        endpoint_id: str = CLOUD_ID,
        context_length: int = 8192,
    ) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=provider,
                base_url=CLOUD_URL,
                endpoint_id=endpoint_id,
                context_length=context_length,
            )
        )
        self.fail_with = fail_with
        self.unsent_with = unsent_with
        #: Raised after the first output, once the stream is committed.
        self.fail_after_output: BaseException | None = None
        self.calls = 0

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.calls += 1
        if self.unsent_with is not None:
            raise self.unsent_with
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            return self.format_response(content="cloud", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        if self.unsent_with is not None:
            raise self.unsent_with
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            yield _chunk({"content": "cloud"})
            if self.fail_after_output is not None:
                raise self.fail_after_output
            yield _chunk({}, finish_reason="stop")


class _BadRequest(Exception):
    """An upstream's 400: the request itself is malformed."""

    status = 400


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _fixed(*adapters: BaseAdapter | None) -> FixedRouter:
    fixed = FixedRouter()
    fixed.register_route(MODEL, [(adapter, 1.0) for adapter in adapters if adapter is not None])
    return fixed


async def _greedy(*adapters: BaseAdapter, **params: Any) -> GreedyRouter:
    router = build_router("greedy", params)
    assert isinstance(router, GreedyRouter)
    router.attach_route_table(_fixed(*adapters), model_scope={MODEL})
    await router.start()
    return router


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter whose acquire timeout outlasts every test here."""
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


def _stream(router: GreedyRouter, options: RoutingRequestOptions | None = None, messages=MESSAGES):
    """Start one caller's stream in a task of its own; it returns the chunks."""

    async def run() -> list[str]:
        return [
            chunk
            async for chunk in router.stream_chat_completion(
                MODEL, messages, routing_options=options
            )
        ]

    return asyncio.create_task(run())


def _content(chunks: list[str]) -> str:
    text = ""
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        for choice in payload.get("choices") or []:
            text += (choice.get("delta") or {}).get("content") or ""
    return text


def _routing(chunks: list[str]) -> dict[str, Any]:
    """Merge a stream's routing frames as the serving layer does.

    A fallback-flagged frame starts a new attempt and replaces the keys before
    it; any other frame merges on top.
    """
    merged: dict[str, Any] = {}
    for chunk in chunks:
        routing = json.loads(chunk.removeprefix("data: ").strip()).get("_routing")
        if not isinstance(routing, dict):
            continue
        merged = dict(routing) if routing.get("fallback") else {**merged, **routing}
    return merged


def _availability(router: GreedyRouter, endpoint_id: str) -> float:
    """Return an endpoint's availability in the shared router's health, 1.0 when unseen."""
    return float(router.get_provider_status().get(endpoint_id, {}).get("availability", 1.0))


def _line() -> dict[str, int]:
    state = get_upstream_limiter().engine_snapshot()[ENGINE_ADDRESS]
    return {"pending": state["pending"], "waiting": state["waiting"]}


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def _busy_engine(router: GreedyRouter, engine: _Engine) -> asyncio.Task[list[str]]:
    """Take the engine's one place with a stream whose first token the test holds."""
    ahead = _stream(router)
    await engine.wait_sent(1)
    return ahead


# ------------------------------------------------------------ parameters


def test_the_cutoff_deadline_is_the_nimbus_baselines():
    cutoff = CutoffParams.model_validate(
        {
            "ttft_budget": {"fixed_ms": 2000, "per_token_ms": 0.5, "threshold_tokens": 1000},
            "cloud_ttft": {"fixed_ms": 400, "per_input_token_ms": 0.1},
            "margin_ms": 30,
        }
    )

    # 5000 tokens: budget 2000 + 0.5 * 4000 = 4000 ms, cloud 400 + 500 = 900 ms.
    assert cutoff_deadline(cutoff, 5000, 100.0) == pytest.approx(100.0 + (4000 - 900 - 30) / 1000)
    # Under the threshold the budget is the fixed part alone.
    assert cutoff_deadline(cutoff, 500, 0.0) == pytest.approx((2000 - 450 - 30) / 1000)


def test_a_cloud_that_cannot_meet_the_budget_gives_no_deadline():
    cutoff = CutoffParams.model_validate(
        {
            "ttft_budget": {"fixed_ms": 1000, "per_token_ms": 0},
            "cloud_ttft": {"fixed_ms": 1500, "per_input_token_ms": 0},
        }
    )

    assert cutoff_deadline(cutoff, 10, 0.0) is None


@pytest.mark.parametrize(
    "params",
    [
        {"cutoff": SHORT_CUTOFF, "max_wait_seconds": 1},
        {"engine_queue_limit": 0},
        {"max_wait_seconds": -1},
        {"max_wait_seconds": float("inf")},
        {"cloud_max_input_tokens": 0},
        {"unknown": 1},
    ],
)
def test_greedy_params_refuse_what_they_cannot_mean(params):
    with pytest.raises(ValidationError):
        validate_router_config("greedy", params)


# ------------------------------------------------------- engine queues


async def test_starting_registers_the_engine_and_stopping_clears_it():
    router = await _greedy(_Engine(), _Cloud(), engine_queue_limit=2)
    limiter = get_upstream_limiter()

    assert limiter.engine_queue_limit(ENGINE_URL) == 2
    assert limiter.engine_snapshot()[ENGINE_ADDRESS]["owners"] == [MODEL]
    assert await router.stop() is True
    assert limiter.engine_queue_limit(ENGINE_URL) is None


async def test_a_replaced_router_does_not_clear_its_successors_queues():
    engine, cloud = _Engine(), _Cloud()
    old = await _greedy(engine, cloud, engine_queue_limit=1)
    new = await _greedy(engine, cloud, engine_queue_limit=3)

    await old.stop()

    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) == 3
    await new.stop()
    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) is None


async def test_two_models_sharing_an_engine_must_give_it_one_limit():
    engine, cloud = _Engine(), _Cloud()
    fixed = _fixed(engine, cloud)
    fixed.register_route("m2", [(engine, 1.0), (cloud, 1.0)])
    first = build_router("greedy", {"engine_queue_limit": 1})
    first.attach_route_table(fixed, model_scope={MODEL})
    second = build_router("greedy", {"engine_queue_limit": 2})
    second.attach_route_table(fixed, model_scope={"m2"})
    await first.start()

    with pytest.raises(EngineQueueConflict):
        await second.start()

    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) == 1
    # A start that raised left nothing to stop.
    assert await second.stop() is False


async def test_a_route_edit_moves_the_engine_queue_with_the_route():
    engine, cloud = _Engine(), _Cloud()
    fixed = _fixed(engine, cloud)
    router = build_router("greedy", {})
    router.attach_route_table(fixed, model_scope={MODEL})
    await router.start()
    limiter = get_upstream_limiter()

    fixed.register_route(MODEL, [(cloud, 1.0)])
    router.refresh_route_table()
    assert limiter.engine_queue_limit(ENGINE_URL) is None

    fixed.register_route(MODEL, [(engine, 1.0), (cloud, 1.0)])
    router.refresh_route_table()
    assert limiter.engine_queue_limit(ENGINE_URL) == 1


async def test_a_route_edit_that_would_conflict_keeps_the_queues_and_raises_nothing():
    shared = _Engine()
    own = _Engine(port=18004, provider="engine-b")
    cloud = _Cloud()
    fixed = _fixed(shared, cloud)
    fixed.register_route("m2", [(own, 1.0), (cloud, 1.0)])
    first = build_router("greedy", {"engine_queue_limit": 1})
    first.attach_route_table(fixed, model_scope={MODEL})
    second = build_router("greedy", {"engine_queue_limit": 2})
    second.attach_route_table(fixed, model_scope={"m2"})
    await first.start()
    await second.start()
    limiter = get_upstream_limiter()

    # The edit brings the first model's engine into the second model's route.
    fixed.register_route("m2", [(own, 1.0), (shared, 1.0), (cloud, 1.0)])
    second.refresh_route_table()

    assert limiter.engine_queue_limit(ENGINE_URL) == 1
    assert limiter.engine_queue_limit(own.config.base_url) == 2
    assert limiter.engine_snapshot()[ENGINE_ADDRESS]["owners"] == [MODEL]


async def test_a_router_nobody_started_registers_its_engine_on_its_first_request():
    router = build_router("greedy", {})
    router.attach_route_table(_fixed(_Engine(), _Cloud()), model_scope={MODEL})

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["_routing"]["backend"] == "local"
    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) == 1


async def test_stopping_a_router_that_registered_on_its_first_request_clears_its_queues():
    router = build_router("greedy", {})
    router.attach_route_table(_fixed(_Engine(), _Cloud()), model_scope={MODEL})
    await router.chat_completion(MODEL, MESSAGES)
    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) == 1

    assert await router.stop() is True

    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) is None


async def test_engines_a_refresh_could_not_register_are_sent_nothing():
    engine, cloud = _Engine(), _Cloud()
    added = _Engine(port=18004, provider="engine-b")
    shared = _Engine(port=18005, provider="engine-c")
    fixed = _fixed(engine, cloud)
    fixed.register_route("m2", [(shared, 1.0), (cloud, 1.0)])
    router = build_router("greedy", {"engine_queue_limit": 1})
    router.attach_route_table(fixed, model_scope={MODEL})
    other = build_router("greedy", {"engine_queue_limit": 2})
    other.attach_route_table(fixed, model_scope={"m2"})
    await router.start()
    await other.start()
    # The edit brings in a new engine and one the other model gave another limit.
    fixed.register_route(MODEL, [(engine, 1.0), (added, 1.0), (shared, 1.0), (cloud, 1.0)])
    router.refresh_route_table()
    ahead = await _busy_engine(router, engine)

    chunks = await _stream(router)

    # Only the engine the last registration named may take it, and it is full.
    assert _content(chunks) == "cloud"
    assert (added.sent_count, shared.sent_count) == (0, 0)
    engine.answer(0)
    await ahead


async def test_a_cloud_route_replaced_while_the_request_waits_takes_it_at_its_deadline():
    engine, old_cloud = _Engine(), _Cloud()
    new_cloud = _Cloud(provider="cloud-new", endpoint_id=f"{MODEL}:cloud-new")
    fixed = _fixed(engine, old_cloud)
    router = build_router("greedy", {"cutoff": SHORT_CUTOFF})
    router.attach_route_table(fixed, model_scope={MODEL})
    await router.start()
    ahead = await _busy_engine(router, engine)

    behind = _stream(router)
    await _until(lambda: _line()["waiting"] == 1)
    fixed.register_route(MODEL, [(engine, 1.0), (new_cloud, 1.0)])
    router.refresh_route_table()

    chunks = await behind
    assert _routing(chunks)["provider"] == "cloud-new"
    assert old_cloud.calls == 0
    engine.answer(0)
    await ahead


async def test_a_cloud_route_edited_into_a_self_hosted_one_is_not_used_as_the_cloud():
    engine, cloud = _Engine(), _Cloud()
    fixed = _fixed(engine, cloud)
    router = build_router("greedy", {})
    router.attach_route_table(fixed, model_scope={MODEL})
    await router.start()
    ahead = await _busy_engine(router, engine)
    # The cloud route, edited in place into an engine nobody registered; the
    # pools have not followed the edit yet.
    edited = _Engine(port=18009, provider="engine-x", endpoint_id=CLOUD_ID)
    fixed.register_route(MODEL, [(engine, 1.0), (edited, 1.0)])

    behind = _stream(router)
    await _until(lambda: _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await behind) == "engine"
    assert edited.sent_count == 0
    await ahead


async def test_a_route_to_the_same_engine_by_another_url_keeps_its_queue():
    engine, cloud = _Engine(), _Cloud()
    fixed = _fixed(engine, cloud)
    router = build_router("greedy", {})
    router.attach_route_table(fixed, model_scope={MODEL})
    await router.start()
    # The same server under another path: one engine, as the limiter counts it.
    moved = _Engine(provider="engine-moved", endpoint_id=ENGINE_ID, path="/other/v1")
    fixed.register_route(MODEL, [(moved, 1.0), (cloud, 1.0)])

    stream = _stream(router)
    await moved.wait_sent(1)
    moved.answer(0)

    assert _content(await stream) == "engine"
    assert cloud.calls == 0


async def test_an_edit_the_pools_have_not_followed_yet_is_not_dispatched():
    engine, cloud = _Engine(), _Cloud()
    # The same engine under a new endpoint id: its queue is registered, but the
    # pools' range, derived when the table was last refreshed, does not name it.
    renamed = _Engine(provider="engine-b", endpoint_id=f"{MODEL}:local-renamed")
    fixed = _fixed(engine, cloud)
    router = build_router("greedy", {})
    router.attach_route_table(fixed, model_scope={MODEL})
    await router.start()

    fixed.register_route(MODEL, [(renamed, 1.0), (cloud, 1.0)])
    chunks = await _stream(router)

    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_LAST_RESORT
    assert renamed.sent_count == 0

    # Once the router follows the edit, the engine takes requests again.
    router.refresh_route_table()
    stream = _stream(router)
    await renamed.wait_sent(1)
    renamed.answer(0)
    assert _content(await stream) == "engine"


async def test_a_stopped_router_does_not_register_again():
    engine = _Engine()
    router = await _greedy(engine, _Cloud())
    await router.stop()

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert get_upstream_limiter().engine_queue_limit(ENGINE_URL) is None
    # It has no queue at its engine any more, so it sends the engine nothing.
    assert (resp["choices"][0]["message"]["content"], engine.chat_sent) == ("cloud", 0)


@pytest.mark.parametrize("missing", ["engine", "cloud"])
def test_a_model_needs_a_local_and_a_cloud_route(missing):
    router = build_router("greedy", {})
    fixed = _fixed(
        None if missing == "engine" else _Engine(), None if missing == "cloud" else _Cloud()
    )

    with pytest.raises(GreedyRoutingError, match=missing if missing == "cloud" else "self-hosted"):
        router.attach_route_table(fixed, model_scope={MODEL})


async def test_the_registry_builds_and_manages_a_greedy_model():
    fixed = _fixed(_Engine(), _Cloud())
    registry = ModelRouterRegistry(
        {MODEL: {"router": "greedy", "router_params": {"engine_queue_limit": 2}}},
        shared_fixed_router=fixed,
    )

    router = registry.get_router(MODEL)

    assert isinstance(router, GreedyRouter)
    assert router.model_id == MODEL
    assert router in registry.managed_routers()


# ------------------------------------------------------------ streaming


async def test_a_request_with_a_free_place_goes_to_the_engine():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)

    stream = _stream(router)
    await engine.wait_sent(1)
    engine.answer(0)

    chunks = await stream
    assert _content(chunks) == "engine"
    assert cloud.calls == 0
    assert "offload" not in _routing(chunks)


async def test_spillover_sends_a_request_that_finds_no_place_to_the_cloud_at_once():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    chunks = await _stream(router)

    assert _content(chunks) == "cloud"
    routing = _routing(chunks)
    assert (routing["offload"], routing["provider"], routing["fallback"]) == (
        OFFLOAD_SPILLOVER,
        "cloud",
        True,
    )
    # The log names the cloud route and nothing of the local frame before it,
    # and leaving the queue is the reason, not a failed attempt.
    assert (routing["endpoint_id"], routing["base_url"]) == (CLOUD_ID, CLOUD_URL)
    assert "failed_attempts" not in routing
    # The engine never saw it, and the request ahead was not touched.
    assert (engine.sent_count, engine.cancelled) == (1, 0)
    engine.answer(0)
    assert _content(await ahead) == "engine"
    # Never contacted, so nothing is held against the engine's health.
    assert _availability(router, ENGINE_ID) == 1.0


async def test_spillover_keeps_a_request_local_while_any_engine_has_a_place():
    busy = _Engine(provider="engine-a")
    free = _Engine(port=18004, provider="engine-b")
    cloud = _Cloud()
    router = await _greedy(busy, free, cloud)
    ahead = _stream(router, RoutingRequestOptions(pin_provider="engine-a"))
    await busy.wait_sent(1)

    # Whichever engine the pool picks first, the request ends on the free one.
    for index in range(4):
        stream = _stream(router)
        await free.wait_sent(index + 1)
        free.answer(index)
        assert _content(await stream) == "engine"
    assert (busy.sent_count, cloud.calls) == (1, 0)
    busy.answer(0)
    await ahead


async def test_cutoff_waits_for_a_place_until_its_deadline_then_goes_to_the_cloud():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud, cutoff=SHORT_CUTOFF)
    ahead = await _busy_engine(router, engine)

    started = time.monotonic()
    behind = _stream(router)
    await _until(lambda: _line()["waiting"] == 1)
    chunks = await behind

    assert time.monotonic() - started >= 0.09
    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_QUEUE_WAIT
    assert engine.sent_count == 1
    engine.answer(0)
    await ahead


async def test_cutoff_goes_local_when_a_place_frees_before_the_deadline():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(
        engine,
        cloud,
        cutoff={**SHORT_CUTOFF, "ttft_budget": {"fixed_ms": 6000, "per_token_ms": 0}},
    )
    ahead = await _busy_engine(router, engine)
    behind = _stream(router)
    await _until(lambda: _line()["waiting"] == 1)

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert (_content(await ahead), _content(await behind)) == ("engine", "engine")
    assert cloud.calls == 0


async def test_a_sent_request_is_never_taken_back_however_late_its_first_token():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud, cutoff=SHORT_CUTOFF)

    stream = _stream(router)
    await engine.wait_sent(1)
    # Twice the Cutoff's deadline: a sent request stays where it is.
    await asyncio.sleep(0.2)
    engine.answer(0)

    assert _content(await stream) == "engine"
    assert (engine.cancelled, cloud.calls) == (0, 0)


async def test_a_cloud_too_slow_for_the_budget_leaves_the_request_waiting_locally():
    engine, cloud = _Engine(), _Cloud()
    slow_cloud = {**SHORT_CUTOFF, "cloud_ttft": {"fixed_ms": 5000, "per_input_token_ms": 0}}
    router = await _greedy(engine, cloud, cutoff=slow_cloud)
    ahead = await _busy_engine(router, engine)
    behind = _stream(router)
    await _until(lambda: _line()["waiting"] == 1)
    await asyncio.sleep(0.15)
    assert not behind.done()

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await behind) == "engine"
    assert cloud.calls == 0
    await ahead


async def test_a_prompt_over_the_cloud_limit_waits_for_its_engine():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud, cloud_max_input_tokens=100)
    ahead = await _busy_engine(router, engine)
    behind = _stream(router, messages=LONG_MESSAGES)
    await _until(lambda: _line()["waiting"] == 1)

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await behind) == "engine"
    assert cloud.calls == 0
    await ahead


async def test_a_prompt_too_long_for_the_cloud_window_waits_for_its_engine():
    engine, cloud = _Engine(), _Cloud(context_length=100)
    router = await _greedy(engine, cloud, cutoff=SHORT_CUTOFF)
    ahead = await _busy_engine(router, engine)
    behind = _stream(router, messages=LONG_MESSAGES)
    await _until(lambda: _line()["waiting"] == 1)
    await asyncio.sleep(0.15)
    assert not behind.done()

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await behind) == "engine"
    assert cloud.calls == 0
    await ahead


async def test_a_request_held_to_the_local_routes_never_goes_to_the_cloud():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)
    local_only = RoutingRequestOptions(endpoint_scope=frozenset({ENGINE_ID}), affinity=False)

    behind = _stream(router, local_only)
    await _until(lambda: _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await behind) == "engine"
    assert cloud.calls == 0
    await ahead


async def test_a_local_failure_before_output_goes_to_the_cloud_once():
    engine, cloud = _Engine(fail_with=RuntimeError("engine down")), _Cloud()
    router = await _greedy(engine, cloud)

    chunks = await _stream(router)

    assert _content(chunks) == "cloud"
    routing = _routing(chunks)
    assert "offload" not in routing
    assert [attempt["backend"] for attempt in routing["failed_attempts"]] == ["local"]
    assert _availability(router, ENGINE_ID) < 1.0


async def test_a_request_the_cloud_then_fails_goes_back_to_its_engine_and_waits():
    engine, cloud = _Engine(), _Cloud(fail_with=RuntimeError("cloud down"))
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    behind = _stream(router)
    await _until(lambda: cloud.calls >= 1 and _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    chunks = await behind
    assert _content(chunks) == "engine"
    routing = _routing(chunks)
    assert (routing["provider"], routing["backend"]) == ("engine", "local")
    # Sent to the cloud, so offloaded whichever route answered, as the shared
    # router's offload says it.
    assert (routing["offload"], routing["offload_endpoint_id"]) == (OFFLOAD_SPILLOVER, CLOUD_ID)
    await ahead


async def test_the_offload_reason_survives_the_cloud_pool_moving_to_another_route():
    engine = _Engine()
    failing = _Cloud(
        fail_with=RuntimeError("cloud a down"), provider="cloud-a", endpoint_id=f"{MODEL}:cloud-a"
    )
    serving = _Cloud(provider="cloud-b", endpoint_id=f"{MODEL}:cloud-b")
    router = await _greedy(engine, failing, serving)
    ahead = await _busy_engine(router, engine)
    failing_first = RoutingRequestOptions(preferred_endpoint_id=f"{MODEL}:cloud-a")

    chunks = await _stream(router, failing_first)

    assert _content(chunks) == "cloud"
    routing = _routing(chunks)
    assert (routing["provider"], routing["offload"]) == ("cloud-b", OFFLOAD_SPILLOVER)
    assert [attempt["endpoint_id"] for attempt in routing["failed_attempts"]] == [
        f"{MODEL}:cloud-a"
    ]
    engine.answer(0)
    await ahead


async def test_a_client_that_stops_reading_frees_the_engine_place_without_the_collector():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    chunks = router.stream_chat_completion(MODEL, MESSAGES)
    while engine.sent_count == 0:
        await anext(chunks)
    assert _line()["pending"] == 1

    gc.disable()
    try:
        await chunks.aclose()  # type: ignore[attr-defined]
        await _until(lambda: _line()["pending"] == 0)
    finally:
        gc.enable()


@pytest.mark.parametrize("busy_first", [True, False], ids=["busy-first", "failing-first"])
async def test_an_engine_left_full_beside_one_that_failed_takes_the_request_back(
    monkeypatch, busy_first
):
    monkeypatch.setattr("random.random", lambda: 0.0)
    busy = _Engine(provider="engine-a")
    failing = _Engine(port=18004, provider="engine-b", fail_with=RuntimeError("engine b down"))
    cloud = _Cloud(fail_with=RuntimeError("cloud down"))
    engines = (busy, failing) if busy_first else (failing, busy)
    router = await _greedy(*engines, cloud)
    ahead = _stream(router, RoutingRequestOptions(pin_provider="engine-a"))
    await busy.wait_sent(1)

    behind = _stream(router)
    await _until(lambda: cloud.calls >= 1 and _line()["waiting"] == 1)
    busy.answer(0)
    await busy.wait_sent(2)
    busy.answer(1)

    chunks = await behind
    assert _content(chunks) == "engine"
    routing = _routing(chunks)
    assert (routing["offload"], routing["offload_endpoint_id"]) == (OFFLOAD_SPILLOVER, CLOUD_ID)
    # Back to the engine that was full, not to the one that failed on it.
    assert failing.sent_count == 1
    await ahead


async def test_a_request_two_engines_refused_is_not_sent_to_the_cloud(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)
    first = _Engine(provider="engine-a", fail_with=_BadRequest("unknown field"))
    second = _Engine(port=18004, provider="engine-b", fail_with=_BadRequest("unknown field"))
    cloud = _Cloud()
    router = await _greedy(first, second, cloud)

    with pytest.raises(_BadRequest):
        await _stream(router)

    # The local pool's walk ended at the shared bound; the cloud is not a third route.
    assert (first.sent_count, second.sent_count, cloud.calls) == (1, 1, 0)


async def test_a_cloud_route_sent_the_request_counts_however_its_failure_is_charged():
    engine = _Engine()
    throttled = _Cloud(
        provider="cloud-a",
        endpoint_id=f"{MODEL}:cloud-a",
        unsent_with=UpstreamSaturated("no outbound slot"),
    )
    timed_out = _Cloud(
        provider="cloud-b",
        endpoint_id=f"{MODEL}:cloud-b",
        fail_with=UpstreamRequestTimedOut(
            request_info=SimpleNamespace(real_url=CLOUD_URL),  # type: ignore[arg-type]
            history=(),
            status=504,
            message="upstream_timeout",
        ),
    )
    router = await _greedy(engine, throttled, timed_out)
    ahead = await _busy_engine(router, engine)
    throttled_first = RoutingRequestOptions(preferred_endpoint_id=f"{MODEL}:cloud-a")

    behind = _stream(router, throttled_first)
    await _until(lambda: timed_out.calls >= 1 and _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    # Route b answered 504 after it was sent: the request was offloaded, to b.
    routing = _routing(await behind)
    assert (routing["offload"], routing["offload_endpoint_id"]) == (
        OFFLOAD_SPILLOVER,
        f"{MODEL}:cloud-b",
    )
    await ahead


async def test_a_malformed_request_is_not_sent_back_to_a_full_engine(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)
    busy = _Engine(provider="engine-a")
    refusing = _Engine(port=18004, provider="engine-b", fail_with=_BadRequest("unknown field"))
    cloud = _Cloud(fail_with=_BadRequest("unknown field"))
    router = await _greedy(busy, refusing, cloud)
    ahead = _stream(router, RoutingRequestOptions(pin_provider="engine-a"))
    await busy.wait_sent(1)

    async with asyncio.timeout(2):
        with pytest.raises(_BadRequest) as caught:
            await _stream(router)

    # Two routes refused it: the full engine is not waited for a third time.
    assert caught.value.status == 400
    assert (busy.sent_count, cloud.calls) == (1, 1)
    busy.answer(0)
    await ahead


async def test_a_prompt_too_long_for_the_engine_window_goes_straight_to_the_cloud():
    engine, cloud = _Engine(context_length=100), _Cloud()
    router = await _greedy(engine, cloud)

    chunks = await _stream(router, messages=LONG_MESSAGES)

    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_LAST_RESORT
    assert engine.sent_count == 0


async def test_a_cloud_route_too_small_for_the_prompt_is_never_tried():
    engine = _Engine()
    small = _Cloud(provider="cloud-small", endpoint_id=f"{MODEL}:cloud-small", context_length=100)
    large = _Cloud(provider="cloud-large", endpoint_id=f"{MODEL}:cloud-large")
    router = await _greedy(engine, small, large)
    ahead = await _busy_engine(router, engine)
    prefer_small = RoutingRequestOptions(preferred_endpoint_id=f"{MODEL}:cloud-small")

    chunks = await _stream(router, prefer_small, messages=LONG_MESSAGES)

    assert _routing(chunks)["provider"] == "cloud-large"
    assert small.calls == 0
    engine.answer(0)
    await ahead


async def test_a_prompt_no_window_fits_is_refused_with_why():
    router = await _greedy(_Engine(context_length=100), _Cloud(context_length=100))

    with pytest.raises(AllCircuitsOpenError, match="context window"):
        await router.chat_completion(MODEL, LONG_MESSAGES)


async def test_a_cloud_that_never_left_the_gateway_does_not_mark_the_request_offloaded():
    engine, cloud = _Engine(), _Cloud(unsent_with=UpstreamSaturated("no outbound slot"))
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    behind = _stream(router)
    await _until(lambda: cloud.calls >= 1 and _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    chunks = await behind
    assert _content(chunks) == "engine"
    assert "offload" not in _routing(chunks)
    await ahead


async def test_a_cloud_stream_that_breaks_after_its_output_keeps_the_offload_reason():
    engine, cloud = _Engine(), _Cloud()
    cloud.fail_after_output = RuntimeError("cloud dropped the stream")
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    with pytest.raises(RuntimeError, match="dropped") as caught:
        await _stream(router)

    routing = caught.value._routing  # type: ignore[attr-defined]
    assert routing["offload"] == OFFLOAD_SPILLOVER
    engine.answer(0)
    await ahead


async def test_a_range_named_by_provider_reaches_the_engine_it_names():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    by_provider = RoutingRequestOptions(endpoint_scope=frozenset({"engine"}), affinity=False)

    stream = _stream(router, by_provider)
    await engine.wait_sent(1)
    engine.answer(0)

    assert _content(await stream) == "engine"
    assert cloud.calls == 0


async def test_a_fixed_wait_holds_a_request_the_same_time_before_the_cloud():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud, max_wait_seconds=0.1)
    ahead = await _busy_engine(router, engine)

    started = time.monotonic()
    chunks = await _stream(router)

    assert time.monotonic() - started >= 0.09
    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_QUEUE_WAIT
    engine.answer(0)
    await ahead


async def test_a_request_pinned_to_a_full_engine_waits_for_its_place():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    pinned = _stream(router, RoutingRequestOptions(pin_provider="engine"))
    await _until(lambda: _line()["waiting"] == 1)
    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await pinned) == "engine"
    assert cloud.calls == 0
    await ahead


async def test_a_model_whose_engine_no_longer_has_a_route_goes_to_the_cloud():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    cloud_only = RoutingRequestOptions(endpoint_scope=frozenset({CLOUD_ID}))

    chunks = await _stream(router, cloud_only)

    assert _content(chunks) == "cloud"
    assert _routing(chunks)["offload"] == OFFLOAD_LAST_RESORT
    assert engine.sent_count == 0


# -------------------------------------------------------- non-streaming


async def test_a_non_streaming_request_spills_over_with_its_reason():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "cloud"
    assert (resp["_routing"]["offload"], resp["_routing"]["backend"]) == (
        OFFLOAD_SPILLOVER,
        "cloud",
    )
    assert engine.chat_sent == 0
    engine.answer(0)
    await ahead


async def test_a_non_streaming_request_with_a_free_place_is_served_locally():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "engine"
    assert resp["_routing"]["backend"] == "local"
    assert "offload" not in resp["_routing"]
    assert get_upstream_limiter().engine_snapshot()[ENGINE_ADDRESS]["released_answered"] == 1


async def test_a_pinned_request_is_dispatched_by_the_shared_router_as_pinned():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)

    resp = await router.chat_completion(
        MODEL, MESSAGES, routing_options=RoutingRequestOptions(pin_provider="cloud")
    )

    assert resp["choices"][0]["message"]["content"] == "cloud"
    assert "offload" not in resp["_routing"]
    assert engine.chat_sent == 0


async def test_a_non_streaming_request_the_cloud_fails_is_served_by_its_engine_as_offloaded():
    engine, cloud = _Engine(), _Cloud(fail_with=RuntimeError("cloud down"))
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)

    behind = asyncio.create_task(router.chat_completion(MODEL, MESSAGES))
    await _until(lambda: cloud.calls >= 1 and _line()["waiting"] == 1)
    engine.answer(0)
    resp = await behind

    assert resp["choices"][0]["message"]["content"] == "engine"
    routing = resp["_routing"]
    assert (routing["backend"], routing["offload"], routing["offload_endpoint_id"]) == (
        "local",
        OFFLOAD_SPILLOVER,
        CLOUD_ID,
    )
    await ahead


async def test_a_request_that_fails_everywhere_after_offloading_is_logged_as_offloaded():
    engine, cloud = _Engine(), _Cloud(fail_with=RuntimeError("cloud down"))
    router = await _greedy(engine, cloud)
    ahead = await _busy_engine(router, engine)
    engine.fail_with = RuntimeError("engine down")

    behind = asyncio.create_task(router.chat_completion(MODEL, MESSAGES))
    await _until(lambda: cloud.calls >= 1 and _line()["waiting"] == 1)
    engine.answer(0)

    with pytest.raises(RuntimeError) as caught:
        await behind
    routing = caught.value._routing  # type: ignore[attr-defined]
    assert (routing["offload"], routing["offload_endpoint_id"]) == (OFFLOAD_SPILLOVER, CLOUD_ID)
    await ahead


async def test_a_request_no_route_in_its_range_can_take_is_refused_as_the_shared_router_refuses():
    router = await _greedy(_Engine(), _Cloud())
    nowhere = RoutingRequestOptions(endpoint_scope=frozenset({"elsewhere"}))

    with pytest.raises(AllCircuitsOpenError, match="inside the dispatch scope"):
        await router.chat_completion(MODEL, MESSAGES, routing_options=nowhere)


async def test_a_prompt_too_long_for_the_only_route_in_its_range_is_refused():
    router = await _greedy(_Engine(), _Cloud(), cloud_max_input_tokens=100)
    cloud_only = RoutingRequestOptions(endpoint_scope=frozenset({CLOUD_ID}))

    with pytest.raises(AllCircuitsOpenError, match="cloud_max_input_tokens"):
        await router.chat_completion(MODEL, LONG_MESSAGES, routing_options=cloud_only)


# -------------------------------------------------------------- feedback


async def test_feedback_goes_to_the_shared_router():
    engine, cloud = _Engine(), _Cloud()
    router = await _greedy(engine, cloud)
    heard: list[Any] = []
    router._fixed.record_observation = heard.append  # type: ignore[method-assign]

    router.record_observation("observation")  # type: ignore[arg-type]

    assert heard == ["observation"]
