"""The offload route's max input in FixedRouter (``OffloadPolicy.max_input_tokens``).

A request whose prompt is estimated at more than the max input is never sent to
the offload route, for any of the three reasons: it gets no queue deadline, no
first-token watch and no last resort, and is served only by the model's other
routes. The adapters take a real outbound slot from the process-wide limiter, as
in ``test_offload_routing``, so a queue wait here is the limiter's own.

Prompts are sized by ``estimate_prefill_tokens``, at four bytes a token: ``LONG``
estimates at 100 tokens, over the max input of 99 most tests use.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from routing.offload import OFFLOAD_LAST_RESORT, OFFLOAD_QUEUE_WAIT, OffloadPolicy
from routing.prefill_load import estimate_prefill_tokens
from routing.routers import AllCircuitsOpenError, FixedRouter
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
KEY = "shared-key"
SHORT = [{"role": "user", "content": "hi"}]
LONG = [{"role": "user", "content": "x" * 400}]
MAX_INPUT = 99
UNSET = object()


class _Route(BaseAdapter):
    """Answer with the provider label after holding an outbound slot.

    Records the queue deadline and first-token watch each call ran under. A
    stream sends its first token once ``gate`` is set, which it is from the start
    unless the test says otherwise.
    """

    def __init__(
        self,
        provider: str,
        *,
        route_id: str | None = None,
        local: bool = False,
        answered: bool = True,
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
        self.fail_with = fail_with
        self.calls = 0
        self.deadlines: list[Any] = []
        self.watches: list[Any] = []

    def _record(self) -> None:
        self.calls += 1
        ctx = req_ctx.get()
        self.deadlines.append(ctx.get(req_ctx.UPSTREAM_QUEUE_DEADLINE, UNSET))
        self.watches.append(ctx.get(req_ctx.UPSTREAM_DISPATCH_WATCH, UNSET))

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self._record()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            return self.format_response(content=self.config.provider, model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self._record()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if self.fail_with is not None:
                raise self.fail_with
            yield _chunk({"role": "assistant"})
            await self.gate.wait()
            yield _chunk({"content": self.config.provider})


def _chunk(delta: dict[str, str]) -> str:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


class _Policies:
    def __init__(self, policy: OffloadPolicy) -> None:
        self.policy = policy

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policy if model_id == MODEL else None


def _policy(max_input_tokens: int | None = MAX_INPUT, wait_seconds: float = 0.05) -> OffloadPolicy:
    return OffloadPolicy(
        route_id="offload", wait_seconds=wait_seconds, max_input_tokens=max_input_tokens
    )


def _router(routes: list[tuple[BaseAdapter, float]], policy: OffloadPolicy) -> FixedRouter:
    router = FixedRouter(offload_policy_resolver=_Policies(policy))
    router.register_route(MODEL, routes)
    return router


def _open_circuit(router: FixedRouter, adapter: BaseAdapter) -> None:
    for _ in range(10):
        router.endpoint_health_registry.record_failure(
            f"{MODEL}:{adapter.config.provider}-api", reason="test"
        )


@pytest.fixture
def limiter():
    """One slot per provider key, and an acquire timeout no test here waits out."""
    installed = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=10.0)
    reset_upstream_limiter(installed)
    yield installed
    reset_upstream_limiter()


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    """Make the weighted draw pick the first candidate, so the primary is known."""
    monkeypatch.setattr("random.random", lambda: 0.0)


async def _saturate(limiter: UpstreamConcurrencyLimiter, adapter: BaseAdapter):
    """Hold the adapter's only outbound slot, so its next request must queue."""
    return await limiter.acquire(adapter.config.provider, KEY, base_url=adapter.config.base_url)


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


def _content(chunks: list[str]) -> str:
    payloads = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
    return "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in payloads
        for choice in payload.get("choices") or []
    )


# --------------------------------------------------------------- the value


def test_the_prompts_here_are_sized_either_side_of_the_max_input():
    assert estimate_prefill_tokens(SHORT) <= MAX_INPUT < estimate_prefill_tokens(LONG)


@pytest.mark.parametrize("max_input_tokens", [0, -1, True, 1.5, "100"])
def test_policy_rejects_a_max_input_routing_could_not_honor(max_input_tokens):
    with pytest.raises(ValueError, match="max_input_tokens"):
        OffloadPolicy(route_id="r", wait_seconds=1.0, max_input_tokens=max_input_tokens)


def test_the_max_input_itself_may_be_offloaded():
    policy = OffloadPolicy(route_id="r", wait_seconds=1.0, max_input_tokens=100)

    assert policy.takes_input(0)
    assert policy.takes_input(100)
    assert not policy.takes_input(101)


def test_without_a_max_input_any_size_may_be_offloaded():
    policy = OffloadPolicy(route_id="r", wait_seconds=1.0)

    assert policy.max_input_tokens is None
    assert policy.takes_input(10**9)


# ----------------------------------------------------------------- selection


