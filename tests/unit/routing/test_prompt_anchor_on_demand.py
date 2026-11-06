"""Prompt anchors are computed only for attempts whose endpoint reads them.

The anchor (``prefill_load.prompt_anchor``) proves to a later turn that it
contains this prompt, and ``_anchor_holds`` checks that proof; each is a pass
over the whole prompt, about 10 ms for a 3 MB agent history. Their one reader is
the scheduling priority, which only reaches the upstream on a route that runs
sglang priority scheduling -- off in every distribution. Before this, both ran
on every request and every attempt.

What must not change: on a priority-scheduling route the anchor, the hint it is
stored in and the published priority are exactly what they were.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from routing import prefill_load, routers as routers_module
from routing.prefill_load import PrefillLoadTracker
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_HUGE = "x" * (4 * 300_000)


def _cfg(provider: str, *, priority_scheduling: bool) -> ModelConfig:
    return ModelConfig(
        id="m",
        name="m",
        provider=provider,
        base_url=f"http://{provider}",
        context_length=8192,
        max_output_length=4096,
        priority_scheduling=priority_scheduling,
    )


class _Capturing(BaseAdapter):
    """Answers, and records the priority published for each dispatch."""

    def __init__(self, config: ModelConfig, *, fail: bool = False) -> None:
        super().__init__(config)
        self.fail = fail
        self.seen: list[Any] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.seen.append(req_ctx.get().get(req_ctx.UPSTREAM_PRIORITY))
        if self.fail:
            raise RuntimeError("down")
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.seen.append(req_ctx.get().get(req_ctx.UPSTREAM_PRIORITY))
        if self.fail:
            raise RuntimeError("down")
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _Counting:
    """Count calls to the two whole-prompt passes, still running them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.anchors = 0
        self.holds = 0
        real_anchor = prefill_load.prompt_anchor
        real_holds = prefill_load._anchor_holds

        def anchor(messages):
            self.anchors += 1
            return real_anchor(messages)

        def holds(messages, stored):
            self.holds += 1
            return real_holds(messages, stored)

        monkeypatch.setattr(routers_module, "prompt_anchor", anchor)
        monkeypatch.setattr(prefill_load, "_anchor_holds", holds)


async def _turn(router: FixedRouter, content: str, *, stream: bool) -> None:
    messages = [{"role": "user", "content": content}]
    if stream:
        async for _ in router.stream_chat_completion("m", messages):
            pass
    else:
        await router.chat_completion("m", messages)


_PATHS = pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_route_without_priority_scheduling_computes_no_anchor(monkeypatch, stream):
    counts = _Counting(monkeypatch)
    adapter = _Capturing(_cfg("plain", priority_scheduling=False))
    router = FixedRouter()
    router.register_route("m", [(adapter, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    await _turn(router, _HUGE, stream=stream)
    await _turn(router, _HUGE + "x" * 400, stream=stream)

    assert (counts.anchors, counts.holds) == (0, 0)
    # Nothing to send, so nothing is published.
    assert adapter.seen == [None, None]
    # The warm hint is still written for routing; it just carries no anchor.
    size = prefill_load.estimate_prefill_tokens([{"role": "user", "content": _HUGE}])
    assert router.prefill_load.uncached_estimate("plain", size, "u1") < size


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_priority_scheduling_route_is_unchanged(monkeypatch, stream):
    """Same anchor, same hint, same tiers as when the anchor was computed eagerly."""
    counts = _Counting(monkeypatch)
    adapter = _Capturing(_cfg("sglang", priority_scheduling=True))
    router = FixedRouter()
    router.register_route("m", [(adapter, 1.0)])
    req_ctx.set({"affinity_key": "u1"})
    first = [{"role": "user", "content": _HUGE}]

    await _turn(router, _HUGE, stream=stream)
    stored = router.prefill_load._prefix_hints[("u1", "sglang")]
    assert stored.anchor == prefill_load.prompt_anchor(first)

    await _turn(router, _HUGE + "x" * 400, stream=stream)

    assert adapter.seen == [prefill_load.PRIORITY_ELEPHANT, prefill_load.PRIORITY_INTERACTIVE]
    # One anchor per request, and the second turn checked the first's.
    assert (counts.anchors, counts.holds) == (2, 1)


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_only_the_attempt_that_reads_it_pays_for_it(monkeypatch, stream):
    """A fallback onto a priority-scheduling route computes the anchor once, for itself."""
    counts = _Counting(monkeypatch)
    plain = _Capturing(_cfg("plain", priority_scheduling=False), fail=True)
    sglang = _Capturing(_cfg("sglang", priority_scheduling=True))
    router = FixedRouter()
    router.register_route("m", [(plain, 1.0), (sglang, 1.0)])
    router._select_adapter = lambda *args, **kwargs: plain  # type: ignore[assignment]

    await _turn(router, _HUGE, stream=stream)

    assert plain.seen == [None]
    assert sglang.seen == [prefill_load.PRIORITY_ELEPHANT]
    assert counts.anchors == 1


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_the_anchor_is_not_computed_inside_the_routing_transaction(monkeypatch, stream):
    """Every request's selection waits on that transaction; a whole-prompt pass does not belong in it."""

    class _Tracker(PrefillLoadTracker):
        depth = 0

        @contextlib.contextmanager
        def routing_transaction(self):
            with super().routing_transaction():
                self.depth += 1
                try:
                    yield
                finally:
                    self.depth -= 1

    tracker = _Tracker()
    depths: list[int] = []
    real_anchor = prefill_load.prompt_anchor

    def anchor(messages):
        depths.append(tracker.depth)
        return real_anchor(messages)

    monkeypatch.setattr(routers_module, "prompt_anchor", anchor)
    failing = _Capturing(_cfg("first", priority_scheduling=True), fail=True)
    answering = _Capturing(_cfg("second", priority_scheduling=True))
    router = FixedRouter()
    router._prefill_load = tracker
    router.register_route("m", [(failing, 1.0), (answering, 1.0)])
    router._select_adapter = lambda *args, **kwargs: failing  # type: ignore[assignment]

    await _turn(router, _HUGE, stream=stream)

    assert depths == [0]
    assert answering.seen == [prefill_load.PRIORITY_ELEPHANT]
