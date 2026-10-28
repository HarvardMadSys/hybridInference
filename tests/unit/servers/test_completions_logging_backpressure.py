"""Regression tests for the bounded request-log queue (issue #133).

Before the fix, ``CompletionsLogger.schedule_log`` spawned one asyncio task
per payload with no admission bound: a stalled database accumulated one
pending task (plus its full prompt/response payload) per completed request,
graceful shutdown never waited for them, and queued billing rows were lost.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_logging import (
    _LOG_QUEUE_MAXSIZE,
    _LOG_WORKERS,
    CompletionsLogger,
)


def _store(hang: asyncio.Event | None = None) -> MagicMock:
    store = MagicMock()
    if hang is None:
        store.log_request = AsyncMock(return_value=None)
    else:

        async def _slow(**kwargs: object) -> None:
            await hang.wait()

        store.log_request = AsyncMock(side_effect=_slow)
    return store


async def _wait_for_writes(store: MagicMock, count: int) -> None:
    """Pump the loop until the pool has pulled ``count`` payloads."""
    for _ in range(200):
        if store.log_request.await_count >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} writes, saw {store.log_request.await_count}")


async def _wait_for_first_write(store: MagicMock) -> None:
    """Pump the loop until the worker has pulled the first payload."""
    await _wait_for_writes(store, 1)


@pytest.mark.asyncio
async def test_stalled_db_bounds_queued_payloads():
    """A stalled DB must not accumulate one pending task per request."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)

    cl.schedule_log("req-0", {"request_id": "req-0"})
    await _wait_for_first_write(store)

    total = _LOG_QUEUE_MAXSIZE + 976
    for i in range(1, total):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
        if i % 64 == 0:
            await asyncio.sleep(0)

    # A fixed worker pool instead of one task per write, and the queued
    # payload count remains capped; everything beyond the survivors was
    # dropped loudly, not silently piled up.
    assert cl._queue.qsize() <= _LOG_QUEUE_MAXSIZE
    assert len(cl._background_tasks) == _LOG_WORKERS
    # The queue plus the workers' in-flight rows hold at most the bound; no
    # per-request tasks exist.
    admitted = store.log_request.await_count + cl._queue.qsize()
    assert total - admitted == cl._dropped_count

    hang.set()
    await cl.stop()
    # Draining writes exactly the admitted survivors; drops are unchanged.
    assert store.log_request.await_count == admitted
    assert cl._dropped_count == total - admitted


@pytest.mark.asyncio
async def test_stop_drains_queued_rows_before_returning():
    """Graceful shutdown flushes every queued row exactly once."""
    cl = CompletionsLogger(log_store=_store())
    for i in range(50):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}", "n": i})
    await cl.stop()
    assert cl._log_store.log_request.await_count == 50
    assert cl._queue.qsize() == 0
    ids = [call.kwargs["request_id"] for call in cl._log_store.log_request.await_args_list]
    assert sorted(ids, key=lambda value: int(value.split("-")[1])) == [
        f"req-{i}" for i in range(50)
    ]


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_stops_the_worker():
    cl = CompletionsLogger(log_store=_store())
    cl.schedule_log("req-1", {"request_id": "req-1"})
    await cl.stop()
    await cl.stop()
    assert cl._workers is None


