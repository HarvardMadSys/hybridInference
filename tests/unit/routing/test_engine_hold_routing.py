"""The engine queue limit in FixedRouter: a local engine's queue, held in the gateway.

Under an engine queue limit of N, a model's local engine is sent at most N streaming
requests that have not returned a first token; the rest wait in the gateway
(``upstream_limiter.EngineHold``). The fake engine here takes its outbound slot from
the process-wide limiter and reports its first output the way the real adapters do,
and each request's first token is released by the test, so the engine's queue is
exactly what the test says it is.

The offload route refuses free callers (as a route whose only key is reserved for
pro does), which is how a test makes one request unable to leave: a free caller gets
no deadline and no first-token watch, and so holds its place for as long as the test
wants.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from routing.offload import OFFLOAD_LAST_RESORT, OFFLOAD_QUEUE_WAIT, OffloadPolicy
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import (
    EngineHold,
    UpstreamConcurrencyLimiter,
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

#: A held request that could be offloaded leaves at the wait; the first-token wait
#: that follows a send is enforced with ``asyncio.timeout`` (Python 3.11+).
needs_timeout = pytest.mark.skipif(
    not hasattr(asyncio, "timeout"), reason="the first-token wait needs asyncio.timeout"
)


class _Engine(BaseAdapter):
    """A local engine whose every request waits for the test to release its first token.

    ``sent`` counts the requests that got past the gateway -- the ones the engine
    has seen. ``holds`` records each call's engine hold, and ``answers`` each sent
    request's first-token event, in the order they were sent.
    """

    def __init__(self, *, keep_open: bool = False) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="engine",
                base_url=ENGINE_URL,
                endpoint_id=f"{MODEL}:local-18003",
            )
        )
        self.keep_open = keep_open
        self.holds: list[Any] = []
        self.answers: list[asyncio.Event] = []
        self.finish = asyncio.Event()
        self.cancelled = 0

    @property
    def sent_count(self) -> int:
        return len(self.answers)

    async def wait_sent(self, count: int) -> None:
        while self.sent_count < count:
            await asyncio.sleep(0.005)

    def answer(self, index: int) -> None:
        self.answers[index].set()

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.holds.append(req_ctx.get().get(req_ctx.UPSTREAM_ENGINE_HOLD))
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            return self.format_response(content="engine", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.holds.append(req_ctx.get().get(req_ctx.UPSTREAM_ENGINE_HOLD))
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            answered = asyncio.Event()
            self.answers.append(answered)
            yield _chunk({"role": "assistant"})
            try:
                await answered.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            report_first_token()
            yield _chunk({"content": "engine"})
            if self.keep_open:
                await self.finish.wait()
            yield _chunk({}, finish_reason="stop")


class _OffloadRoute(BaseAdapter):
    """A remote offload route that serves pro callers only, and may fail instead."""

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="reserved",
                base_url="https://reserved.example/v1",
                endpoint_id=f"{MODEL}:reserved-api",
                route_metadata={"route_id": "offload"},
            )
        )
        self.fail_with = fail_with
        self.calls = 0
        self.holds: list[Any] = []

    def has_capacity_for_role(self, role: str | None) -> bool:
        return role != "free"

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.format_response(content="offload", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        self.holds.append(req_ctx.get().get(req_ctx.UPSTREAM_ENGINE_HOLD))
        if self.fail_with is not None:
            raise self.fail_with
        yield _chunk({"content": "offload"})
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


def _router(
    engine: _Engine,
    offload: _OffloadRoute,
    *,
    limit: int | None = 1,
    wait_seconds: float = 0.1,
) -> FixedRouter:
    policy = OffloadPolicy(route_id="offload", wait_seconds=wait_seconds, engine_queue_limit=limit)
    router = FixedRouter(offload_policy_resolver=_Policies(policy))
    router.register_route(MODEL, [(engine, 1.0), (offload, 1.0)])
    return router


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter; its acquire timeout outlasts every test here."""
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    """Make the weighted draw pick the first candidate, so the engine is the primary."""
    monkeypatch.setattr("random.random", lambda: 0.0)


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


def _stream(
    router: FixedRouter, role: str, options: RoutingRequestOptions | None = None
) -> asyncio.Task[list[str]]:
    """Start one caller's stream in a task of its own, as the serving layer runs one."""

    async def run() -> list[str]:
        with req_ctx.push(**{req_ctx.USER_ROLE: role}):
            return await _collect(
                router.stream_chat_completion(MODEL, MESSAGES, routing_options=options)
            )

    return asyncio.create_task(run())


def _payloads(chunks: list[str]) -> list[dict[str, Any]]:
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]


def _content(chunks: list[str]) -> str:
    return "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in _payloads(chunks)
        for choice in payload.get("choices") or []
    )


def _routing(chunks: list[str]) -> list[dict[str, Any]]:
    return [payload["_routing"] for payload in _payloads(chunks) if "_routing" in payload]


def _line() -> dict[str, int]:
    return get_upstream_limiter().engine_snapshot().get(ENGINE_ADDRESS, {})


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


# --------------------------------------------------------- who gets a hold


async def test_a_stream_under_an_engine_queue_limit_carries_its_place_in_line():
    engine = _Engine()
    router = _router(engine, _OffloadRoute(), limit=3)

    first = _stream(router, "free")
    await engine.wait_sent(1)
    engine.answer(0)
    await first

    assert len(engine.holds) == 1
    assert isinstance(engine.holds[0], EngineHold)
    assert engine.holds[0].limit == 3


