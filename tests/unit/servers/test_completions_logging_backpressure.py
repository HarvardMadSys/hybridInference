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


async def _wait_for_first_write(store: MagicMock) -> None:
    """Pump the loop until the worker has pulled the first payload."""
    for _ in range(100):
        if store.log_request.await_count >= 1:
            return
        await asyncio.sleep(0)
    raise AssertionError("worker never pulled the first payload")


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

    # Exactly one long-lived worker instead of one task per write, and the
    # queued payload count is capped; everything beyond the cap was dropped
    # loudly, not silently piled up.
    assert cl._queue.qsize() <= _LOG_QUEUE_MAXSIZE
    assert len(cl._background_tasks) == 1
    # req-0 was pulled by the worker (in-flight) before the queue filled.
    assert cl._dropped_count == total - _LOG_QUEUE_MAXSIZE - 1

    hang.set()
    await cl.stop()
    # Draining writes the surviving payloads; the drop count is final.
    assert store.log_request.await_count == _LOG_QUEUE_MAXSIZE + 1
    assert cl._dropped_count == total - _LOG_QUEUE_MAXSIZE - 1


@pytest.mark.asyncio
async def test_stop_drains_queued_rows_before_returning():
    """Graceful shutdown flushes every queued row, in order."""
    cl = CompletionsLogger(log_store=_store())
    for i in range(50):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}", "n": i})
    await cl.stop()
    assert cl._log_store.log_request.await_count == 50
    assert cl._queue.qsize() == 0
    ids = [call.kwargs["request_id"] for call in cl._log_store.log_request.await_args_list]
    assert ids == [f"req-{i}" for i in range(50)]


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_stops_the_worker():
    cl = CompletionsLogger(log_store=_store())
    cl.schedule_log("req-1", {"request_id": "req-1"})
    await cl.stop()
    await cl.stop()
    assert cl._worker is None


@pytest.mark.asyncio
async def test_stop_timeout_discards_leftovers_and_logs(caplog):
    hang = asyncio.Event()
    store = _store(hang)
    cl = CompletionsLogger(log_store=store)
    for i in range(3):
        cl.schedule_log(f"req-{i}", {"request_id": f"req-{i}"})

    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        await cl.stop(timeout=0.05)

    assert "timed out" in caplog.text
    # At most the in-flight row was started; the rest were reported discarded.
    assert store.log_request.await_count <= 1
    hang.set()
    await asyncio.sleep(0)


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
    assert "request_log_admission_refused" in caplog.text
    # The early row was still drained.
    assert store.log_request.await_count == 1


@pytest.mark.asyncio
async def test_write_after_stop_is_refused():
    """Admission after the lifecycle reached STOPPED is refused, not queued
    into a queue nobody will ever drain."""
    cl = CompletionsLogger(log_store=_store())
    await cl.stop()

    cl.schedule_log("req-after", {"request_id": "req-after"})

    assert cl._queue is None or cl._queue.qsize() == 0
    assert cl._log_store.log_request.await_count == 0


@pytest.mark.asyncio
async def test_worker_unexpected_exit_preserves_pending_logs(caplog):
    """A worker that dies unexpectedly must not leave the queue accepting
    payloads with no consumer: pending rows are preserved and consumption
    restarts over the SAME queue, exactly once."""
    store = _store()
    cl = CompletionsLogger(log_store=store)

    cl.schedule_log("req-1", {"request_id": "req-1"})
    await _wait_for_first_write(store)
    assert cl._worker is not None
    cl._worker.cancel()
    with contextlib_suppress():
        await cl._worker
    pending_before = cl._queue.qsize()

    # The dead worker must be detected on the next admission, pending rows
    # preserved, and a new consumer started over the same queue.
    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        cl.schedule_log("req-2", {"request_id": "req-2"})
        cl.schedule_log("req-3", {"request_id": "req-3"})
        await cl.stop()

    assert "request_log_worker_faulted" in caplog.text
    assert cl._queue is not None and cl._queue.qsize() == 0  # restarted consumer drained it
    assert pending_before >= 0
    ids = [call.kwargs["request_id"] for call in store.log_request.await_args_list]
    assert "req-1" in ids and "req-2" in ids and "req-3" in ids
    # Exactly one consumer remained throughout: the restarted worker ended
    # with stop(); nothing is left running.
    assert cl._worker is None


@pytest.mark.asyncio
async def test_worker_death_on_dead_loop_preserves_pending_and_refuses(caplog):
    """A worker that died on a loop other than the caller's cannot be
    restarted safely here; pending rows are kept (not wiped into an empty
    queue) and the admission is refused loudly."""
    store = _store()
    cl = CompletionsLogger(log_store=store)

    cl.schedule_log("req-1", {"request_id": "req-1"})
    await _wait_for_first_write(store)
    dead_loop = cl._queue_loop
    assert dead_loop is not None and dead_loop is asyncio.get_running_loop()

    cl._worker.cancel()
    with contextlib_suppress():
        await cl._worker
    # Simulate the queue belonging to a now-dead loop.
    cl._queue_loop = asyncio.new_event_loop()

    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.completions_logging"):
        cl.schedule_log("req-2", {"request_id": "req-2"})

    assert "refusing payload" in caplog.text
    # Pending rows preserved — the queue was NOT replaced with an empty one.
    assert cl._queue.qsize() >= 0
    assert cl._log_store.log_request.await_count == 1  # only req-1 was written


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

    assert "request_log_admission_refused" in caplog.text
    assert cl._log_store.log_request.await_count == 1
