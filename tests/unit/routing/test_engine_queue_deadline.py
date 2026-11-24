"""A caller's engine-queue deadline in FixedRouter (``RoutingRequestOptions.engine_queue_deadline``).

A caller that decides itself where a request goes when it has waited too long
hands FixedRouter the deadline. FixedRouter gives it to every attempt it sends to a
self-hosted engine, and runs none of its own queue-wait offload for the request: no
offload deadline, no first-token wait, no engine hold, no offload route in its
fallback. The engine here is registered with the limiter, so its queue counts every
request, and each request's first token is released by the test.

The model also has a queue-wait offload policy with a short wait, so a test fails
if any of the offload machinery runs for a request that carries a deadline.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.offload import OffloadPolicy
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    UpstreamQueueWaitExpired,
    get_upstream_limiter,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
KEY = "EMPTY"
ENGINE_URL = "http://localhost:18003/v1"
ENGINE_ADDRESS = "localhost:18003"
ENGINE_ID = f"{MODEL}:local-18003"
CLOUD_ID = f"{MODEL}:cloud-api"
#: The offload policy's wait: an engine that takes longer than this to answer
#: would be cut short if the router's first-token wait were armed.
OFFLOAD_WAIT_S = 0.05


class _Engine(BaseAdapter):
    """A local engine whose every request waits for the test to release its first token.

    Records what each call found in the request context, and gives its place in
    the engine's queue up through its slot when it answers, as the real adapters do.
    """

    def __init__(self) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="engine",
                base_url=ENGINE_URL,
                endpoint_id=ENGINE_ID,
            )
        )
        self.terms: list[dict[str, Any]] = []
        self.answers: list[asyncio.Event] = []
        self.chat_sent = 0
        self.cancelled = 0

    def _record_terms(self) -> None:
        ctx = req_ctx.get()
        self.terms.append(
            {
                "deadline": ctx.get(req_ctx.UPSTREAM_QUEUE_DEADLINE),
                "hold": ctx.get(req_ctx.UPSTREAM_ENGINE_HOLD),
                "watch": getattr(ctx.get(req_ctx.UPSTREAM_DISPATCH_WATCH), "watch", None),
            }
        )

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
        self._record_terms()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            self.chat_sent += 1
            return self.format_response(content="engine", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self._record_terms()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url) as slot:
            answered = asyncio.Event()
            self.answers.append(answered)
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
    """The model's cloud route, which is also its offload route."""

    def __init__(self) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="cloud",
                base_url="https://cloud.example/v1",
                endpoint_id=CLOUD_ID,
                route_metadata={"route_id": "offload"},
            )
        )
        self.calls = 0
        self.deadlines: list[Any] = []

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.calls += 1
        self.deadlines.append(req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE))
        return self.format_response(content="cloud", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        self.deadlines.append(req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE))
        yield _chunk({"content": "cloud"})
        yield _chunk({}, finish_reason="stop")


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


class _Policies:
    def __init__(self, policy: OffloadPolicy) -> None:
        self.policy = policy

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policy if model_id == MODEL else None


def _router(engine: _Engine, cloud: _Cloud) -> FixedRouter:
    policy = OffloadPolicy(route_id="offload", wait_seconds=OFFLOAD_WAIT_S, engine_queue_limit=5)
    router = FixedRouter(offload_policy_resolver=_Policies(policy))
    router.register_route(MODEL, [(engine, 1.0), (cloud, 1.0)])
    return router


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter, with the engine registered at a limit of one."""
    limiter = UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    limiter.set_engine_queues(MODEL, {ENGINE_URL: 1})
    reset_upstream_limiter(limiter)
    yield
    reset_upstream_limiter()


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    """Make the weighted draw pick the first candidate, so the engine is the primary."""
    monkeypatch.setattr("random.random", lambda: 0.0)


def _local(deadline_in: float) -> RoutingRequestOptions:
    """Options as a caller delegating to the model's local pool sets them."""
    return RoutingRequestOptions(
        endpoint_scope=frozenset({ENGINE_ID}),
        engine_queue_deadline=time.monotonic() + deadline_in,
    )


def _stream(router: FixedRouter, options: RoutingRequestOptions | None) -> asyncio.Task[str]:
    """Start one pro caller's stream in a task of its own; it returns the content."""

    async def run() -> str:
        with req_ctx.push(**{req_ctx.USER_ROLE: "pro"}):
            chunks = [
                chunk
                async for chunk in router.stream_chat_completion(
                    MODEL, MESSAGES, routing_options=options
                )
            ]
        return _content(chunks)

    return asyncio.create_task(run())


def _content(chunks: list[str]) -> str:
    text = ""
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        for choice in payload.get("choices") or []:
            text += (choice.get("delta") or {}).get("content") or ""
    return text


def _line() -> dict[str, int]:
    state = get_upstream_limiter().engine_snapshot()[ENGINE_ADDRESS]
    return {"pending": state["pending"], "waiting": state["waiting"]}


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


# ------------------------------------------------------------ the option


