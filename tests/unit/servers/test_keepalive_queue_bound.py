"""Regression tests for the bounded hand-off queues in the forced-streaming
chain (``_streaming_response_with_keepalive`` + ``StreamSession.stream``).

Before the fix both queues were unbounded: with the consumer stalled
(slow/stopped client applying transport backpressure), the readers kept
buffering every chunk — a second full copy of the generation in memory,
growing for as long as the client stalls. Reproduced pre-fix: 2000-chunk
upstream + 0.3 s consumer stall left 1941 chunks queued in the outer queue
alone; the inner queue (``StreamSession.stream``) silently inherited the
buffering once the outer queue was bounded.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import completions as comp
from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.completions_stream import StreamSession
from serving.servers.routers.routing_info import RoutingInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX = comp._KEEPALIVE_QUEUE_MAXSIZE


def _chunk(text: str) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-1",
                "created": 1,
                "model": "m",
                "choices": [{"delta": {"content": text}}],
            }
        )
        + "\n\n"
    )


async def _fast_upstream(count: int, close_flag: list[bool] | None = None):
    try:
        for _ in range(count):
            await asyncio.sleep(0)  # let the event loop interleave reader/consumer
            yield _chunk("x")
    except (GeneratorExit, asyncio.CancelledError):
        if close_flag is not None:
            close_flag.append(True)
        raise


async def _burst_upstream(count: int, close_flag: list[bool] | None = None) -> Any:
    """Fast producer whose FIRST chunk arrives after the keepalive window.

    Delaying the first chunk past the (shrunken) keepalive interval makes the
    outer generator's first ``__anext__`` return a ``b" "`` keepalive
    deterministically — with a pure ``sleep(0)`` producer the generator can
    chew the whole stream without ever suspending and return the final JSON
    body as its first item, which would make the "stalled consumer" setup a
    no-op.
    """
    try:
        await asyncio.sleep(0.05)
        for _ in range(count):
            await asyncio.sleep(0)
            yield _chunk("x")
    except (GeneratorExit, asyncio.CancelledError):
        if close_flag is not None:
            close_flag.append(True)
        raise


class _Headers(dict):
    pass


def _make_gen(upstream: Any) -> Any:
    return comp._streaming_response_with_keepalive(
        upstream, request_id="rid", model="m", request_headers=_Headers()
    )


def _make_session() -> StreamSession:
    """Minimal StreamSession, mirroring test_completions_stream._make_session."""
    cost_tracker = MagicMock(spec=CostTracker)

    async def _identity_increment(**kwargs: Any) -> Any:
        return kwargs["routing"]

    cost_tracker.schedule_increment = AsyncMock(side_effect=_identity_increment)
    pricing_lookup = MagicMock(spec=PricingLookup)
    pricing_lookup.raw_dict_for_routing = MagicMock(return_value=None)
    return StreamSession(
        routing=RoutingInfo(request_id="rid", model="m"),
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        params={"stream": True},
        request_id="rid",
        start_time=time.time(),
        request_headers=_Headers(),
        metadata={},
        user_id="user-1",
        is_synthetic_probe=True,
        suppress_synthetic_logging=True,
        log_store=MagicMock(),
        active_router=MagicMock(),
        cost_tracker=cost_tracker,
        completions_logger=MagicMock(),
        pricing_lookup=pricing_lookup,
        get_adapter_config_for_provider=lambda _provider, _base_url: None,
    )


def _make_chain_gen(upstream: Any) -> Any:
    """The real forced-streaming chain: adapter stream → session → keepalive."""
    session = _make_session()
    return comp._streaming_response_with_keepalive(
        session.stream(upstream), request_id="rid", model="m", request_headers=_Headers()
    )


@contextmanager
def _spy_queues() -> Iterator[list[asyncio.Queue]]:
    """Capture every asyncio.Queue created while the context is open.

    Scoped tightly: the shared ``asyncio.Queue`` symbol is patched only for
    the statements inside the ``with`` block — long enough to construct the
    generators and pull their first item (where the queues are created) — and
    restored immediately after, so the rest of the event loop never sees it.
    """
    captured: list[asyncio.Queue] = []
    real_queue = asyncio.Queue

    class SpyQueue(real_queue):  # type: ignore[type-arg,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured.append(self)

    asyncio.Queue = SpyQueue  # type: ignore[assignment]
    try:
        yield captured
    finally:
        asyncio.Queue = real_queue


def _chain_queues(queues: list[asyncio.Queue]) -> tuple[asyncio.Queue, asyncio.Queue]:
    """Return ``(outer, inner)`` from the captured queues.

    Creation order is deterministic in the chain: the keepalive generator
    builds its own queue before starting the reader task that first iterates
    ``session.stream`` (which builds the inner one).
    """
    bounded = [q for q in queues if q.maxsize == _MAX]
    assert len(bounded) == 2, f"expected exactly 2 bounded queues, got {len(bounded)}"
    return bounded[0], bounded[1]


def _fast_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the keepalive window so the queue-vs-consumer race is observable."""
    monkeypatch.setattr(comp, "_FORCE_STREAMING_KEEPALIVE_S", 0.01)


