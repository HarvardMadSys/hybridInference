"""A request counts against other callers' affinity pins only once it has left the gateway.

The prefill lease is taken when selection commits, so concurrent selections see
it and spread. A request can then wait here for up to half a minute -- fetching
remote media for a self-hosted route, queued for an outbound slot -- and while
it does it occupies no engine. Counted from selection, one large prompt stalled
that way moved every caller pinned to its endpoint onto a cold one and re-pinned
them there, without ever reaching an engine.

The rules under test: a lease counts against other callers' affinity ceiling
only while its request is out of the gateway; no lease charges more than its
route's context window; and a pin passed over for backlog is kept, so only that
one request goes elsewhere.

The fake engines take their outbound slot from the process-wide limiter, as the
real adapters do: that is what tells the router the request was sent.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.prefill_load import PrefillLoadTracker
from routing.routers import FixedRouter, _Affinity, _DispatchReport
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_first_token, report_queued, report_sent
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

MODEL = "m"
KEY = "k"
# ~175k estimated tokens: past the 150k affinity ceiling on its own.
LARGE_PROMPT = "y" * 700_000
SMALL_PROMPT = "x" * 4000

_PATHS = pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])


class _Engine(BaseAdapter):
    """An engine that answers at once, except for one caller's request, which it holds.

    With ``hold_before_send`` the held request waits before it takes its
    outbound slot, so it never leaves the gateway (a stalled media fetch).
    Otherwise it is sent and then waits at the engine (a real prefill).
    """

    def __init__(self, name: str, *, context_length: int = 262_144) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=name,
                base_url=f"https://{name}.example/v1",
                endpoint_id=f"{MODEL}:{name}",
                context_length=context_length,
                max_output_length=4096,
            )
        )
        self.held_caller: str | None = None
        self.hold_before_send = False
        self.release = asyncio.Event()

    async def _serve(self) -> None:
        held = req_ctx.get().get("affinity_key") == self.held_caller
        if held and self.hold_before_send:
            await self.release.wait()
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if held and not self.hold_before_send:
                await self.release.wait()

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        await self._serve()
        return self.format_response(content="ok", model=MODEL)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        await self._serve()
        yield self.format_stream_chunk(model=MODEL, content="ok")


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter with room to spare, so nothing queues by accident."""
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


def _router(*engines: _Engine) -> FixedRouter:
    router = FixedRouter()
    router.register_route(MODEL, [(engine, 1.0) for engine in engines])
    return router


def _pin(router: FixedRouter, caller: str, endpoint_id: str) -> None:
    now = time.monotonic()
    router._affinity[(caller, MODEL)] = _Affinity(
        endpoint_id=endpoint_id, expires_at=now + 300, created_at=now
    )


async def _call(router: FixedRouter, caller: str, prompt: str, *, stream: bool) -> str:
    """Send one request as ``caller`` and return the endpoint that served it."""
    messages = [{"role": "user", "content": prompt}]
    with req_ctx.push(affinity_key=caller):
        if not stream:
            response = await router.chat_completion(MODEL, messages)
            return response["_routing"]["endpoint_id"]
        served = None
        async for chunk in router.stream_chat_completion(MODEL, messages):
            payload = json.loads(chunk.removeprefix("data: "))
            routing = payload.get("_routing")
            if routing is not None and not payload.get("choices"):
                served = routing["endpoint_id"]
        assert served is not None
        return served


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


# ---------------------------------------------------------------------------
# FixedRouter end to end
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_request_still_in_the_gateway_moves_no_pinned_caller(stream):
    """The finding: an unsent large prompt pushed every warm caller onto a cold engine."""
    a, b = _Engine("a"), _Engine("b")
    a.held_caller, a.hold_before_send = "stalled", True
    router = _router(a, b)
    _pin(router, "victim", "m:a")
    _pin(router, "stalled", "m:a")

    stalled = asyncio.create_task(_call(router, "stalled", LARGE_PROMPT, stream=stream))
    await _until(lambda: router.prefill_load.backlog("m:a") > 0)
    # Selection still sees the reservation, so concurrent requests spread...
    assert router.prefill_load.backlog("m:a") == 175_000
    # ...but nothing has reached the engine, so no pin yields to it.
    assert router.prefill_load.dispatched_backlog("m:a") == 0

    assert await _call(router, "victim", SMALL_PROMPT, stream=stream) == "m:a"
    assert router._affinity[("victim", MODEL)].endpoint_id == "m:a"

    a.release.set()
    assert await stalled == "m:a"
    assert router.prefill_load.backlog("m:a") == 0
    assert router.prefill_load.dispatched_backlog("m:a") == 0


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_sent_request_moves_a_pinned_caller_for_one_request_only(stream):
    """Once the prompt is at the engine the ceiling applies, and the pin survives it."""
    a, b = _Engine("a"), _Engine("b")
    a.held_caller = "big"
    router = _router(a, b)
    _pin(router, "victim", "m:a")
    _pin(router, "big", "m:a")

    big = asyncio.create_task(_call(router, "big", LARGE_PROMPT, stream=stream))
    await _until(lambda: router.prefill_load.dispatched_backlog("m:a") > 0)
    assert router.prefill_load.dispatched_backlog("m:a") == 175_000

    # This request is spared the queue on A...
    assert await _call(router, "victim", SMALL_PROMPT, stream=stream) == "m:b"
    # ...and the caller keeps its pin, so it returns to its warm prefix.
    assert router._affinity[("victim", MODEL)].endpoint_id == "m:a"

    a.release.set()
    await big
    assert await _call(router, "victim", SMALL_PROMPT, stream=stream) == "m:a"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_charge_never_exceeds_the_route_context_window(stream):
    """A prompt longer than the window cannot make the engine prefill more than it."""
    a, b = _Engine("a", context_length=131_072), _Engine("b", context_length=131_072)
    a.held_caller = "big"
    router = _router(a, b)
    _pin(router, "victim", "m:a")
    _pin(router, "big", "m:a")

    big = asyncio.create_task(_call(router, "big", LARGE_PROMPT, stream=stream))
    await _until(lambda: router.prefill_load.dispatched_backlog("m:a") > 0)
    assert router.prefill_load.backlog("m:a") == 131_072
    assert router.prefill_load.dispatched_backlog("m:a") == 131_072
    # Under the 150k ceiling once capped: one such request moves nobody.
    assert await _call(router, "victim", SMALL_PROMPT, stream=stream) == "m:a"

    a.release.set()
    await big