@pytest.mark.asyncio
async def test_concurrent_stop_calls_wait_for_the_same_drain():
    """A second stop must not mark STOPPED while the first is still draining."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    count = 8
    for i in range(count):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, _LOG_WORKERS)

    first = asyncio.create_task(cl.stop())
    for _ in range(100):
        if cl._lifecycle.value == "stopping":
            break
        await asyncio.sleep(0)
    assert cl._lifecycle.value == "stopping"

    second = asyncio.create_task(cl.stop())
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    # The second caller has not jumped the drain to publish a terminal state.
    assert cl._lifecycle.value == "stopping"
    assert cl._workers is not None

    hang.set()
    await asyncio.gather(first, second)

    assert cl._lifecycle.value == "stopped"
    assert cl._workers is None
    assert cl._in_flight == 0
    assert store.log_request.await_count == count


@pytest.mark.asyncio
async def test_cancelling_the_stop_owner_still_releases_concurrent_callers():
    """Cancelling the caller doing the drain must not strand another stop().

    Cancellation still propagates to the owner, but its finally block ends the
    pool and wakes the waiter, which then observes the same terminal state.
    """
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    for i in range(8):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, _LOG_WORKERS)

    owner = asyncio.create_task(cl.stop())
    for _ in range(100):
        if cl._lifecycle.value == "stopping":
            break
        await asyncio.sleep(0)
    assert cl._lifecycle.value == "stopping"
    waiter = asyncio.create_task(cl.stop())
    await asyncio.sleep(0)
    assert not owner.done()
    assert not waiter.done()

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.wait_for(waiter, timeout=1.0)

    assert cl._lifecycle.value == "stopped"
    assert cl._workers is None
    assert cl._in_flight == 0
    await asyncio.sleep(0)
    assert not cl._background_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduled", "expected_queued", "expected_inflight"),
    [
        # Three blocked writes and an empty queue: the old message said
        # "0 queued payloads discarded", which read as no loss even though
        # every in-flight write was cancelled.
        (3, 0, 3),
        # Both kinds of loss at once: four writes in flight, two still queued.
        (6, 2, 4),
    ],
)
async def test_stop_timeout_reports_queued_and_inflight_loss(
    caplog,
    scheduled: int,
    expected_queued: int,
    expected_inflight: int,
):
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    for i in range(scheduled):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, min(scheduled, _LOG_WORKERS))

    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        await cl.stop(timeout=0.05)

    assert "timed out" in caplog.text
    assert f"discarded {expected_queued} queued row(s)" in caplog.text
    assert f"{expected_inflight} in-flight write(s) were cancelled" in caplog.text
    assert "outcome is uncertain" in caplog.text
    timeout_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "request_log_drain_timeout"
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].queued_rows_discarded == expected_queued
    assert timeout_records[0].inflight_writes_uncertain == expected_inflight
    # The timeout path still ends the pool rather than leaking it.
    assert cl._in_flight == 0
    assert cl._workers is None
    hang.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_drop_warning_refreshes_after_a_quiet_period(monkeypatch, caplog):
    """A later small burst still emits a loss warning after a quiet period.

    The first warning is the lifetime drop #1. Without the time-based refresh
    a second burst dropping only two more rows (lifetime #2 and #3) would stay
    silent because it is neither the first nor a multiple of 100.
    """
    from serving.servers.routers import completions_logging as cl_module

    monkeypatch.setattr(cl_module, "_LOG_QUEUE_MAXSIZE", 1)
    cl = CompletionsLogger(log_store=_store())
    logger_name = "serving.servers.routers.completions_logging"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        # First burst: the second payload evicts the first and emits drop #1.
        cl.schedule_log("req-0", {"request_id": "req-0"})
        cl.schedule_log("req-1", {"request_id": "req-1"})
        initial = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "request_log_queue_saturated"
        ]
        assert len(initial) == 1
        assert initial[0].dropped_total == 1

        caplog.clear()
        # Simulate a quiet period longer than the refresh interval without
        # sleeping the test.
        cl._last_drop_warn_at = -1e9

        # Second small burst: drop #2 emits the refresh warning; drop #3 is
        # inside the interval and must not add another warning.
        cl.schedule_log("req-2", {"request_id": "req-2"})
        cl.schedule_log("req-3", {"request_id": "req-3"})
        refreshed = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "request_log_queue_saturated"
        ]
        assert len(refreshed) == 1
        assert refreshed[0].dropped_total == 2

    await cl.stop()


def test_drop_and_timeout_fields_survive_log_formatters():
    """JsonFormatter and PlainFormatter keep the new structured counts."""
    import json

    from serving.utils.logging import JsonFormatter, PlainFormatter

    def _record(**fields: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="serving.servers.routers.completions_logging",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="queue event",
            args=None,
            exc_info=None,
        )
        for key, value in fields.items():
            setattr(record, key, value)
        return record

    drop = _record(
        event="request_log_queue_saturated",
        dropped_total=3,
        queue_maxsize=1024,
    )
    drop_payload = json.loads(JsonFormatter().format(drop))
    drop_plain = PlainFormatter("%(message)s").format(drop)
    assert drop_payload["dropped_total"] == 3
    assert drop_payload["queue_maxsize"] == 1024
    assert "dropped_total=3" in drop_plain
    assert "queue_maxsize=1024" in drop_plain

    timeout = _record(
        event="request_log_drain_timeout",
        queued_rows_discarded=2,
        inflight_writes_uncertain=4,
    )
    timeout_payload = json.loads(JsonFormatter().format(timeout))
    timeout_plain = PlainFormatter("%(message)s").format(timeout)
    assert timeout_payload["queued_rows_discarded"] == 2
    assert timeout_payload["inflight_writes_uncertain"] == 4
    assert "queued_rows_discarded=2" in timeout_plain
    assert "inflight_writes_uncertain=4" in timeout_plain


@pytest.mark.asyncio
async def test_failed_write_still_emits_alert_event(caplog):
    """A failed row write keeps the tracked_task failure event contract."""
    store = MagicMock()
    store.log_request = AsyncMock(side_effect=RuntimeError("db down"))
    cl = CompletionsLogger(log_store=store)

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        cl.schedule_log("req-1", {"request_id": "req-1"})
        await cl.stop()

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "request_log"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_worker_survives_a_failed_write():
    """One bad row must not end logging for subsequent requests."""
    store = MagicMock()
    store.log_request = AsyncMock(side_effect=[RuntimeError("db down"), None, None])
    cl = CompletionsLogger(log_store=store)

    cl.schedule_log("req-1", {"request_id": "req-1"})
    cl.schedule_log("req-2", {"request_id": "req-2"})
    cl.schedule_log("req-3", {"request_id": "req-3"})
    await cl.stop()

    assert [call.kwargs["request_id"] for call in store.log_request.await_args_list] == [
        "req-1",
        "req-2",
        "req-3",
    ]


@pytest.mark.asyncio
async def test_successful_write_still_emits_alert_event():
    """A successful row write keeps the tracked_task success event contract."""
    store = _store()
    cl = CompletionsLogger(log_store=store)

    cl.schedule_log("req-1", {"foo": "bar"})
    await cl.stop()

    store.log_request.assert_awaited_once_with(foo="bar")


# ---------------------------------------------------------------------------
# Lifecycle: admission during shutdown, and a dead worker (2026-10-20 audit).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_write_during_drain_is_refused(caplog):
    """A producer calling schedule_log while stop() waits on queue.join()
    must not join the drain being flushed — the row would silently lose
    its guarantee of being written (the store is often already shutting
    down by then). Refusal is loud, on the request-log logger."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    cl.schedule_log("req-early", {"request_id": "req-early"})
    await _wait_for_first_write(store)

    stop_task = asyncio.create_task(cl.stop())
    # Let stop() reach its queue.join() await, then attempt a late write.
    await asyncio.sleep(0.01)
    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        cl.schedule_log("req-late", {"request_id": "req-late"})

    hang.set()
    await stop_task

    assert "req-late" not in [
        call.kwargs.get("request_id") for call in store.log_request.await_args_list
    ]
    assert "Request-log admission refused" in caplog.text
    # The early row was still drained.
    assert store.log_request.await_count == 1


@pytest.mark.asyncio
async def test_write_after_stop_is_refused():
    """Admission after the lifecycle reached STOPPED is refused, not queued
    into a queue nobody will ever drain."""
    cl = CompletionsLogger(log_store=_store())
    await cl.stop()

    cl.schedule_log("req-after", {"request_id": "req-after"})

    assert cl._lifecycle.value == "stopped"
    assert cl._workers is None
    assert cl._queue is None or cl._queue.qsize() == 0
    assert cl._log_store.log_request.await_count == 0


@pytest.mark.asyncio
async def test_worker_unexpected_exit_preserves_pending_logs(caplog):
    """A worker that dies unexpectedly must not leave the queue accepting
    payloads with no consumer: pending rows are preserved and consumption
    restarts over the SAME queue, exactly once."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)

    for i in range(6):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, _LOG_WORKERS)
    assert cl._workers and cl._queue is not None
    dead_worker = cl._workers[0]
    dead_worker.cancel()
    with contextlib_suppress():
        await dead_worker
    # Actual non-empty pending rows, not ``qsize() >= 0``. Six rows were
    # scheduled; four had been pulled and two are still queued.
    started = {call.kwargs["request_id"] for call in store.log_request.await_args_list}
    pending_before = [f"req-{i}" for i in range(6) if f"req-{i}" not in started]
    assert pending_before
    assert cl._queue.qsize() == len(pending_before)

    # The dead worker must be detected on the next admission, pending rows
    # preserved, and a new consumer started over the same queue.
    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        cl.schedule_log("req-6", {"request_id": "req-6"})
        hang.set()
        cl.schedule_log("req-7", {"request_id": "req-7"})
        await cl.stop()

    assert (
        "Request-log worker terminated unexpectedly" in caplog.text
        or "Request-log workers died" in caplog.text
    )
    assert cl._queue is not None and cl._queue.qsize() == 0  # restarted consumer drained it
    ids = [call.kwargs["request_id"] for call in store.log_request.await_args_list]
    assert set(pending_before) <= set(ids)
    assert "req-6" in ids and "req-7" in ids
    # The replacement joined the three live workers and all four ended with
    # stop(); nothing is left running.
    assert cl._workers is None


@pytest.mark.asyncio
async def test_stop_recovers_dead_workers_without_a_later_producer(caplog):
    """stop() itself must replace a dead pool, not wait out the drain timeout.

    No ``schedule_log`` follows the worker deaths here: the only trigger left
    is shutdown. The queued rows are the only copy of those reports, so the
    drain must repair the consumer before joining the queue.
    """
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    total = 10
    for i in range(total):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, _LOG_WORKERS)

    assert cl._workers and cl._queue is not None
    started = {call.kwargs["request_id"] for call in store.log_request.await_args_list}
    pending = [f"req-{i}" for i in range(total) if f"req-{i}" not in started]
    assert pending
    assert cl._queue.qsize() == len(pending)

    for worker in cl._workers:
        worker.cancel()
        with contextlib_suppress():
            await worker
    # Let the repaired pool write the rows that were already queued.
    hang.set()

    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        await cl.stop(timeout=1.0)

    written = {call.kwargs["request_id"] for call in store.log_request.await_args_list}
    assert set(pending) <= written
    assert "timed out" not in caplog.text
    assert "terminated before drain" in caplog.text
    assert cl._workers is None
    assert cl._queue is not None and cl._queue.qsize() == 0


@pytest.mark.asyncio
async def test_worker_death_on_dead_loop_preserves_pending_and_refuses(caplog):
    """A worker that died on a loop other than the caller's cannot be
    restarted safely here; pending rows are kept (not wiped into an empty
    queue) and the admission is refused loudly."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)

    for i in range(6):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await _wait_for_writes(store, _LOG_WORKERS)
    dead_loop_original = cl._queue_loop
    assert dead_loop_original is not None and dead_loop_original is asyncio.get_running_loop()
    assert cl._queue is not None
    queue_before = cl._queue
    started = {call.kwargs["request_id"] for call in store.log_request.await_args_list}
    pending_before = [f"req-{i}" for i in range(6) if f"req-{i}" not in started]
    assert pending_before
    assert cl._queue.qsize() == len(pending_before)

    assert cl._workers
    for worker in cl._workers:
        worker.cancel()
        with contextlib_suppress():
            await worker
    # Simulate the queue belonging to a now-dead loop.
    dead_loop = asyncio.new_event_loop()
    cl._queue_loop = dead_loop
    try:
        with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
            cl.schedule_log("req-6", {"request_id": "req-6"})

        assert "refusing payload" in caplog.text
        # Pending rows preserved — the queue was NOT replaced with an empty one.
        assert cl._queue is queue_before
        assert cl._queue.qsize() == len(pending_before)
        assert cl._log_store.log_request.await_count == _LOG_WORKERS
    finally:
        hang.set()
        dead_loop.close()


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


@pytest.mark.asyncio
async def test_stop_then_restart_is_not_possible_but_refusal_is_loud(caplog):
    """stop() is terminal: a restart must never produce two concurrent
    consumers of one queue. Post-stop admission is refused with the
    lifecycle named."""
    store = _store()
    cl = CompletionsLogger(log_store=store)
    cl.schedule_log("req-1", {"request_id": "req-1"})
    await cl.stop()

    assert cl._lifecycle.value == "stopped"
    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        cl.schedule_log("req-2", {"request_id": "req-2"})

    assert "Request-log admission refused" in caplog.text
    assert cl._log_store.log_request.await_count == 1


@pytest.mark.asyncio
async def test_fixed_pool_drains_concurrent_rows_faster_than_one_writer():
    """Multiple database writes overlap; one row no longer gates the queue."""
    store = MagicMock()
    active = 0
    max_active = 0

    async def _write(**kwargs: object) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1

    store.log_request = AsyncMock(side_effect=_write)
    cl = CompletionsLogger(log_store=store)
    count = 12
    started = asyncio.get_running_loop().time()
    for i in range(count):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
    await cl.stop()
    elapsed = asyncio.get_running_loop().time() - started

    assert store.log_request.await_count == count
    assert max_active > 1
    # A single writer needs 12 * 20ms = 240ms. Four workers should finish in
    # about 60ms; the bound leaves room for scheduler noise without accepting
    # the original serialization.
    assert elapsed < 0.15, elapsed


@pytest.mark.asyncio
async def test_pool_size_never_grows_with_queue_pressure():
    """Worker count is fixed by policy even when the queue is full."""
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    for i in range(_LOG_QUEUE_MAXSIZE * 2):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})
        if i % 64 == 0:
            await asyncio.sleep(0)

    assert len(cl._background_tasks) == _LOG_WORKERS
    assert cl._queue.qsize() <= _LOG_QUEUE_MAXSIZE
    hang.set()
    await cl.stop()