async def _assert_no_leaked_tasks() -> None:
    """Fail if any task other than the current one is still pending.

    A few loop ticks first so cancellation callbacks and task finalizers run.
    """
    for _ in range(3):
        await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert not pending, f"leaked tasks: {pending}"


async def _close_and_assert_completion(gen: Any, timeout: float = 5.0) -> None:
    """Drive ``gen.aclose()`` as a task and require it to actually finish.

    ``asyncio.wait_for`` cancels the awaited coroutine on timeout, which would
    silently mask a deadlocked close (the very bug this suite guards against).
    So a timeout here is a hard failure: if ``wait_for`` had to cancel the
    close task, the close did not complete on its own.
    """
    close_task = asyncio.create_task(gen.aclose())
    try:
        await asyncio.wait_for(close_task, timeout=timeout)
    except asyncio.TimeoutError:
        close_task.cancel()
        with suppress(asyncio.CancelledError):
            await close_task
        pytest.fail("aclose() did not finish in time — close is deadlocked")
    assert not close_task.cancelled(), "aclose() task was cancelled instead of finishing"
    exc = close_task.exception()
    assert exc is None or isinstance(exc, (GeneratorExit, asyncio.CancelledError)), (
        f"aclose() raised unexpectedly: {exc!r}"
    )


@pytest.mark.asyncio
async def test_slow_consumer_bounds_queued_chunks(monkeypatch):
    """A stalled consumer must not leave the readers buffering without limit."""
    _fast_keepalive(monkeypatch)
    with _spy_queues() as queues:
        gen = _make_gen(_fast_upstream(2000))
        first = await gen.__anext__()  # first keepalive byte
    assert first == b" "
    # Consumer stalls here (client stopped reading); upstream keeps
    # producing into the queue.
    await asyncio.sleep(0.3)

    assert len(queues) == 1
    q = queues[0]
    assert q.qsize() <= _MAX

    # The bound is backpressure, not data loss: draining the generator
    # to completion still yields the whole generation in the final body.
    items: list[bytes] = [first]
    async for item in gen:
        items.append(item)

    body = json.loads(items[-1].decode())
    assert body["choices"][0]["message"]["content"] == "x" * 2000


