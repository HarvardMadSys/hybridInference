"""The engine wait in FixedRouter (``routing.engine_wait``).

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

from routing.engine_wait import FirstTokenWatch
from routing.offload import OFFLOAD_ENGINE_WAIT, OFFLOAD_QUEUE_WAIT, OffloadPolicy
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.http import AsyncHTTPClient
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
KEY = "shared-key"

#: The first-token wait is enforced with ``asyncio.timeout`` (Python 3.11+).
needs_timeout = pytest.mark.skipif(
    not hasattr(asyncio, "timeout"), reason="the first-token wait needs asyncio.timeout"
)


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


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
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
    return [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
        if chunk.strip() != "data: [DONE]"
    ]


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


@needs_timeout
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


@needs_timeout
async def test_only_streaming_attempts_on_other_routes_are_watched():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    engine.gate.set()
    await router.chat_completion(MODEL, MESSAGES)

    stream_watch, non_stream_watch = engine.watches
    assert isinstance(stream_watch, FirstTokenWatch)
    assert stream_watch.wait_seconds == 0.05
    # A non-stream has no first token to watch, and the offload attempt that
    # took the stream had nowhere left to go.
    assert non_stream_watch is None
    assert reserved.watches == [None]


# ---------------------------------------------------- one decision per request


@needs_timeout
async def test_an_engine_that_kept_one_request_waiting_takes_the_next():
    """Nothing is held against the engine: the next request is sent to it as usual."""
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True, prelude=False)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    offloaded = await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    engine.gate.set()  # the engine's queue drains
    served = await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    answered = await router.chat_completion(MODEL, MESSAGES)

    assert _content(offloaded) == "reserved"
    assert _content(served) == "engine"
    assert [block.get("offload") for block in _routing(served)] == [None]
    assert answered["choices"][0]["message"]["content"] == "engine"
    assert "offload" not in answered["_routing"]
    assert engine.calls == 3
    assert reserved.calls == 1


@needs_timeout
async def test_each_request_waits_from_its_own_start():
    """Two requests queued at the same engine: only the one past its own wait moves."""
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True, prelude=False)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.4))
    loop = asyncio.get_running_loop()

    early = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    await asyncio.sleep(0.2)
    late = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    # The engine starts answering between the two deadlines: 0.5s after the
    # early request (past its 0.4s) and 0.3s after the late one (within its own).
    loop.call_later(0.3, engine.gate.set)

    assert _content(await early) == "reserved"
    assert _content(await late) == "engine"
    assert engine.cancelled == 1
    assert reserved.calls == 1


@needs_timeout
async def test_output_a_processor_holds_back_is_not_an_engine_wait(monkeypatch):
    """A MiniMax ``<think>`` block the adapter strips out is the engine answering.

    The real adapter and processor: ``think_block`` yields nothing until the
    block closes, so the router sees no first token for far longer than the
    wait. The adapter reports the first output it reads, and the request stays.
    """
    thinker = OpenAICompatAdapter(
        ModelConfig(
            id=MODEL,
            name=MODEL,
            provider="thinker",
            base_url="http://localhost:8001/v1",
            api_key="k",
            endpoint_id=f"{MODEL}:thinker-api",
            processor="think_block",
        )
    )
    thinker.http = AsyncHTTPClient()

    async def upstream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[str, None]:
        yield _chunk({"role": "assistant", "content": ""})
        yield _chunk({"content": "<think>"})
        for _ in range(6):
            await asyncio.sleep(0.05)  # 0.3s of thinking against a 0.1s wait
            yield _chunk({"content": "still thinking. "})
        yield _chunk({"content": "</think>answer"})
        yield _chunk({}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(thinker.http, "stream_post", upstream)
    reserved = _Engine("reserved", route_id="offload", answered=True, prelude=False)
    router = _router([(thinker, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.1))

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "answer"
    # The adapter's usage chunk carries a ``_routing`` block of its own; neither
    # it nor the router's says the request moved.
    assert not any(block.get("offload") or block.get("fallback") for block in _routing(chunks))
    assert reserved.calls == 0


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


async def test_a_wait_that_ends_in_the_gateway_queue_is_a_queue_wait():
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=10.0)
    reset_upstream_limiter(limiter)
    engine = _Engine("engine", answered=True, local=False)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))
    held = await limiter.acquire("engine", KEY, base_url=engine.config.base_url)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    assert _routing(chunks)[-1]["offload"] == OFFLOAD_QUEUE_WAIT
    held.release(status_code=200)


async def test_a_non_streaming_request_is_never_cut_short():
    engine = _Engine("engine", answered=True, delay=0.1)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.02))

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "engine"
    assert reserved.calls == 0


async def test_without_an_offload_route_nothing_is_watched():
    engine = _Engine("engine", answered=True, delay=0.05)
    other = _Engine("other", answered=True)
    router = _router([(engine, 1.0), (other, 1.0)])

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    assert engine.watches == [None]


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


async def test_with_the_offload_route_unavailable_a_stream_waits_for_its_engine():
    engine = _Engine("engine", answered=True, delay=0.1)
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.02))
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:reserved-api", reason="test")

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    # With nowhere to offload it, the attempt had no first-token deadline.
    assert engine.watches == [None]


async def test_a_caller_the_offload_route_holds_no_key_for_waits_for_its_engine():
    """A free caller, and an offload route whose only key is reserved for pro and up.

    Cutting the stream short would abort the request at the engine only for the
    offload route to refuse it before sending anything, and put it back in the
    engine's queue behind everyone who arrived since.
    """
    engine = _Engine("engine", answered=True, delay=0.1)
    reserved = OpenAICompatAdapter(
        ModelConfig(
            id=MODEL,
            name=MODEL,
            provider="reserved",
            base_url="https://reserved.example/v1",
            api_keys=["pro-only-key"],
            endpoint_id=f"{MODEL}:reserved-api",
            route_metadata={"route_id": "offload"},
        )
    )
    reserved._key_pool.set_key_min_role("pro-only-key", "pro")
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.02))

    with req_ctx.push(**{req_ctx.USER_ROLE: "free"}):
        chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    assert engine.watches == [None]
    assert engine.cancelled == 0
    assert [block.get("offload") for block in _routing(chunks)] == [None]


async def test_a_client_that_hangs_up_while_waiting_is_not_offloaded():
    engine = _Engine("engine")
    reserved = _Engine("reserved", route_id="offload", answered=True)
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=5.0))

    task = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, MESSAGES)))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine.cancelled == 1
    assert reserved.calls == 0


# ----------------------------------------- back to an engine that was cut short
#
# The offload route failed after the engine's wait cut the stream short. The
# engine never answered, so the request goes back to it and waits this time.


class _SlowThenFailing(_Engine):
    """An engine too slow to answer in time, that fails outright when retried."""

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        if self.calls:
            self.fail_with = RuntimeError("engine 503")
        async for chunk in super().stream_chat_completion(messages, **params):
            yield chunk


@needs_timeout
async def test_a_stream_cut_short_goes_back_to_its_engine_when_the_offload_route_fails():
    engine = _Engine("engine", answered=True, delay=0.2)
    reserved = _Engine("reserved", route_id="offload", fail_with=RuntimeError("upstream 503"))
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "engine"
    assert engine.calls == 2
    assert engine.cancelled == 1
    first_watch, retry_watch = engine.watches
    assert isinstance(first_watch, FirstTokenWatch)
    # With nowhere left to go, the retry waits as long as the engine needs.
    assert retry_watch is None
    served = _routing(chunks)[-1]
    assert served["endpoint_id"] == "m:engine-api"
    assert served["fallback"] is True
    assert served["offload"] == OFFLOAD_ENGINE_WAIT
    assert served["offload_endpoint_id"] == "m:reserved-api"
    assert [(a["endpoint_id"], a["error_type"]) for a in served["failed_attempts"]] == [
        ("m:engine-api", "EngineWaitExpired"),
        ("m:reserved-api", "RuntimeError"),
    ]


@needs_timeout
async def test_a_stream_whose_retry_fails_reports_the_engines_own_error():
    """Not the deadline that cut its first attempt short."""
    engine = _SlowThenFailing("engine")
    reserved = _Engine("reserved", route_id="offload", fail_with=RuntimeError("upstream 503"))
    router = _router([(engine, 1.0), (reserved, 1.0)], _policy(wait_seconds=0.05))

    with pytest.raises(RuntimeError, match="engine 503") as caught:
        await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    routing = caught.value._routing
    assert routing["endpoint_id"] == "m:engine-api"
    assert "fallback" not in routing
    assert routing["offload"] == OFFLOAD_ENGINE_WAIT
    assert routing["offload_endpoint_id"] == "m:reserved-api"
    assert [a["endpoint_id"] for a in routing["failed_attempts"]] == [
        "m:engine-api",
        "m:reserved-api",
        "m:engine-api",
    ]
