"""Engine-stall offload in FixedRouter (``routing.engine_stall``).

The fake engines here take an outbound slot from the process-wide limiter the way
the real adapters do, so the limiter's reports reach the router's first-token
watch. Most are local endpoints, which the limiter exempts -- the case the
feature exists for: a vLLM or SGLang server queues internally, where the gateway
cannot see it.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from routing.engine_stall import FirstTokenWatch
from routing.offload import (
    OFFLOAD_ENGINE_STALLED,
    OFFLOAD_ENGINE_WAIT,
    OFFLOAD_QUEUE_WAIT,
    OffloadPolicy,
)
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
KEY = "shared-key"


class _Engine(BaseAdapter):
    """An upstream that sends its first token only once ``gate`` is set.

    Sends a role-only delta first when ``prelude`` is on, as vLLM and SGLang do.
    Records every call's first-token watch, and every stream that was cancelled
    while it waited -- which is what aborts a request at a real engine.
    """

    def __init__(
        self,
        provider: str,
        *,
        route_id: str | None = None,
        answered: bool = False,
        delay: float | None = None,
        prelude: bool = True,
        local: bool = True,
        fail_with: BaseException | None = None,
    ) -> None:
        base_url = "http://localhost:8000/v1" if local else f"https://{provider}.example/v1"
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=provider,
                base_url=base_url,
                endpoint_id=f"{MODEL}:{provider}-api",
                route_metadata={"route_id": route_id} if route_id else {},
            )
        )
        self.gate = asyncio.Event()
        if answered:
            self.gate.set()
        self.delay = delay
        self.prelude = prelude
        self.fail_with = fail_with
        self.calls = 0
        self.cancelled = 0
        self.watches: list[Any] = []

    def _record(self) -> None:
        self.calls += 1
        self.watches.append(req_ctx.get().get(req_ctx.UPSTREAM_DISPATCH_WATCH))

    async def _first_token(self) -> None:
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        await self.gate.wait()

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self._record()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            await self._first_token()
            return self.format_response(content=self.config.provider, model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self._record()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            if self.prelude:
                yield _chunk({"role": "assistant"})
            try:
                await self._first_token()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            yield _chunk({"content": self.config.provider})


def _chunk(delta: dict[str, str]) -> str:
    payload = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta}]}
    return f"data: {json.dumps(payload)}\n\n"


class _Policies:
    def __init__(self, policies: dict[str, OffloadPolicy]) -> None:
        self.policies = policies

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policies.get(model_id)


def _router(
    routes: list[tuple[BaseAdapter, float]],
    policy: OffloadPolicy | None = None,
) -> FixedRouter:
    router = FixedRouter(offload_policy_resolver=_Policies({MODEL: policy} if policy else {}))
    router.register_route(MODEL, routes)
    return router


def _policy(wait_seconds: float = 0.05) -> OffloadPolicy:
    return OffloadPolicy(route_id="offload", wait_seconds=wait_seconds)


def _stall(router: FixedRouter, endpoint_id: str, *, still_waiting: bool) -> None:
    """Stall an endpoint the way an expired attempt does, optionally leaving one behind."""
    tracker = router.engine_stalls
    if still_waiting:
        tracker.begin(endpoint_id)
    tracker.expired(endpoint_id, tracker.begin(endpoint_id), model_id=MODEL, wait_seconds=1.0)


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter with room to spare, so no test queues by accident."""
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    """Make the weighted draw pick the first candidate, so the primary is known."""
    monkeypatch.setattr("random.random", lambda: 0.0)


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


def _payloads(chunks: list[str]) -> list[dict[str, Any]]:
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]


def _content(chunks: list[str]) -> str:
    return "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in _payloads(chunks)
        for choice in payload.get("choices") or []
    )


def _roles(chunks: list[str]) -> list[str]:
    return [
        choice["delta"]["role"]
        for payload in _payloads(chunks)
        for choice in payload.get("choices") or []
        if "role" in (choice.get("delta") or {})
    ]


def _routing(chunks: list[str]) -> list[dict[str, Any]]:
    return [payload["_routing"] for payload in _payloads(chunks) if "_routing" in payload]


# ------------------------------------------------------ the first-token wait