async def test_a_model_without_an_engine_queue_limit_holds_nothing():
    engine = _Engine()
    router = _router(engine, _OffloadRoute(), limit=None)

    first = _stream(router, "free")
    await engine.wait_sent(1)
    engine.answer(0)
    await first

    assert engine.holds == [None]
    assert get_upstream_limiter().engine_snapshot() == {}


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(RoutingRequestOptions(pin_provider=f"{MODEL}:local-18003"), id="pinned"),
        pytest.param(RoutingRequestOptions(allow_fallback=False), id="caller-owns-the-order"),
    ],
)
async def test_a_stream_that_cannot_be_offloaded_still_takes_its_place_in_line(options):
    """It reaches the engine all the same, so the line has to count it."""
    engine = _Engine()
    router = _router(engine, _OffloadRoute(), limit=1)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)

    behind = _stream(router, "pro", options)
    await _until(lambda: _line().get("waiting") == 1)
    assert engine.sent_count == 1
    assert isinstance(engine.holds[-1], EngineHold)

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)
    assert _content(await ahead) == "engine"
    assert _content(await behind) == "engine"


async def test_the_offload_route_as_the_last_resort_primary_takes_its_place_in_line():
    engine = _Engine()
    offload = _OffloadRoute()
    router = _router(engine, offload, limit=2)
    for _ in range(10):
        router.endpoint_health_registry.record_failure(f"{MODEL}:local-18003", reason="test")

    chunks = await _stream(router, "pro")

    assert _content(chunks) == "offload"
    assert _routing(chunks)[0]["offload"] == OFFLOAD_LAST_RESORT
    assert isinstance(offload.holds[0], EngineHold)
    assert offload.holds[0].limit == 2


async def test_a_response_that_arrives_whole_is_never_held():
    """There is no first token to give the place up on."""
    engine = _Engine()
    router = _router(engine, _OffloadRoute(), limit=1)

    with req_ctx.push(**{req_ctx.USER_ROLE: "free"}):
        response = await router.chat_completion(MODEL, MESSAGES)

    assert response["choices"][0]["message"]["content"] == "engine"
    assert engine.holds == [None]


# ------------------------------------------------------ the line in action


@needs_timeout
async def test_a_held_request_past_its_wait_goes_to_the_offload_route_unsent():
    engine = _Engine()
    offload = _OffloadRoute()
    router = _router(engine, offload, limit=1, wait_seconds=0.1)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)

    chunks = await _stream(router, "pro")

    assert _content(chunks) == "offload"
    assert _routing(chunks)[-1]["offload"] == OFFLOAD_QUEUE_WAIT
    assert _routing(chunks)[-1]["failed_attempts"][0]["error_type"] == "UpstreamQueueWaitExpired"
    # The engine never saw it, so there was nothing to cancel there.
    assert engine.sent_count == 1
    assert engine.cancelled == 0
    engine.answer(0)
    assert _content(await ahead) == "engine"


async def test_a_request_that_cannot_be_offloaded_waits_its_turn():
    engine = _Engine()
    offload = _OffloadRoute()
    router = _router(engine, offload, limit=1, wait_seconds=0.05)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)

    behind = _stream(router, "free")
    await _until(lambda: _line().get("waiting") == 1)
    await asyncio.sleep(0.1)  # well past the wait: nowhere else to go, so it stays
    assert engine.sent_count == 1
    assert _line() == {"pending": 1, "waiting": 1}

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await ahead) == "engine"
    chunks = await behind
    assert _content(chunks) == "engine"
    assert [block.get("offload") for block in _routing(chunks)] == [None]
    assert offload.calls == 0


async def test_a_place_frees_at_the_first_token_not_at_the_end_of_the_stream():
    """A stream the engine is already answering does not count against the limit."""
    engine = _Engine(keep_open=True)
    router = _router(engine, _OffloadRoute(), limit=1, wait_seconds=5.0)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)
    engine.answer(0)
    await _until(lambda: _line().get("pending") == 0)

    behind = _stream(router, "pro")
    await engine.wait_sent(2)

    assert not ahead.done()
    engine.answer(1)
    engine.finish.set()
    assert _content(await ahead) == "engine"
    assert _content(await behind) == "engine"
    assert _line() == {"pending": 0, "waiting": 0}


@needs_timeout
async def test_a_request_back_from_a_failed_offload_route_waits_its_turn_again():
    """The retry after a failed offload has nowhere else to go, so it keeps its line."""
    engine = _Engine()
    offload = _OffloadRoute(fail_with=ConnectionError("offload route down"))
    router = _router(engine, offload, limit=1, wait_seconds=0.05)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)

    behind = _stream(router, "pro")
    await _until(lambda: offload.calls == 1)
    await _until(lambda: _line().get("waiting") == 1)
    assert engine.sent_count == 1

    engine.answer(0)
    await engine.wait_sent(2)
    engine.answer(1)

    assert _content(await ahead) == "engine"
    chunks = await behind
    assert _content(chunks) == "engine"
    last = _routing(chunks)[-1]
    assert last["offload"] == OFFLOAD_QUEUE_WAIT
    assert [attempt["error_type"] for attempt in last["failed_attempts"]] == [
        "UpstreamQueueWaitExpired",
        "ConnectionError",
    ]


async def test_a_held_caller_that_hangs_up_leaves_the_line():
    engine = _Engine()
    router = _router(engine, _OffloadRoute(), limit=1, wait_seconds=5.0)
    ahead = _stream(router, "free")
    await engine.wait_sent(1)

    leaving = _stream(router, "free")
    await _until(lambda: _line().get("waiting") == 1)
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    assert _line() == {"pending": 1, "waiting": 0}
    engine.answer(0)
    assert _content(await ahead) == "engine"
    assert _line() == {"pending": 0, "waiting": 0}
    assert engine.sent_count == 1
