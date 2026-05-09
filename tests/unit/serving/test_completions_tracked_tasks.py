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
    from serving.servers.routers.completions_logging import CompletionsLogger

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(return_value=None)

    cl = CompletionsLogger(log_store=log_store)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        cl.schedule_log("req-1", {"foo": "bar"})
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
    from serving.servers.routers.completions_logging import CompletionsLogger

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(side_effect=RuntimeError("db down"))

    cl = CompletionsLogger(log_store=log_store)

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        cl.schedule_log("req-2", {})
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


def _make_cost_tracker(op_store):
    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.routing_info import Pricing

    pricing_lookup = MagicMock(spec=PricingLookup)
    pricing_lookup.for_routing.return_value = Pricing(prompt_price=1.0, completion_price=2.0)
    return CostTracker(op_store=op_store, pricing=pricing_lookup)


def _routing_for_cost(provider: str = "openai"):
    from serving.servers.routers.routing_info import RoutingInfo

    return RoutingInfo(request_id="req-x", model="m", provider=provider)


async def test_schedule_cost_increment_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cost-increment call-site emits a cost_increment success event."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock(return_value=None)
    tracker = _make_cost_tracker(op_store)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        await tracker.schedule_increment(
            user_id="user-1",
            routing=_routing_for_cost(),
            prompt_tokens=100,
            completion_tokens=50,
        )
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
    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.routing_info import Pricing

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()

    pricing_lookup = MagicMock(spec=PricingLookup)
    pricing_lookup.for_routing.return_value = Pricing(prompt_price=0.0, completion_price=0.0)
    tracker = CostTracker(op_store=op_store, pricing=pricing_lookup)

    await tracker.schedule_increment(
        user_id="user-2",
        routing=_routing_for_cost(),
        prompt_tokens=1,
        completion_tokens=0,
    )
    assert len(_TRACKED_TASKS) == 0
    op_store.increment_user_cost.assert_not_awaited()


async def test_schedule_cost_increment_failure_emits_failure_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When increment_user_cost raises, a failure event is emitted."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock(side_effect=RuntimeError("billing down"))
    tracker = _make_cost_tracker(op_store)

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        await tracker.schedule_increment(
            user_id="user-3",
            routing=_routing_for_cost(),
            prompt_tokens=100,
            completion_tokens=50,
        )
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
