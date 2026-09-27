"""The pieces of the engine wait that stand alone (``routing.engine_wait``).

The per-attempt first-token watch and its deadline, the router's wait for the
first token, and how the fallback order and the circuit breaker read an attempt
that waited out its engine. ``test_engine_wait_routing.py`` drives them through
FixedRouter.
"""

from __future__ import annotations

import asyncio

import pytest

from routing import engine_wait
from routing.endpoint_health import EndpointHealthRegistry
from routing.engine_wait import EngineWaitExpired, FirstTokenWatch
from routing.offload import (
    OFFLOAD_ENGINE_WAIT,
    OFFLOAD_QUEUE_WAIT,
    FallbackOrder,
    offload_reason_for,
)
from routing.routers import FixedRouter
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import UpstreamQueueWaitExpired, UpstreamSaturated
from serving.utils import context as req_ctx

#: The first-token wait is enforced with ``asyncio.timeout`` (Python 3.11+).
needs_timeout = pytest.mark.skipif(
    not hasattr(asyncio, "timeout"), reason="the first-token wait needs asyncio.timeout"
)


# -------------------------------------------------------------------- watch


async def _wait_under(watch: FirstTokenWatch, seconds: float) -> None:
    async with watch.deadline() as deadline:
        watch.arm(deadline)
        await asyncio.sleep(seconds)


@needs_timeout
async def test_the_deadline_times_out_the_wait():
    watch = FirstTokenWatch("e", wait_seconds=0.02)

    with pytest.raises(TimeoutError):
        await _wait_under(watch, 5)

    assert watch.expired()


@needs_timeout
async def test_a_cancellation_from_elsewhere_is_not_the_deadlines():
    watch = FirstTokenWatch("e", wait_seconds=5.0)
    task = asyncio.create_task(_wait_under(watch, 5))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not watch.expired()


@needs_timeout
@pytest.mark.parametrize("deadline_first", [True, False], ids=["deadline-first", "client-first"])
async def test_a_disconnect_landing_with_the_deadline_stays_a_cancellation(deadline_first):
    """Both cancellations reach the task before it resumes, in either order.

    A cancellation the deadline did not ask for alone must never become an
    offload: the client is gone, and an offload request would be one nobody
    reads. Python 3.10 merges the two, which is why the wait needs 3.11+.
    """
    watch = FirstTokenWatch("e", wait_seconds=5.0)
    task = asyncio.create_task(_wait_under(watch, 5))
    await asyncio.sleep(0)

    watch._deadline.reschedule(asyncio.get_running_loop().time())  # the deadline passes
    if deadline_first:
        await asyncio.sleep(0)  # and cancels the waiting task first
    task.cancel()  # the client hangs up before that task resumes

    with pytest.raises(asyncio.CancelledError):
        await task


@needs_timeout
async def test_queueing_pauses_the_deadline_and_sending_restarts_it():
    watch = FirstTokenWatch("e", wait_seconds=0.05)
    loop = asyncio.get_running_loop()

    with pytest.raises(TimeoutError):
        async with watch.deadline() as deadline:
            watch.arm(deadline)
            watch.on_queued()
            await asyncio.sleep(0.15)  # three waits' worth in the gateway queue: no expiry
            watch.on_sent()
            sent_at = loop.time()
            await asyncio.sleep(5)

    assert 0.04 <= loop.time() - sent_at < 1.0


@needs_timeout
async def test_the_first_output_ends_the_deadline_for_good():
    watch = FirstTokenWatch("e", wait_seconds=0.05)

    async with watch.deadline() as deadline:
        watch.arm(deadline)
        watch.on_first_token()
        await asyncio.sleep(0.15)  # three waits' worth: the engine is answering
        watch.on_sent()  # a late report cannot bring the deadline back
        assert deadline.when() is None

    assert not watch.expired()


@needs_timeout
async def test_a_stopped_watch_ignores_every_report():
    watch = FirstTokenWatch("e", wait_seconds=5.0)

    async with watch.deadline() as deadline:
        watch.arm(deadline)
        armed_for = deadline.when()
        watch.stop()
        watch.on_queued()
        watch.on_sent()
        watch.on_first_token()
        assert deadline.when() == armed_for


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
    watch = FirstTokenWatch("e", wait_seconds=1.0)
    stream = _stream(_chunk(role="assistant"), _chunk("hi"), _chunk(" there"))

    held = await FixedRouter._await_first_token(stream, watch)

    assert held == [_chunk(role="assistant"), _chunk("hi")]
    assert [chunk async for chunk in stream] == [_chunk(" there")]