# ---------------------------------------------------------------------------
# The tracker and the dispatch report
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_undispatched_lease_breaks_no_pin_until_it_is_sent():
    tracker = PrefillLoadTracker()
    lease = tracker.acquire("e", 500_000, affinity_key="other", dispatched=False)
    assert tracker.backlog("e") == 500_000
    assert tracker.dispatched_backlog("e") == 0
    assert tracker.should_keep_affinity("e", affinity_key="me", ceiling=1000)

    tracker.mark_dispatched(lease)
    tracker.mark_dispatched(lease)  # idempotent
    assert tracker.dispatched_backlog("e") == 500_000
    assert not tracker.should_keep_affinity("e", affinity_key="me", ceiling=1000)
    # Still never the caller's own.
    assert tracker.should_keep_affinity("e", affinity_key="other", ceiling=1000)

    # Back in the gateway (another key's slot after a refusal): off the engine.
    tracker.mark_dispatched(lease, dispatched=False)
    assert tracker.should_keep_affinity("e", affinity_key="me", ceiling=1000)

    tracker.mark_dispatched(lease)
    tracker.release(lease)
    assert tracker.backlog("e") == 0
    assert tracker.dispatched_backlog("e") == 0
    tracker.mark_dispatched(lease, dispatched=False)
    tracker.mark_dispatched(lease)  # a report after release changes nothing
    assert tracker.dispatched_backlog("e") == 0
    assert tracker.should_keep_affinity("e", affinity_key="me", ceiling=1000)


@pytest.mark.unit
def test_selection_still_spreads_over_an_undispatched_lease():
    tracker = PrefillLoadTracker()
    tracker.acquire("busy", 500_000, dispatched=False)
    draws = iter([0.1, 0.9])  # first draw "busy", second "idle"
    assert tracker.select_index(["busy", "idle"], [0.5, 0.5], 1000, lambda: next(draws)) == 1


@pytest.mark.unit
def test_a_lease_charges_at_most_the_context_window():
    tracker = PrefillLoadTracker()
    lease = tracker.acquire("e", 175_000, context_window=131_072)
    assert lease.tokens == 131_072
    assert tracker.backlog("e") == 131_072
    assert tracker.dispatched_backlog("e") == 131_072
    # The prompt itself is still what a completed prefill remembers.
    assert lease.prompt_tokens == 175_000


class _Watch:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def on_queued(self) -> None:
        self.calls.append("on_queued")

    def on_sent(self) -> None:
        self.calls.append("on_sent")

    def on_first_token(self) -> None:
        self.calls.append("on_first_token")


@pytest.mark.unit
def test_the_dispatch_report_follows_the_request_and_feeds_the_watch():
    tracker = PrefillLoadTracker()
    watch = _Watch()
    report = _DispatchReport(tracker, watch)
    report.attach(tracker.acquire("e", 1000, dispatched=False))

    with req_ctx.push(**{req_ctx.UPSTREAM_DISPATCH_WATCH: report}):
        report_queued()  # a media fetch
        assert (tracker.dispatched_backlog("e"), report.sent) == (0, False)
        report_sent()
        assert (tracker.dispatched_backlog("e"), report.sent) == (1000, True)
        report_queued()  # waiting for another key's slot
        assert (tracker.dispatched_backlog("e"), report.sent) == (0, False)
        report_sent()
        report_first_token()

    assert tracker.dispatched_backlog("e") == 1000
    assert watch.calls == ["on_queued", "on_sent", "on_queued", "on_sent", "on_first_token"]


@pytest.mark.unit
def test_a_lease_attached_after_the_send_counts_at_once():
    tracker = PrefillLoadTracker()
    report = _DispatchReport(tracker)
    report.on_sent()
    report.attach(tracker.acquire("e", 1000, dispatched=False))
    assert tracker.dispatched_backlog("e") == 1000
