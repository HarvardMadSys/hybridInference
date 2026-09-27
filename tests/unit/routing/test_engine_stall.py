"""The pieces of engine-stall offload that stand alone (``routing.engine_stall``).

The per-endpoint tracker, the per-attempt first-token watch and its deadline, and
how the fallback order and the circuit breaker read an attempt that waited out
its engine. ``test_engine_stall_routing.py`` drives them through FixedRouter.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from routing.endpoint_health import EndpointHealthRegistry
from routing.engine_stall import EngineStallTracker, EngineWaitExpired, FirstTokenWatch
from routing.offload import (
    OFFLOAD_ENGINE_WAIT,
    OFFLOAD_LAST_RESORT,
    OFFLOAD_QUEUE_WAIT,
    FallbackOrder,
    offload_reason_for,
)
from routing.routers import FixedRouter
from serving.adapters.upstream_limiter import UpstreamQueueWaitExpired, UpstreamSaturated


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ tracker


def test_an_answered_attempt_leaves_nothing_behind():
    tracker = EngineStallTracker()
    token = tracker.begin("e")
    tracker.answered("e", token)

    assert tracker.admits("e", probe=False)
    assert tracker.snapshot() == {}
    assert tracker._endpoints == {}


def test_an_expired_attempt_stalls_its_endpoint():
    clock = _Clock()
    tracker = EngineStallTracker(clock=clock)
    token = tracker.begin("e")
    clock.now += 2.0
    tracker.expired("e", token, model_id="m", wait_seconds=1.5)
    clock.now += 3.0

    assert tracker.is_stalled("e")
    assert tracker.snapshot() == {
        "e": {"model_id": "m", "wait_seconds": 1.5, "stalled_seconds": 3.0, "pending": 0}
    }


def test_a_stalled_endpoint_takes_no_request_but_a_lone_streaming_probe():
    tracker = EngineStallTracker()
    tracker.expired("e", tracker.begin("e"), model_id="m", wait_seconds=1.0)

    # Nothing of ours is left on it: a stream may probe, a non-stream may not.
    assert tracker.admits("e", probe=True)
    assert not tracker.admits("e", probe=False)

    probe = tracker.begin("e")
    # The probe is now ahead of anything new in the engine's queue.
    assert not tracker.admits("e", probe=True)

    tracker.abandoned("e", probe)
    assert tracker.admits("e", probe=True)


def test_an_attempt_still_on_a_stalled_endpoint_keeps_new_ones_away():
    tracker = EngineStallTracker()
    earlier = tracker.begin("e")
    tracker.expired("e", tracker.begin("e"), model_id="m", wait_seconds=1.0)

    assert not tracker.admits("e", probe=True)
    assert tracker.snapshot()["e"]["pending"] == 1
    assert tracker.is_stalled("e")
    tracker.answered("e", earlier)
    assert not tracker.is_stalled("e")


def test_any_answer_ends_a_stall_and_is_logged(caplog):
    clock = _Clock()
    tracker = EngineStallTracker(clock=clock)
    waiting = tracker.begin("e")
    tracker.expired("e", tracker.begin("e"), model_id="m", wait_seconds=1.0)
    clock.now += 4.0

    with caplog.at_level("INFO", logger="routing.engine_stall"):
        tracker.answered("e", waiting)

    assert not tracker.is_stalled("e")
    assert tracker.admits("e", probe=False)
    (record,) = [r for r in caplog.records if r.getMessage() == "engine_recovered"]
    assert record.endpoint_id == "e"
    assert record.stalled_seconds == 4.0


def test_an_abandoned_attempt_neither_starts_nor_ends_a_stall():
    tracker = EngineStallTracker()
    tracker.abandoned("e", tracker.begin("e"))
    assert not tracker.is_stalled("e")

    tracker.expired("e", tracker.begin("e"), model_id="m", wait_seconds=1.0)
    tracker.abandoned("e", tracker.begin("e"))
    assert tracker.is_stalled("e")


def test_each_expiry_is_logged_but_only_the_first_starts_the_stall(caplog):
    clock = _Clock()
    tracker = EngineStallTracker(clock=clock)
    first, second = tracker.begin("e"), tracker.begin("e")
    with caplog.at_level("INFO", logger="routing.engine_stall"):
        tracker.expired("e", first, model_id="m", wait_seconds=1.0)
        clock.now += 5.0
        tracker.expired("e", second, model_id="m", wait_seconds=1.0)

    assert [r.newly_stalled for r in caplog.records if r.getMessage() == "engine_stalled"] == [
        True,
        False,
    ]
    assert tracker.snapshot()["e"]["stalled_seconds"] == 5.0


def test_stalled_lists_only_stalled_endpoints_in_the_order_asked():
    tracker = EngineStallTracker()
    for endpoint_id in ("b", "a"):
        tracker.expired(endpoint_id, tracker.begin(endpoint_id), model_id="m", wait_seconds=1.0)
    tracker.begin("c")

    assert tracker.stalled(["a", "c", "b", "d"]) == ["a", "b"]


# -------------------------------------------------------------------- watch


def test_a_watch_settles_its_attempt_once():
    tracker = EngineStallTracker()
    watch = FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=1.0)
    assert tracker.snapshot() == {} and tracker._endpoints["e"].pending

    watch.record_expiry()
    watch.record_answer()
    watch.close()

    assert tracker.is_stalled("e")
    assert not tracker._endpoints["e"].pending


def test_closing_an_unsettled_watch_abandons_the_attempt():
    tracker = EngineStallTracker()
    FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=1.0).close()

    assert tracker._endpoints == {}


async def test_the_deadline_cancels_the_waiting_task_and_claims_it():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=0.02)
    watch.start()
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        assert watch.owns_cancellation()
    else:
        pytest.fail("the deadline did not fire")
    finally:
        watch.stop()
    # The task is not left marked as being cancelled.
    await asyncio.sleep(0)


async def test_a_cancellation_from_elsewhere_is_not_the_deadlines():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=5.0)

    async def wait() -> bool:
        watch.start()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return watch.owns_cancellation()
        finally:
            watch.stop()
        return True

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    task.cancel()
    assert await task is False


@pytest.mark.skipif(sys.version_info < (3, 11), reason="needs Task.uncancel")
async def test_a_disconnect_landing_with_the_deadline_still_cancels():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=5.0)

    async def wait() -> bool:
        watch.start()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return watch.owns_cancellation()
        finally:
            watch.stop()
        return True

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    watch._fire()  # the deadline passes...
    task.cancel()  # ...as the client hangs up
    assert await task is False


async def test_queueing_pauses_the_deadline_and_sending_restarts_it():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=0.05)
    watch.start()
    watch.on_queued()
    await asyncio.sleep(0.15)  # three waits' worth in the gateway queue: no expiry

    loop = asyncio.get_running_loop()
    watch.on_sent()
    sent_at = loop.time()
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        assert watch.owns_cancellation()
        waited = loop.time() - sent_at
    finally:
        watch.stop()
    assert 0.04 <= waited < 1.0


async def test_a_watch_without_a_wait_never_fires():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=None)
    watch.start()
    watch.on_sent()
    await asyncio.sleep(0.05)
    assert not watch.owns_cancellation()
    watch.close()


async def test_a_stopped_watch_ignores_the_limiter():
    watch = FirstTokenWatch(EngineStallTracker(), "e", model_id="m", wait_seconds=0.01)
    watch.start()
    watch.stop()
    watch.on_sent()
    await asyncio.sleep(0.05)
    assert not watch.owns_cancellation()


# ----------------------------------------------------- awaiting the first token


def _chunk(content: str | None = None, role: str | None = None) -> str:
    import json

    delta: dict[str, str] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return f"data: {json.dumps({'choices': [{'index': 0, 'delta': delta}]})}\n\n"


async def _stream(*chunks: str, stall_after: int | None = None, closed: list | None = None):
    try:
        for index, chunk in enumerate(chunks):
            if stall_after is not None and index == stall_after:
                await asyncio.sleep(5)
            yield chunk
    finally:
        if closed is not None:
            closed.append(True)


async def test_chunks_before_the_first_token_are_held_until_it_arrives():
    tracker = EngineStallTracker()
    watch = FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=1.0)
    stream = _stream(_chunk(role="assistant"), _chunk("hi"), _chunk(" there"))

    held = await FixedRouter._await_first_token(stream, watch)

    assert held == [_chunk(role="assistant"), _chunk("hi")]
    assert [chunk async for chunk in stream] == [_chunk(" there")]
    assert tracker._endpoints == {}


async def test_a_stream_that_ends_before_a_token_counts_as_answered():
    tracker = EngineStallTracker()
    tracker.expired("e", tracker.begin("e"), model_id="m", wait_seconds=1.0)
    watch = FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=1.0)

    held = await FixedRouter._await_first_token(_stream(_chunk(role="assistant")), watch)

    assert held == [_chunk(role="assistant")]
    assert not tracker.is_stalled("e")


async def test_no_first_token_in_time_unwinds_the_stream_and_stalls_the_endpoint():
    tracker = EngineStallTracker()
    watch = FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=0.02)
    closed: list = []
    stream = _stream(_chunk(role="assistant"), _chunk("hi"), stall_after=1, closed=closed)

    with pytest.raises(EngineWaitExpired) as excinfo:
        await FixedRouter._await_first_token(stream, watch)

    assert excinfo.value.endpoint_id == "e"
    assert excinfo.value.wait_seconds == 0.02
    assert closed == [True]
    assert tracker.is_stalled("e")
    assert not tracker._endpoints["e"].pending


async def test_an_upstream_error_before_the_token_is_not_a_stall():
    tracker = EngineStallTracker()
    watch = FirstTokenWatch(tracker, "e", model_id="m", wait_seconds=1.0)

    async def failing():
        yield _chunk(role="assistant")
        raise RuntimeError("upstream 500")

    with pytest.raises(RuntimeError):
        await FixedRouter._await_first_token(failing(), watch)
    watch.close()

    assert tracker._endpoints == {}


# ---------------------------------------------------------- fallback order


def test_an_engine_wait_sends_the_request_to_the_offload_route_next():
    assert offload_reason_for(EngineWaitExpired("e", 1.0)) == OFFLOAD_ENGINE_WAIT
    assert offload_reason_for(UpstreamQueueWaitExpired("x")) == OFFLOAD_QUEUE_WAIT
    assert offload_reason_for(UpstreamSaturated("x")) == OFFLOAD_QUEUE_WAIT
    assert offload_reason_for(RuntimeError("500")) is None

    a, offload = object(), object()
    order = FallbackOrder([a], offload)
    order.record_failure(EngineWaitExpired("e", 1.0))
    assert order.next() == (offload, OFFLOAD_ENGINE_WAIT)
    assert order.next() == (a, None)
    assert order.next() is None


def test_a_deferred_candidate_comes_after_the_offload_route():
    a, b, offload = object(), object(), object()
    order = FallbackOrder([a, b], offload)

    assert order.next() == (a, None)
    assert order.defer(a)
    assert order.next() == (b, None)
    assert order.next() == (offload, OFFLOAD_LAST_RESORT)
    assert order.next() == (a, None)
    # Now that only deferred candidates are left, one is tried rather than deferred.
    assert not order.defer(a)
    assert order.next() is None


# ----------------------------------------------------------- circuit breaker


def test_an_engine_wait_is_not_charged_to_the_circuit(caplog):
    registry = EndpointHealthRegistry()
    with caplog.at_level("INFO", logger="routing.endpoint_health"):
        for _ in range(20):
            registry.record_failure("e", exc=EngineWaitExpired("e", 1.0))

    assert registry.allow_request("e")
    assert [r for r in caplog.records if r.getMessage() == "engine_wait_skip_breaker"]