@pytest.mark.asyncio
async def test_closing_while_reader_blocked_on_full_queue_does_not_hang(monkeypatch):
    """Closing the generator with the reader blocked on a full queue must end it.

    The old reader's ``finally: await queue.put(None)`` would block forever on
    a full bounded queue once the consumer was gone, leaking the task and the
    upstream connection. The close is driven as a task and must *complete*:
    if ``wait_for`` had to cancel it, the test fails — a cancelled close is a
    masked deadlock, not a pass.
    """
    _fast_keepalive(monkeypatch)
    close_flag: list[bool] = []
    with _spy_queues() as queues:
        gen = _make_gen(_fast_upstream(5000, close_flag))
        await gen.__anext__()
    await asyncio.sleep(0.1)  # fill the queue; reader blocks on put

    assert queues[0].qsize() <= _MAX
    await _close_and_assert_completion(gen)
    assert close_flag == [True]  # the upstream generator was closed, not leaked
    await _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_upstream_exception_still_reaches_the_consumer_with_bound(monkeypatch):
    """The error path through the bounded queue is unchanged."""

    async def _fail_after_first():
        yield _chunk("hello")
        raise RuntimeError("upstream blew up")

    # Default keepalive window: the consumer receives the exception long
    # before the keepalive timer fires, exercising the clean re-raise path.
    slow = _fail_after_first()

    async def run() -> None:
        agen = comp._streaming_response_with_keepalive(
            slow, request_id="rid", model="m", request_headers=_Headers()
        )
        # Nothing was yielded before the failure, so the clean re-raise path
        # surfaces from the very first item pulled.
        with pytest.raises(RuntimeError):
            await agen.__anext__()

    await asyncio.wait_for(run(), timeout=5.0)


# ---------------------------------------------------------------------------
# Chain-bound regression: the inner queue (StreamSession.stream) must be
# bounded too, and the whole chain must tear down cleanly under cancellation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_slow_consumer_bounds_both_queues(monkeypatch):
    """A stalled consumer fills BOTH bounded queues, then backpressure stops.

    The inner (adapter→session) queue is the actual regression: once the outer
    queue was bounded, an unbounded inner queue would silently relocate the
    buffering one hop upstream and backpressure would never reach the provider.
    """
    _fast_keepalive(monkeypatch)  # let the outer generator yield so a real stall exists
    with _spy_queues() as queues:
        gen = _make_chain_gen(_burst_upstream(5000))
        first = await gen.__anext__()
    assert first == b" "
    outer, inner = _chain_queues(queues)
    await asyncio.sleep(0.3)  # fill both queues; both readers park on put

    assert inner.qsize() <= _MAX, "inner (adapter) queue grew past its bound"
    assert outer.qsize() <= _MAX, "outer (keepalive) queue grew past its bound"

    # Backpressure, not data loss: draining to completion still delivers
    # the whole generation.
    items = [item async for item in gen]
    body = json.loads(items[-1].decode())
    assert body["choices"][0]["message"]["content"] == "x" * 5000
    await _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_scenario_a_blocked_producer_reader_exits_when_consumer_closes():
    """(a) Blocked producer: inner queue full, adapter reader parked on put.

    The consumer of ``session.stream`` closes while the adapter reader is
    blocked on ``put``: the reader must be cancelled without blocking on the
    sentinel, and the upstream generator must be closed, not leaked.
    """
    close_flag: list[bool] = []
    with _spy_queues() as queues:
        upstream = _fast_upstream(5000, close_flag)
        session = _make_session()
        gen = session.stream(upstream)
        await gen.__anext__()
    assert len(queues) == 1
    inner = queues[0]
    await asyncio.sleep(0.2)  # fill the inner queue; adapter reader parks on put
    assert inner.qsize() == _MAX, f"expected a full inner queue, got {inner.qsize()}"

    await _close_and_assert_completion(gen)
    assert close_flag == [True]  # upstream generator closed by teardown, not leaked
    await _assert_no_leaked_tasks()  # reader exited; no pending tasks


async def _slow_upstream(count: int, interval: float, close_flag: list[bool]) -> Any:
    """A producer paced with real sleeps, so a disconnect lands mid-stream."""
    try:
        for _ in range(count):
            await asyncio.sleep(interval)
            yield _chunk("x")
    except (GeneratorExit, asyncio.CancelledError):
        # Either a close (GeneratorExit) or a task cancellation delivered into
        # the generator mid-iteration: in both cases the producer terminated.
        close_flag.append(True)
        raise


async def _drain_discard(gen: Any) -> int:
    """Consume a generator discarding output; used to simulate a live client."""
    n = 0
    async for _item in gen:
        n += 1
    return n


