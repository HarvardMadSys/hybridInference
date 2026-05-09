"""Unit tests for serving.observability.tracked_tasks."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from serving.observability.tracked_tasks import _TRACKED_TASKS, tracked_task


@pytest.fixture(autouse=True)
def _clear_tracked_tasks():
    """Make sure module-level set starts empty for each test."""
    _TRACKED_TASKS.clear()
    yield
    _TRACKED_TASKS.clear()


async def test_tracked_task_emits_success_event(caplog: pytest.LogCaptureFixture) -> None:
    """Successful coroutine emits a tracked_task_completed event with success=True."""

    async def _ok() -> None:
        await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_ok(), name="unit_test_ok")
        await task

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    record = matching[0]
    assert record.task_name == "unit_test_ok"
    assert record.success is True
    assert isinstance(record.duration_ms, int)
    assert record.duration_ms >= 0
    assert record.levelno == logging.INFO


async def test_tracked_task_emits_failure_event(caplog: pytest.LogCaptureFixture) -> None:
    """Failing coroutine emits a tracked_task_completed event with success=False."""

    async def _fail() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_fail(), name="unit_test_fail")
        await task  # must not raise

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    record = matching[0]
    assert record.task_name == "unit_test_fail"
    assert record.success is False
    assert record.error_type == "RuntimeError"
    assert "boom" in record.error
    assert record.levelno == logging.WARNING


async def test_tracked_task_no_exception_escapes() -> None:
    """Awaiting the wrapper never raises even if the inner coro fails."""

    async def _fail() -> None:
        raise ValueError("nope")

    task = tracked_task(_fail(), name="no_escape")
    # Awaiting must not raise — the wrapper swallows.
    await task
    assert task.exception() is None


async def test_tracked_task_gc_safety(caplog: pytest.LogCaptureFixture) -> None:
    """Dropping the external task reference must not cause the task to be cancelled."""
    completed = asyncio.Event()

    async def _slow() -> None:
        await asyncio.sleep(0.05)
        completed.set()

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        # Schedule and immediately discard the returned task.
        tracked_task(_slow(), name="gc_safety")
        # Force GC to prove the module-level set keeps it alive.
        gc.collect()
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        # Yield once more so the done callback fires and the set is cleaned.
        await asyncio.sleep(0)

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    assert matching[0].success is True
    assert len(_TRACKED_TASKS) == 0


async def test_tracked_task_double_wrap_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """Wrapping a coroutine that itself was scheduled via tracked_task adds a second event but is otherwise harmless."""

    async def _inner() -> None:
        await asyncio.sleep(0)

    async def _outer() -> None:
        # Inner-as-coro is awaited inside the outer coroutine.
        await _inner()

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_outer(), name="double_wrap")
        await task

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    # Exactly one outer event; the inner coroutine wasn't tracked because we only wrapped once.
    assert len(matching) == 1
    assert matching[0].task_name == "double_wrap"
    assert matching[0].success is True