@pytest.mark.asyncio
async def test_messages_surface_uses_bounded_queue_and_shutdown_drain(monkeypatch):
    """`/v1/messages` no longer creates its own unbounded logging task.

    The helper still accepts a store for compatibility with direct callers,
    but when the route supplies the shared CompletionsLogger its payload must
    go through the same bounded queue and shutdown drain as chat and
    embeddings. The quota cost increment stays outside this assertion.
    """
    from serving.servers.routers import anthropic_messages as amod

    store = _store()
    cl = CompletionsLogger(log_store=store)
    created: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _capture_create_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(amod.asyncio, "create_task", _capture_create_task)
    amod._schedule_log_store_task(
        store,
        request_id="amsg-1",
        model_id="claude",
        provider="anthropic",
        usage={},
        latency_ms=1,
        status_code=200,
        pricing={},
        metadata={},
        params={},
        op_store=None,
        completions_logger=cl,
    )
    await cl.stop()

    # ``usage={}`` reports no cache usage, so the cache keys are omitted (stored
    # NULL, unknown) rather than logged as a fabricated 0 (a measured miss).
    store.log_request.assert_awaited_once_with(
        request_id="amsg-1",
        model_id="claude",
        provider="anthropic",
        prompt=[],
        response=None,
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        latency_ms=1,
        status_code=200,
        params={},
        metadata={},
        pricing={},
        ttft_ms=None,
        error=None,
        request_payload=None,
    )
    # Every task was a fixed-pool request-log worker; the Messages helper's
    # legacy one-off task branch created none.
    assert 1 <= len(created) <= _LOG_WORKERS
    assert all(task.get_name().startswith("request_log_worker") for task in created)


@pytest.mark.asyncio
async def test_messages_stream_log_survives_shutdown_before_store_cleanup():
    """Rows queued by the Messages stream are drained by app shutdown order."""
    from serving.servers.routers import anthropic_messages as amod

    store = _store()
    cl = CompletionsLogger(log_store=store)
    amod._schedule_log_store_task(
        store,
        request_id="amsg-stream",
        model_id="claude",
        provider="anthropic",
        usage={"input_tokens": 3, "output_tokens": 4},
        latency_ms=9,
        status_code=200,
        pricing={},
        metadata={},
        params={},
        completions_logger=cl,
    )
    assert store.log_request.await_count == 0
    await cl.stop()
    assert store.log_request.await_count == 1