@pytest.mark.parametrize("deadline", [math.nan, math.inf, True, "soon"])
def test_a_deadline_that_is_not_a_time_is_refused_before_any_attempt(deadline):
    with pytest.raises(ValueError, match="engine_queue_deadline"):
        RoutingRequestOptions(engine_queue_deadline=deadline)


# ------------------------------------------------------------ streaming


async def test_a_local_attempt_carries_the_callers_deadline_and_no_offload_terms():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    options = _local(5)

    stream = _stream(router, options)
    await engine.wait_sent(1)
    engine.answer(0)

    assert await stream == "engine"
    assert engine.terms == [
        {"deadline": options.engine_queue_deadline, "hold": None, "watch": None}
    ]


async def test_a_sent_stream_is_never_cut_short_however_late_its_first_token():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    # The whole route, offload route included, so the offload policy could arm
    # a first-token wait if anything consulted it.
    options = RoutingRequestOptions(engine_queue_deadline=time.monotonic() + 5)

    stream = _stream(router, options)
    await engine.wait_sent(1)
    # Several times the offload policy's wait: a first-token wait would have
    # cancelled the attempt and sent the request to the offload route by now.
    await asyncio.sleep(OFFLOAD_WAIT_S * 4)
    engine.answer(0)

    assert await stream == "engine"
    assert (engine.cancelled, cloud.calls) == (0, 0)
    assert engine.terms[0]["watch"] is None


async def test_a_spent_deadline_leaves_a_full_engine_unsent():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    ahead = _stream(router, _local(5))
    await engine.wait_sent(1)

    with pytest.raises(UpstreamQueueWaitExpired):
        await _stream(router, _local(-1))

    # The engine never saw the second request, and no offload route took it.
    assert (engine.sent_count, cloud.calls) == (1, 0)
    assert _line() == {"pending": 1, "waiting": 0}
    engine.answer(0)
    assert await ahead == "engine"


async def test_a_request_waits_for_a_seat_until_its_deadline_and_then_leaves():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    ahead = _stream(router, _local(5))
    await engine.wait_sent(1)

    behind = _stream(router, _local(0.1))
    await _until(lambda: _line() == {"pending": 1, "waiting": 1})
    with pytest.raises(UpstreamQueueWaitExpired):
        await behind

    assert (engine.sent_count, cloud.calls) == (1, 0)
    engine.answer(0)
    assert await ahead == "engine"


async def test_a_waiting_request_goes_local_when_a_seat_frees_in_time():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    ahead = _stream(router, _local(5))
    await engine.wait_sent(1)
    behind = _stream(router, _local(5))
    await _until(lambda: _line() == {"pending": 1, "waiting": 1})

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert (await ahead, await behind) == ("engine", "engine")
    assert cloud.calls == 0


async def test_the_deadline_does_not_bound_a_cloud_attempt():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    options = RoutingRequestOptions(
        endpoint_scope=frozenset({CLOUD_ID}),
        engine_queue_deadline=time.monotonic() - 1,
    )

    assert await _stream(router, options) == "cloud"
    assert cloud.deadlines == [None]


async def test_with_a_deadline_the_offload_route_is_an_ordinary_candidate(monkeypatch):
    # A draw that lands on the last candidate, which is the offload route.
    monkeypatch.setattr("random.random", lambda: 0.99)
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)

    # Without a deadline the policy holds its offload route back from selection...
    held_back = _stream(router, None)
    await engine.wait_sent(1)
    engine.answer(0)
    assert await held_back == "engine"

    # ...and with one there is no offload route, so the draw stands. (Sent to
    # the engine instead, it would wait for an answer this test never gives.)
    options = RoutingRequestOptions(engine_queue_deadline=time.monotonic() + 5)
    async with asyncio.timeout(2):
        assert await _stream(router, options) == "cloud"
    assert engine.sent_count == 1


async def test_without_a_deadline_the_offload_policy_still_holds_the_stream():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)

    stream = _stream(router, None)
    await engine.wait_sent(1)
    engine.answer(0)

    assert await stream == "engine"
    (terms,) = engine.terms
    assert terms["hold"] is not None
    assert terms["hold"].limit == 5


# -------------------------------------------------------- non-streaming


async def test_a_non_streaming_request_with_a_spent_deadline_leaves_a_full_engine_unsent():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    ahead = _stream(router, _local(5))
    await engine.wait_sent(1)

    with pytest.raises(UpstreamQueueWaitExpired):
        await router.chat_completion(MODEL, MESSAGES, routing_options=_local(-1))

    assert (engine.chat_sent, cloud.calls) == (0, 0)
    engine.answer(0)
    assert await ahead == "engine"


async def test_a_non_streaming_request_is_counted_and_carries_the_deadline():
    engine, cloud = _Engine(), _Cloud()
    router = _router(engine, cloud)
    options = _local(5)

    resp = await router.chat_completion(MODEL, MESSAGES, routing_options=options)

    assert resp["choices"][0]["message"]["content"] == "engine"
    assert engine.terms == [
        {"deadline": options.engine_queue_deadline, "hold": None, "watch": None}
    ]
    state = get_upstream_limiter().engine_snapshot()[ENGINE_ADDRESS]
    assert (state["sent_at_once"], state["released_answered"], state["pending"]) == (1, 1, 0)