async def test_a_stream_that_ends_before_a_token_is_returned_whole():
    watch = FirstTokenWatch("e", wait_seconds=1.0)

    held = await FixedRouter._await_first_token(_stream(_chunk(role="assistant")), watch)

    assert held == [_chunk(role="assistant")]
    assert not watch.expired()


@needs_timeout
async def test_no_first_token_in_time_unwinds_the_stream():
    watch = FirstTokenWatch("e", wait_seconds=0.02)
    closed: list = []
    stream = _stream(_chunk(role="assistant"), _chunk("hi"), stall_after=1, closed=closed)

    with pytest.raises(EngineWaitExpired) as excinfo:
        await FixedRouter._await_first_token(stream, watch)

    assert excinfo.value.endpoint_id == "e"
    assert excinfo.value.wait_seconds == 0.02
    assert closed == [True]


@needs_timeout
async def test_output_the_adapter_holds_back_is_the_engine_answering():
    """A tool call a processor keeps until it is whole is not a queued request.

    The adapter reports the upstream's first output as it reads it; everything
    after that has no deadline, however long the adapter takes to yield it.
    """
    watch = FirstTokenWatch("e", wait_seconds=0.02)

    async def buffering():
        yield _chunk(role="assistant")
        report_first_token()  # the adapter read "<tool_call>" and kept it
        await asyncio.sleep(0.1)  # five waits' worth of tool call being written
        yield _chunk("<tool_call>...</tool_call>")

    with req_ctx.push(**{req_ctx.UPSTREAM_DISPATCH_WATCH: watch}):
        held = await FixedRouter._await_first_token(buffering(), watch)

    assert held[-1] == _chunk("<tool_call>...</tool_call>")
    assert not watch.expired()


async def test_an_upstream_error_before_the_token_propagates():
    watch = FirstTokenWatch("e", wait_seconds=1.0)

    async def failing():
        yield _chunk(role="assistant")
        raise RuntimeError("upstream 500")

    with pytest.raises(RuntimeError):
        await FixedRouter._await_first_token(failing(), watch)
    assert not watch.expired()


@needs_timeout
@pytest.mark.parametrize("deadline_first", [True, False], ids=["deadline-first", "client-first"])
async def test_a_client_hanging_up_as_the_deadline_passes_is_not_offloaded(deadline_first):
    """The router's side of the race: the request is cancelled, not sent elsewhere."""
    watch = FirstTokenWatch("e", wait_seconds=5.0)
    stream = _stream(_chunk(role="assistant"), _chunk("hi"), stall_after=1)
    task = asyncio.create_task(FixedRouter._await_first_token(stream, watch))
    for _ in range(3):
        await asyncio.sleep(0)  # let it hold the role delta and wait for the token

    watch._deadline.reschedule(asyncio.get_running_loop().time())
    if deadline_first:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@needs_timeout
async def test_a_timeout_the_adapter_raises_is_its_own_failure():
    watch = FirstTokenWatch("e", wait_seconds=5.0)

    async def timing_out():
        yield _chunk(role="assistant")
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError, match="read timed out"):
        await FixedRouter._await_first_token(timing_out(), watch)
    assert not watch.expired()


async def test_without_asyncio_timeout_streams_are_not_timed(monkeypatch, caplog):
    monkeypatch.setattr(engine_wait, "_asyncio_timeout", None)
    monkeypatch.setattr(engine_wait, "_warned_untimed", False)

    async def slow():
        yield _chunk(role="assistant")
        await asyncio.sleep(0.05)
        yield _chunk("hi")

    with caplog.at_level("WARNING", logger="routing.engine_wait"):
        for _ in range(2):
            watch = FirstTokenWatch("e", wait_seconds=0.01)
            held = await FixedRouter._await_first_token(slow(), watch)
            assert held[-1] == _chunk("hi")

    assert [r.getMessage() for r in caplog.records].count("engine_wait_untimed") == 1


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


# ----------------------------------------------------------- circuit breaker


def test_an_engine_wait_is_not_charged_to_the_circuit(caplog):
    registry = EndpointHealthRegistry()
    with caplog.at_level("INFO", logger="routing.endpoint_health"):
        for _ in range(20):
            registry.record_failure("e", exc=EngineWaitExpired("e", 1.0))

    assert registry.allow_request("e")
    assert [r for r in caplog.records if r.getMessage() == "engine_wait_skip_breaker"]
