"""Verify completions.py call-sites use tracked_task with the right names."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    import pytest


async def test_schedule_db_log_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DB-log call-site emits a request_log success event."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_db_log_task

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(return_value=None)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        _schedule_db_log_task(log_store, "req-1", {"foo": "bar"})
        # Drain pending tasks.
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "request_log"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    log_store.log_request.assert_awaited_once_with(foo="bar")


async def test_schedule_db_log_failure_emits_failure_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the underlying log_request raises, a failure event is emitted."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_db_log_task

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(side_effect=RuntimeError("db down"))

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        _schedule_db_log_task(log_store, "req-2", {})
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "request_log"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"


async def test_schedule_cost_increment_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cost-increment call-site emits a cost_increment success event."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_cost_increment

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock(return_value=None)

    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    pricing = {"prompt": "1.0", "completion": "2.0"}  # nonzero so cost > 0

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        _schedule_cost_increment(op_store, "user-1", usage, pricing)
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "cost_increment"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    op_store.increment_user_cost.assert_awaited_once()


async def test_schedule_cost_increment_skipped_when_zero_cost() -> None:
    """Zero-cost requests do not schedule any background work."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_cost_increment

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()

    # Zero pricing -> cost == 0 -> no task scheduled.
    _schedule_cost_increment(
        op_store,
        "user-2",
        {"prompt_tokens": 1},
        {"prompt": "0", "completion": "0"},
    )
    assert len(_TRACKED_TASKS) == 0
    op_store.increment_user_cost.assert_not_awaited()


async def test_schedule_cost_increment_failure_emits_failure_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When increment_user_cost raises, a failure event is emitted."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_cost_increment

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock(side_effect=RuntimeError("billing down"))

    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    pricing = {"prompt": "1.0", "completion": "2.0"}

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        _schedule_cost_increment(op_store, "user-3", usage, pricing)
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "cost_increment"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"