@pytest.mark.asyncio
async def test_scenario_b_client_disconnect_mid_stream_terminates_both_readers(
    monkeypatch,
):
    """(b) Client disconnect: the consumer task is cancelled mid-stream.

    Mirrors uvicorn cancelling the ASGI response task: the chain is mid-flight
    on a slow upstream when the task driving the outer generator is cancelled.
    The generator teardown must reach BOTH readers (each exits via the
    cancellation guard, without blocking on a full queue) and leave no task
    behind.
    """
    _fast_keepalive(monkeypatch)  # shrink the window so the first anext yields keepalive
    close_flag: list[bool] = []
    with _spy_queues() as queues:
        gen = _make_chain_gen(_slow_upstream(200, 0.05, close_flag))
        first = await gen.__anext__()
    assert first == b" "  # first chunk only arrives at 50ms, window is 10ms
    outer, inner = _chain_queues(queues)

    consumer_task = asyncio.create_task(_drain_discard(gen))
    await asyncio.sleep(0.12)  # mid-stream: ~2 chunks in, queues in use
    assert inner.qsize() <= _MAX
    assert outer.qsize() <= _MAX

    consumer_task.cancel()
    with suppress(asyncio.CancelledError):
        await consumer_task
    assert consumer_task.cancelled()

    assert close_flag == [True], "upstream generator was not closed by the teardown"
    await _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_scenario_c_generator_closure_while_both_queues_full(monkeypatch):
    """(c) aclose() on the outer generator while both queues are full.

    The strictest teardown: every queue is at its bound, every reader is
    parked on ``put``, and the consumer asks for closure. Everything must
    terminate without deadlock and without leaking tasks.
    """
    _fast_keepalive(monkeypatch)  # shrink the window so the first anext yields keepalive
    close_flag: list[bool] = []
    with _spy_queues() as queues:
        gen = _make_chain_gen(_burst_upstream(5000, close_flag))
        first = await gen.__anext__()
    assert first == b" "
    outer, inner = _chain_queues(queues)
    await asyncio.sleep(0.3)  # fill both queues to the bound
    assert inner.qsize() == _MAX, f"inner queue not full: {inner.qsize()}"
    assert outer.qsize() == _MAX, f"outer queue not full: {outer.qsize()}"

    await _close_and_assert_completion(gen)
    assert close_flag == [True]  # the upstream was closed, not leaked
    await _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_scenario_d_upstream_cancelled_while_queue_full_and_consumer_stalled():
    """(d) Upstream cancellation with the inner queue full and consumer stalled.

    The producer blocks on a provider task after emitting exactly enough
    chunks to fill the queue: the reader has pulled one more chunk (which
    parks the *generator* on the provider) and the queue is at its bound.
    Cancelling the provider task raises CancelledError through the adapter
    reader's ``async for`` — the guard must skip the sentinel put, the reader
    must exit, and no task may be left pending.
    """
    provider_gone = asyncio.Event()
    provider_task = asyncio.create_task(provider_gone.wait())

    async def _upstream() -> Any:
        for _ in range(_MAX):
            await asyncio.sleep(0)
            yield _chunk("x")
        # The provider connection this generator "reads from": hangs until the
        # test cancels it, surfacing CancelledError through ``async for``.
        await provider_task

    with _spy_queues() as queues:
        session = _make_session()
        gen = session.stream(_upstream())
        await gen.__anext__()  # consumer takes one chunk, then stalls
    inner = queues[0]
    await asyncio.sleep(0.2)  # queue fills to the bound; generator hangs on provider
    assert inner.qsize() == _MAX - 1, (
        f"expected a full inner queue minus the consumed chunk, got {inner.qsize()}"
    )

    provider_task.cancel()
    with suppress(asyncio.CancelledError):
        await provider_task
    assert provider_task.cancelled()

    # The reader observed the cancellation through ``async for`` and exited
    # (the guard skipped the sentinel). Close the stalled consumer cleanly.
    await _close_and_assert_completion(gen)
    await _assert_no_leaked_tasks()
