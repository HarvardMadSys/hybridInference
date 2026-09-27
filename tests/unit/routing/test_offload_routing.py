"""Queue-wait offload in FixedRouter (``routing.offload``).

The adapters here take a real outbound slot from the process-wide
``UpstreamConcurrencyLimiter`` before answering, so the tests exercise the whole
chain an offload depends on: the router arms a queue deadline around an attempt,
the limiter ends that attempt's wait, and the router sends the request on to the
offload route.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.offload import (
    OFFLOAD_LAST_RESORT,
    OFFLOAD_QUEUE_WAIT,
    FallbackOrder,
    OffloadPolicy,
    ended_in_gateway_queue,
)
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    UpstreamQueueWaitExpired,
    UpstreamSaturated,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]
KEY = "shared-key"
UNSET = object()


class _SlotAdapter(BaseAdapter):
    """Answer with the provider label after holding an outbound slot.

    Records the queue deadline each call ran under, so a test can tell which
    attempts the router armed.
    """

    def __init__(
        self,
        provider: str,
        *,
        route_id: str | None = None,
        fail_with: BaseException | None = None,
        local: bool = False,
        context_length: int = 8192,
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
                context_length=context_length,
            )
        )
        self.fail_with = fail_with
        self.calls = 0
        self.deadlines: list[Any] = []

    def _record(self) -> None:
        self.calls += 1
        self.deadlines.append(req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE, UNSET))

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
            yield self.format_stream_chunk(content=self.config.provider, model=MODEL)


class _Policies:
    """Minimal ``OffloadPolicySource``."""

    def __init__(self, policies: dict[str, OffloadPolicy] | None = None) -> None:
        self.policies = dict(policies or {})

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policies.get(model_id)


class _BrokenPolicies:
    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        raise RuntimeError("policy store unavailable")


@pytest.fixture
def limiter():
    """One slot per provider key and a long acquire timeout.

    The timeout is far longer than any offload wait below, so a test that
    finishes quickly proves the offload deadline -- not the limiter's own
    timeout -- ended the wait.
    """
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


def _router(
    routes: list[tuple[BaseAdapter, float]],
    policy: OffloadPolicy | None = None,
    *,
    source: Any = None,
) -> FixedRouter:
    if source is None:
        source = _Policies({MODEL: policy} if policy is not None else {})
    router = FixedRouter(offload_policy_resolver=source)
    router.register_route(MODEL, routes)
    return router


def _policy(route_id: str = "offload", wait_seconds: float = 0.05) -> OffloadPolicy:
    return OffloadPolicy(route_id=route_id, wait_seconds=wait_seconds)


# --------------------------------------------------------------- the values


@pytest.mark.parametrize(
    ("route_id", "wait_seconds"),
    [
        ("", 1.0),
        ("   ", 1.0),
        (None, 1.0),
        ("r", 0),
        ("r", -1.0),
        ("r", float("nan")),
        ("r", float("inf")),
        ("r", True),
        ("r", "5"),
    ],
)
def test_policy_rejects_values_routing_could_not_honor(route_id, wait_seconds):
    with pytest.raises(ValueError):
        OffloadPolicy(route_id=route_id, wait_seconds=wait_seconds)


def test_policy_normalizes_an_integer_wait_to_float():
    policy = OffloadPolicy(route_id="r", wait_seconds=3)
    assert policy.wait_seconds == 3.0
    assert isinstance(policy.wait_seconds, float)


def test_only_gateway_queue_refusals_count_as_a_queue_wait():
    assert ended_in_gateway_queue(UpstreamQueueWaitExpired("x"))
    assert ended_in_gateway_queue(UpstreamSaturated("x"))
    assert not ended_in_gateway_queue(RuntimeError("upstream 500"))
    assert not ended_in_gateway_queue(asyncio.TimeoutError())


def test_fallback_order_without_offload_is_route_order():
    a, b = object(), object()
    order = FallbackOrder([a, b])
    order.record_failure(UpstreamSaturated("x"))
    assert order.next() == (a, None)
    assert order.next() == (b, None)
    assert order.next() is None


def test_fallback_order_keeps_the_offload_route_last_after_ordinary_failures():
    a, b, off = object(), object(), object()
    order = FallbackOrder([a, off, b], off)
    order.record_failure(RuntimeError("500"))
    assert order.next() == (a, None)
    order.record_failure(RuntimeError("500"))
    assert order.next() == (b, None)
    order.record_failure(RuntimeError("500"))
    assert order.next() == (off, OFFLOAD_LAST_RESORT)
    assert order.next() is None


def test_fallback_order_jumps_to_the_offload_route_after_a_queue_wait():
    a, b, off = object(), object(), object()
    order = FallbackOrder([a, b], off)
    order.record_failure(UpstreamQueueWaitExpired("x"))
    assert order.next() == (off, OFFLOAD_QUEUE_WAIT)
    assert order.offload_pending is None
    # Handed out once: a later queue wait has nothing left to jump to.
    order.record_failure(UpstreamQueueWaitExpired("x"))
    assert order.next() == (a, None)
    assert order.next() == (b, None)
    assert order.next() is None


# ----------------------------------------------------------------- selection


def test_selection_never_draws_the_offload_route_while_another_is_admissible(monkeypatch):
    monkeypatch.setattr("random.random", __import__("random").Random(7).random)
    ordinary = _SlotAdapter("ordinary")
    offload = _SlotAdapter("reserved", route_id="offload")
    # Weighted almost entirely toward the offload route: without the reservation
    # nearly every draw would land on it.
    router = _router([(ordinary, 0.01), (offload, 100.0)], _policy())

    picks = {router.select_adapter(MODEL).config.provider for _ in range(500)}

    assert picks == {"ordinary"}


def test_selection_falls_back_to_the_offload_route_when_nothing_else_is_admissible():
    ordinary = _SlotAdapter("ordinary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:ordinary-api", reason="test")

    assert router.select_adapter(MODEL) is offload


def test_a_policy_naming_no_route_changes_nothing(monkeypatch):
    monkeypatch.setattr("random.random", __import__("random").Random(3).random)
    a = _SlotAdapter("a")
    b = _SlotAdapter("b")
    router = _router([(a, 1.0), (b, 1.0)], _policy(route_id="gone"))

    picks = {router.select_adapter(MODEL).config.provider for _ in range(200)}

    assert picks == {"a", "b"}


def test_an_offload_route_weighted_to_zero_is_not_an_offload_route():
    ordinary = _SlotAdapter("ordinary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 0.0)], _policy())
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:ordinary-api", reason="test")

    with pytest.raises(AllCircuitsOpenError):
        router.select_adapter(MODEL)


def test_eligible_adapters_lists_the_offload_route_last():
    offload = _SlotAdapter("reserved", route_id="offload")
    a = _SlotAdapter("a")
    b = _SlotAdapter("b")
    router = _router([(offload, 1.0), (a, 1.0), (b, 1.0)], _policy())

    eligible = [adapter for adapter, _weight in router.eligible_adapters(MODEL)]

    assert eligible == [a, b, offload]


def test_a_broken_policy_source_routes_without_offload():
    a = _SlotAdapter("a", route_id="offload")
    router = _router([(a, 1.0)], source=_BrokenPolicies())

    assert router.select_adapter(MODEL) is a


# ---------------------------------------------------------- non-streaming


async def test_a_request_queued_past_the_wait_is_sent_to_the_offload_route(limiter):
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))
    held = await _saturate(limiter, primary)

    started = time.monotonic()
    resp = await router.chat_completion(MODEL, MESSAGES)

    assert time.monotonic() - started < 5.0
    assert resp["choices"][0]["message"]["content"] == "reserved"
    routing = resp["_routing"]
    assert routing["offload"] == OFFLOAD_QUEUE_WAIT
    assert routing["fallback"] is True
    assert routing["endpoint_id"] == "m:reserved-api"
    assert [attempt["error_type"] for attempt in routing["failed_attempts"]] == [
        "UpstreamQueueWaitExpired"
    ]
    # The queued attempt was never sent, so the endpoint keeps its circuit.
    assert router.endpoint_health_registry.allow_request("m:primary-api")
    held.release(status_code=200)


async def test_only_ordinary_attempts_are_armed_with_the_queue_deadline(limiter):
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))
    held = await _saturate(limiter, primary)

    before = time.monotonic()
    await router.chat_completion(MODEL, MESSAGES)

    (primary_deadline,) = primary.deadlines
    assert isinstance(primary_deadline, float)
    assert before < primary_deadline <= time.monotonic() + 0.05
    # The offload attempt queues normally: None, not the primary's leftover.
    assert offload.deadlines == [None]
    held.release(status_code=200)


async def test_a_queue_wait_jumps_the_offload_route_ahead_of_the_other_routes(limiter):
    primary = _SlotAdapter("primary")
    sibling = _SlotAdapter("sibling")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())
    held = await _saturate(limiter, primary)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["_routing"]["offload"] == OFFLOAD_QUEUE_WAIT
    assert sibling.calls == 0
    held.release(status_code=200)


async def test_an_ordinary_failure_walks_the_route_before_the_offload_route(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _SlotAdapter("sibling")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "sibling"
    assert "offload" not in resp["_routing"]
    assert offload.calls == 0
    # The sibling could still have offloaded, so it was armed too.
    assert isinstance(sibling.deadlines[0], float)


async def test_the_offload_route_is_the_last_resort_when_every_route_fails(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _SlotAdapter("sibling", fail_with=RuntimeError("upstream 502"))
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "reserved"
    assert resp["_routing"]["offload"] == OFFLOAD_LAST_RESORT
    assert [attempt["endpoint_id"] for attempt in resp["_routing"]["failed_attempts"]] == [
        "m:primary-api",
        "m:sibling-api",
    ]


async def test_a_queue_wait_on_a_fallback_attempt_also_offloads(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _SlotAdapter("sibling")
    other = _SlotAdapter("other")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router(
        [(primary, 1.0), (sibling, 1.0), (other, 1.0), (offload, 1.0)],
        _policy(wait_seconds=0.05),
    )
    held = await _saturate(limiter, sibling)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["_routing"]["offload"] == OFFLOAD_QUEUE_WAIT
    assert other.calls == 0
    held.release(status_code=200)


async def test_the_limiters_own_timeout_offloads_too(limiter):
    """A wait longer than the acquire timeout still ends in a slotless queue."""
    short = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=0.05)
    reset_upstream_limiter(short)
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=60.0))
    held = await _saturate(short, primary)

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["_routing"]["offload"] == OFFLOAD_QUEUE_WAIT
    assert resp["_routing"]["failed_attempts"][0]["error_type"] == "UpstreamSaturated"
    held.release(status_code=200)


async def test_a_primary_chosen_as_last_resort_says_so(limiter):
    ordinary = _SlotAdapter("ordinary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:ordinary-api", reason="test")

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "reserved"
    assert resp["_routing"]["offload"] == OFFLOAD_LAST_RESORT
    assert offload.deadlines == [None]


async def test_without_a_policy_nothing_is_armed(limiter):
    a = _SlotAdapter("a")
    b = _SlotAdapter("b")
    router = _router([(a, 1.0), (b, 1.0)])

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert "offload" not in resp["_routing"]
    assert a.deadlines == [None]


async def test_a_pinned_request_never_offloads(limiter):
    short = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=0.05)
    reset_upstream_limiter(short)
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.01))
    held = await _saturate(short, primary)

    with pytest.raises(UpstreamSaturated) as excinfo:
        await router.chat_completion(
            MODEL,
            MESSAGES,
            routing_options=RoutingRequestOptions(pin_provider="m:primary-api"),
        )

    # It waited out the limiter's timeout, not the offload wait.
    assert not isinstance(excinfo.value, UpstreamQueueWaitExpired)
    assert primary.deadlines == [None]
    assert offload.calls == 0
    held.release(status_code=200)


async def test_a_caller_that_owns_the_order_gets_no_deadline(limiter):
    short = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=1, acquire_timeout=0.05)
    reset_upstream_limiter(short)
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.01))
    held = await _saturate(short, primary)

    with pytest.raises(UpstreamSaturated):
        await router.chat_completion(
            MODEL,
            MESSAGES,
            routing_options=RoutingRequestOptions(allow_fallback=False),
        )

    assert primary.deadlines == [None]
    assert offload.calls == 0
    held.release(status_code=200)


async def test_no_deadline_while_the_offload_routes_circuit_is_open(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _SlotAdapter("sibling")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:reserved-api", reason="test")

    resp = await router.chat_completion(MODEL, MESSAGES)

    # Leaving a queue for a route that would refuse the request gains nothing.
    assert primary.deadlines == [None]
    assert sibling.deadlines == [None]
    assert resp["choices"][0]["message"]["content"] == "sibling"


async def test_an_offload_route_outside_the_dispatch_scope_is_not_used(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy())

    with pytest.raises(RuntimeError, match="upstream 500"):
        await router.chat_completion(
            MODEL,
            MESSAGES,
            routing_options=RoutingRequestOptions(endpoint_scope=frozenset({"m:primary-api"})),
        )

    assert primary.deadlines == [None]
    assert offload.calls == 0


async def test_an_alias_uses_the_canonical_models_policy(limiter):
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = FixedRouter(offload_policy_resolver=_Policies({MODEL: _policy()}))
    router.register_route(MODEL, [(primary, 1.0), (offload, 1.0)], aliases=["m-alias"])
    held = await _saturate(limiter, primary)

    resp = await router.chat_completion("m-alias", MESSAGES)

    assert resp["_routing"]["offload"] == OFFLOAD_QUEUE_WAIT
    held.release(status_code=200)


async def test_a_queue_wait_keeps_the_endpoints_prefill_hints(limiter, monkeypatch):
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy())
    forgotten: list[str] = []
    monkeypatch.setattr(router.prefill_load, "forget_endpoint", forgotten.append)
    held = await _saturate(limiter, primary)

    await router.chat_completion(MODEL, MESSAGES)

    assert forgotten == []
    held.release(status_code=200)


async def test_a_real_failure_still_forgets_the_endpoints_prefill_hints(limiter, monkeypatch):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy())
    forgotten: list[str] = []
    monkeypatch.setattr(router.prefill_load, "forget_endpoint", forgotten.append)

    await router.chat_completion(MODEL, MESSAGES)

    assert forgotten == ["m:primary-api"]


async def test_a_local_route_never_queues_so_never_offloads(limiter):
    local = _SlotAdapter("local", local=True)
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(local, 1.0), (offload, 1.0)], _policy())
    # Two requests at once against a one-slot limit: a remote route would queue
    # the second. The limiter exempts local servers, so both are served locally.
    first, second = await asyncio.gather(
        router.chat_completion(MODEL, MESSAGES),
        router.chat_completion(MODEL, MESSAGES),
    )

    assert {first["choices"][0]["message"]["content"]} == {"local"}
    assert {second["choices"][0]["message"]["content"]} == {"local"}
    assert offload.calls == 0


# --------------------------------------------------------------- streaming


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


def _content(chunks: list[str]) -> str:
    import json

    text = ""
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        for choice in payload.get("choices") or []:
            text += (choice.get("delta") or {}).get("content") or ""
    return text


def _routing_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    import json

    blocks = []
    for chunk in chunks:
        payload = json.loads(chunk.removeprefix("data: ").strip())
        if "_routing" in payload:
            blocks.append(payload["_routing"])
    return blocks


async def test_a_queued_stream_is_sent_to_the_offload_route(limiter):
    primary = _SlotAdapter("primary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (offload, 1.0)], _policy(wait_seconds=0.05))
    held = await _saturate(limiter, primary)

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    first_attempt, offload_attempt = _routing_chunks(chunks)
    assert "offload" not in first_attempt
    assert offload_attempt["offload"] == OFFLOAD_QUEUE_WAIT
    assert offload_attempt["fallback"] is True
    assert offload_attempt["failed_attempts"][0]["error_type"] == "UpstreamQueueWaitExpired"
    assert isinstance(primary.deadlines[0], float)
    assert offload.deadlines == [None]
    held.release(status_code=200)


async def test_a_stream_uses_the_offload_route_as_the_last_resort(limiter):
    primary = _SlotAdapter("primary", fail_with=RuntimeError("upstream 500"))
    sibling = _SlotAdapter("sibling", fail_with=RuntimeError("upstream 502"))
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    assert _routing_chunks(chunks)[-1]["offload"] == OFFLOAD_LAST_RESORT


async def test_a_stream_chosen_as_last_resort_marks_its_first_routing_chunk(limiter):
    ordinary = _SlotAdapter("ordinary")
    offload = _SlotAdapter("reserved", route_id="offload")
    router = _router([(ordinary, 1.0), (offload, 1.0)], _policy())
    for _ in range(10):
        router.endpoint_health_registry.record_failure("m:ordinary-api", reason="test")

    chunks = await _collect(router.stream_chat_completion(MODEL, MESSAGES))

    assert _content(chunks) == "reserved"
    assert _routing_chunks(chunks)[0]["offload"] == OFFLOAD_LAST_RESORT


# ---------------------------------------------------- context-window refusals


class _TooLong(RuntimeError):
    """An upstream refusing the prompt as too long for its window, worded as SGLang does."""

    status_code = 400

    def __init__(self, window: int) -> None:
        super().__init__(
            f"The input (20000 tokens) is longer than the model's context length ({window} tokens)."
        )


_PATHS = pytest.mark.parametrize("stream", [True, False], ids=["stream", "chat"])


async def _dispatch(router: FixedRouter, *, stream: bool) -> Any:
    if stream:
        return await _collect(router.stream_chat_completion(MODEL, MESSAGES))
    return await router.chat_completion(MODEL, MESSAGES)


@_PATHS
async def test_an_offload_route_too_narrow_for_a_refused_prompt_is_not_sent_it(limiter, stream):
    """The context-window skip covers the offload route like any other fallback."""
    refusal = _TooLong(8192)
    primary = _SlotAdapter("primary", fail_with=refusal, context_length=8192)
    offload = _SlotAdapter("reserved", route_id="offload", context_length=8192)
    router = _router([(primary, 1.0), (offload, 1.0)], _policy())

    with pytest.raises(_TooLong) as exc_info:
        await _dispatch(router, stream=stream)

    assert exc_info.value is refusal
    assert offload.calls == 0


@_PATHS
async def test_a_wider_offload_route_is_still_the_last_resort_for_a_refused_prompt(limiter, stream):
    primary = _SlotAdapter("primary", fail_with=_TooLong(8192), context_length=8192)
    sibling = _SlotAdapter("sibling", context_length=8192)
    offload = _SlotAdapter("reserved", route_id="offload", context_length=32768)
    router = _router([(primary, 1.0), (sibling, 1.0), (offload, 1.0)], _policy())

    result = await _dispatch(router, stream=stream)

    assert sibling.calls == 0
    assert offload.calls == 1
    if stream:
        assert _content(result) == "reserved"
        assert _routing_chunks(result)[-1]["offload"] == OFFLOAD_LAST_RESORT
    else:
        assert result["_routing"]["offload"] == OFFLOAD_LAST_RESORT


@_PATHS
@pytest.mark.parametrize(
    ("offload_window", "armed"),
    [(8192, False), (65536, True)],
    ids=["offload-cannot-fit", "offload-can-fit"],
)
async def test_a_fallback_is_armed_only_if_the_offload_route_could_fit_the_prompt(
    limiter, stream, offload_window, armed
):
    """Leaving the queue for an offload route the walk would pass over only wastes the wait."""
    primary = _SlotAdapter("primary", fail_with=_TooLong(8192), context_length=8192)
    wide = _SlotAdapter("wide", context_length=65536)
    offload = _SlotAdapter("reserved", route_id="offload", context_length=offload_window)
    router = _router([(primary, 1.0), (wide, 1.0), (offload, 1.0)], _policy())

    result = await _dispatch(router, stream=stream)

    if stream:
        assert _content(result) == "wide"
    else:
        assert result["choices"][0]["message"]["content"] == "wide"
    # Nothing had refused the prompt when the primary was dispatched.
    assert isinstance(primary.deadlines[0], float)
    (deadline,) = wide.deadlines
    if armed:
        assert isinstance(deadline, float)
    else:
        assert deadline is None