def test_a_long_prompt_is_not_sent_to_the_offload_route_as_the_last_one_standing():
    ordinary = _Route("ordinary")
    offload = _Route("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())
    _open_circuit(router, ordinary)

    with pytest.raises(AllCircuitsOpenError, match="max input of 99 tokens"):
        router.select_adapter(MODEL, prefill_tokens=MAX_INPUT + 1)
    # Up to the max input, the offload route is still the last resort.
    assert router.select_adapter(MODEL, prefill_tokens=MAX_INPUT) is offload


def test_a_long_prompt_still_gets_the_other_routes():
    ordinary = _Route("ordinary")
    offload = _Route("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())

    assert router.select_adapter(MODEL, prefill_tokens=10**6) is ordinary


def test_eligible_adapters_leaves_the_offload_route_out_for_a_long_prompt():
    offload = _Route("reserved", route_id="offload")
    a = _Route("a")
    b = _Route("b")
    router = _router([(a, 1.0), (offload, 1.0), (b, 1.0)], _policy())

    asked: list[BaseAdapter] = []

    def listed(tokens: int) -> list[BaseAdapter]:
        def size_prompt(adapter: BaseAdapter) -> int:
            asked.append(adapter)
            return tokens

        return [adapter for adapter, _w in router.eligible_adapters(MODEL, size_prompt=size_prompt)]

    assert listed(MAX_INPUT + 1) == [a, b]
    assert listed(MAX_INPUT) == [a, b, offload]
    # Sized for the route the prompt might be sent to.
    assert asked == [offload, offload]
    # A surface that does not size its prompt keeps the old order.
    assert [adapter for adapter, _w in router.eligible_adapters(MODEL)] == [a, b, offload]


def test_eligible_adapters_sizes_the_prompt_only_for_a_route_with_a_max_input():
    offload = _Route("reserved", route_id="offload")
    a = _Route("a")
    router = _router([(a, 1.0), (offload, 1.0)], _policy(max_input_tokens=None))
    sized: list[BaseAdapter] = []

    def size_prompt(adapter: BaseAdapter) -> int:
        sized.append(adapter)
        return 10**6

    eligible = [adapter for adapter, _w in router.eligible_adapters(MODEL, size_prompt=size_prompt)]

    assert eligible == [a, offload]
    assert sized == []


# ---------------------------------------------------------- non-streaming


async def test_a_long_prompt_keeps_its_place_in_line_past_the_wait(limiter):
    primary = _Route("primary")
    offload = _Route("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))
    held = await _saturate(limiter, primary)

    request = asyncio.create_task(router.chat_completion(MODEL, LONG))
    await asyncio.sleep(0.3)
    assert not request.done()

    held.release(status_code=200)
    resp = await request

    assert resp["choices"][0]["message"]["content"] == "primary"
    assert "offload" not in resp["_routing"]
    assert primary.deadlines == [None]
    assert offload.calls == 0


async def test_a_prompt_at_the_max_input_is_still_offloaded(limiter):
    primary = _Route("primary")
    offload = _Route("reserved", route_id="offload")
    router = _router(
        [(primary, 1.0), (offload, 1.0)],
        _policy(max_input_tokens=estimate_prefill_tokens(LONG)),
    )
    held = await _saturate(limiter, primary)

    resp = await router.chat_completion(MODEL, LONG)

    assert resp["choices"][0]["message"]["content"] == "reserved"
    assert resp["_routing"]["offload"] == OFFLOAD_QUEUE_WAIT
    held.release(status_code=200)


async def test_a_long_prompt_has_no_last_resort(limiter):
    primary = _Route("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _Route("sibling", fail_with=RuntimeError("upstream 502"))
    offload = _Route("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    with pytest.raises(RuntimeError, match="upstream 50") as raised:
        await router.chat_completion(MODEL, LONG)

    assert offload.calls == 0
    assert "offload" not in raised.value._routing  # type: ignore[attr-defined]
    # The same failures send a short prompt on to the offload route.
    resp = await router.chat_completion(MODEL, SHORT)
    assert resp["_routing"]["offload"] == OFFLOAD_LAST_RESORT


async def test_a_long_prompts_fallback_attempts_get_no_deadline(limiter):
    primary = _Route("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _Route("sibling")
    offload = _Route("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    resp = await router.chat_completion(MODEL, LONG)

    assert resp["choices"][0]["message"]["content"] == "sibling"
    assert primary.deadlines == [None]
    assert sibling.deadlines == [None]


async def test_a_long_prompt_fails_rather_than_being_offloaded(limiter):
    ordinary = _Route("ordinary")
    offload = _Route("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())
    _open_circuit(router, ordinary)

    with pytest.raises(AllCircuitsOpenError):
        await router.chat_completion(MODEL, LONG)
    with pytest.raises(AllCircuitsOpenError):
        await _collect(router.stream_chat_completion(MODEL, LONG))

    assert offload.calls == 0


# ---------------------------------------------------------------- streaming


async def test_a_long_streams_engine_is_not_watched(limiter):
    engine = _Route("engine", local=True, answered=False)
    offload = _Route("reserved", route_id="offload")
    router = _router([(engine, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))

    stream = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, LONG)))
    await asyncio.sleep(0.3)
    assert not stream.done()

    engine.gate.set()
    chunks = await stream

    assert _content(chunks) == "engine"
    assert engine.watches == [None]
    assert offload.calls == 0


async def test_a_queued_long_stream_keeps_its_place_in_line(limiter):
    primary = _Route("primary")
    offload = _Route("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))
    held = await _saturate(limiter, primary)

    stream = asyncio.create_task(_collect(router.stream_chat_completion(MODEL, LONG)))
    await asyncio.sleep(0.3)
    assert not stream.done()

    held.release(status_code=200)
    chunks = await stream

    assert _content(chunks) == "primary"
    assert primary.deadlines == [None]
    assert offload.calls == 0


async def test_a_long_stream_has_no_last_resort(limiter):
    primary = _Route("primary", fail_with=RuntimeError("upstream 500"))
    offload = _Route("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy())

    with pytest.raises(RuntimeError, match="upstream 500"):
        await _collect(router.stream_chat_completion(MODEL, LONG))

    assert offload.calls == 0