async def test_a_stream_with_no_first_token_is_cancelled_and_offloaded():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True, prelude=False)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    # The engine's role-only delta was held back, so it never reached the client.
    assert _roles(chunks) == []
    first_attempt, offload_attempt = _routing(chunks)
    assert "offload" not in first_attempt
    assert offload_attempt["offload"] == OFFLOAD_ENGINE_WAIT
    assert offload_attempt["fallback"] is True
    (failure,) = offload_attempt["failed_attempts"]
    assert failure["error_type"] == "EngineWaitExpired"
    assert failure["endpoint_id"] == "m:engine-api"
    # Closing the stream is what aborts the request at a real engine.
    assert engine.cancelled == 1
    assert router.engine_stalls.is_stalled("m:engine-api")
    # A busy engine is not a broken one: the circuit is untouched.
    assert router.endpoint_health_registry.allow_request("m:engine-api")


async def test_a_first_token_in_time_goes_out_with_what_came_before_it():
    engine = _Engine("engine", answered=True, delay=0.01)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=1.0))

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _roles(chunks) == ["assistant"]
    assert _content(chunks) == "engine"
    assert [block.get("offload") for block in _routing(chunks)] == [None]
    assert reserved.calls == 0
    assert router.engine_stalls._endpoints == {}


async def test_only_attempts_on_other_routes_are_watched():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    await router.chat_completion(MODEL, MESSAGES)

    (engine_watch,) = engine.watches
    assert isinstance(engine_watch, FirstTokenWatch)
    assert engine_watch.wait_seconds == 0.05
    # The offload attempt has nowhere left to go, and a non-stream nothing to watch.
    assert reserved.watches == [None, None]


async def test_time_in_the_gateway_queue_does_not_count_against_the_engine():
    """The watch pauses while the request waits for a slot and restarts when it is sent."""
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=10.0)
    reset_upstream_limiter(limiter)
    engine = _Engine("engine", answered=True, delay=0.3, local=False)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.5))
    held = await limiter.acquire("engine", KEY, base_url=engine.config.base_url)
    asyncio.get_running_loop().call_later(0.3, lambda: held.release(status_code=200))

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    # 0.3s queued plus 0.3s at the engine is past the wait, but neither part is.
    assert _content(chunks) == "engine"
    assert reserved.calls == 0


async def test_a_wait_that_ends_in_the_gateway_queue_is_not_a_stall():
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=10.0)
    reset_upstream_limiter(limiter)
    engine = _Engine("engine", answered=True, local=False)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))
    held = await limiter.acquire("engine", KEY, base_url=engine.config.base_url)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    assert _routing(chunks)[-1]["offload"] == OFFLOAD_QUEUE_WAIT
    assert not router.engine_stalls.is_stalled("m:engine-api")
    held.release(status_code=200)


async def test_a_non_streaming_request_is_never_cut_short():
    engine = _Engine("engine", answered=True, delay=0.1)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.02))

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "engine"
    assert router.engine_stalls._endpoints == {}


async def test_without_an_offload_route_nothing_is_watched():
    engine = _Engine("engine", answered=True, delay=0.05)
    other = _Engine("other", answered=True)
    router = _router([(engine, 1.0), (other, 1.0)])

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    assert engine.watches == [None]
    assert router.engine_stalls._endpoints == {}


async def test_a_pinned_stream_waits_as_long_as_its_engine_needs():
    engine = _Engine("engine", answered=True, delay=0.1)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.02))

    chunks = await _collect(
        router.stream_chat_completion(
            MODEL,
            MESSAGES,
            routing_options=RoutingRequestOptions(pin_provider="m:engine-api"),
        )
    )

    assert _content(chunks) == "engine"
    assert engine.watches == [None]


async def test_a_client_that_hangs_up_while_waiting_leaves_nothing_behind():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=5.0))

    task = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    await asyncio.sleep(0.02)
    assert router.engine_stalls._endpoints["m:engine-api"].pending
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine.cancelled == 1
    assert reserved.calls == 0
    assert router.engine_stalls._endpoints == {}


# ------------------------------------------------------- stalled endpoints


async def test_new_requests_go_around_an_engine_that_still_holds_one_of_ours():
    engine = _Engine("engine", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=True)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    resp = await router.chat_completion(MODEL, MESSAGES)

    assert _content(chunks) == "reserved"
    assert _routing(chunks)[0]["offload"] == OFFLOAD_ENGINE_STALLED
    assert resp["_routing"]["offload"] == OFFLOAD_ENGINE_STALLED
    assert engine.calls == 0


