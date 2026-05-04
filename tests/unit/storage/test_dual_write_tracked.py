"""Verify dual_write shadow writes are wrapped in tracked_task."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    import pytest


def _mock_op_store():
    """Build a bare async mock OperationalStore that supports the methods we exercise."""
    from unittest.mock import MagicMock

    store = MagicMock()
    store.update_user_last_login = AsyncMock(return_value=None)
    return store


async def test_dual_write_shadow_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful shadow write emits a dual_write_shadow success event."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.storage.dual_write import DualWriteOperationalStore

    _TRACKED_TASKS.clear()
    primary = _mock_op_store()
    shadow = _mock_op_store()
    store = DualWriteOperationalStore(primary, shadow)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        await store.update_user_last_login("user-1")
        # Drain the fire-and-forget shadow task.
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "dual_write_shadow"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    primary.update_user_last_login.assert_awaited_once_with("user-1")
    shadow.update_user_last_login.assert_awaited_once_with("user-1")


async def test_dual_write_shadow_failure_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing shadow write emits a failure event and does not propagate."""
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.storage.dual_write import DualWriteOperationalStore

    _TRACKED_TASKS.clear()
    primary = _mock_op_store()
    shadow = _mock_op_store()
    shadow.update_user_last_login = AsyncMock(side_effect=RuntimeError("shadow boom"))
    store = DualWriteOperationalStore(primary, shadow)

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        # Must not raise even though shadow fails.
        await store.update_user_last_login("user-2")
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "dual_write_shadow"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"
    # Shadow health flips to False after the runner observes the failure.
    assert store.shadow_healthy is False