async def test_going_straight_to_the_offload_route_is_logged(caplog):
    engine = _Engine("engine", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=True)

    with caplog.at_level("INFO", logger="routing.routers"):
        await _collect(router.stream_chat_completion(MODEL, MESSAGES))
        await router.chat_completion(MODEL, MESSAGES)

    offloads = [r for r in caplog.records if r.getMessage() == "route_offload"]
    assert [(r.reason, r.endpoint_id) for r in offloads] == [
        (OFFLOAD_ENGINE_STALLED, "m:reserved-api"),
        (OFFLOAD_ENGINE_STALLED, "m:reserved-api"),
    ]


async def test_another_ordinary_route_takes_the_traffic_first():
    engine = _Engine("engine", answered=True)
    sibling = _Engine("sibling", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (sibling, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=True)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "sibling"
    assert "offload" not in _routing(chunks)[0]
    assert engine.calls == reserved.calls == 0


async def test_a_stalled_engine_with_nothing_of_ours_on_it_takes_a_streaming_probe():
    engine = _Engine("engine", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=False)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    assert not router.engine_stalls.is_stalled("m:engine-api")


async def test_a_non_streaming_request_never_probes():
    engine = _Engine("engine", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=False)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "reserved"
    assert resp["_routing"]["offload"] == OFFLOAD_ENGINE_STALLED
    assert engine.calls == 0
    assert router.engine_stalls.is_stalled("m:engine-api")


async def test_one_probe_at_a_time():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=5.0))
    _stall(router, "m:engine-api", still_waiting=False)

    probe = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    await asyncio.sleep(0.02)
    while_probing = await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    engine.gate.set()

    assert _content(while_probing) == "reserved"
    assert _content(await probe) == "engine"
    assert engine.calls == 1
    assert not router.engine_stalls.is_stalled("m:engine-api")


async def test_a_probe_that_gets_no_token_either_is_offloaded_and_the_stall_stands():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))
    _stall(router, "m:engine-api", still_waiting=False)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    assert _routing(chunks)[-1]["offload"] == OFFLOAD_ENGINE_WAIT
    assert router.engine_stalls.is_stalled("m:engine-api")


async def test_a_stall_ends_when_an_attempt_already_on_the_engine_answers():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=5.0))

    waiting = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    await asyncio.sleep(0.02)
    # Some other request on this engine waited out its budget meanwhile.
    _stall(router, "m:engine-api", still_waiting=False)
    assert router.engine_stalls.is_stalled("m:engine-api")
    engine.gate.set()

    assert _content(await waiting) == "engine"
    assert not router.engine_stalls.is_stalled("m:engine-api")


async def test_a_stalled_fallback_is_tried_only_after_the_offload_route():
    primary = _Engine("primary", fail_with=RuntimeError("upstream 500"))
    stalled = _Engine("stalled", answered=True)
    reserved = _Engine("reserved", route_id="offload", fail_with=RuntimeError("upstream 502"))
    router = _router([(primary, 1.0), (stalled, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:stalled-api", still_waiting=True)

    resp = await router.chat_completion(MODEL, MESSAGES)
    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert resp["choices"][0]["message"]["content"] == "stalled"
    assert [attempt["endpoint_id"] for attempt in resp["_routing"]["failed_attempts"]] == [
        "m:primary-api",
        "m:reserved-api",
    ]
    assert _content(chunks) == "stalled"


async def test_a_stalled_route_still_serves_when_the_offload_route_cannot():
    engine = _Engine("engine", answered=True)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:engine-api", still_waiting=True)
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:reserved-api", reason="test")

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    # With nowhere to offload it, the attempt had no first-token deadline.
    (watch,) = engine.watches
    assert watch.wait_seconds is None


def test_eligible_adapters_list_a_stalled_route_after_the_offload_route():
    a = _Engine("a")
    b = _Engine("b")
    reserved = _Engine("reserved", route_id="offload")
    router = _router([(a, 1.0), (b, 1.0), (reserved, 1.0)], _policy())
    _stall(router, "m:a-api", still_waiting=False)

    order = [adapter for adapter, _ in router.eligible_adapters(MODEL)]

    # A surface that sends one unwatched request never uses it as a probe.
    assert order == [b, reserved, a]
