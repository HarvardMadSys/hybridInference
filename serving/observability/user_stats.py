"""User statistics collector for Prometheus metrics.

This module provides a background task that periodically queries the database
to update user-related Prometheus metrics (total users, DAU, MAU).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.storage.database import DatabaseLogger

logger = get_logger(__name__)


class UserStatsCollector:
    """Background task that periodically updates user statistics metrics."""

    def __init__(
        self,
        db_logger: DatabaseLogger | None,
        interval_seconds: int = 60,
    ) -> None:
        """Initialize the user stats collector.

        Args:
            db_logger: Database logger instance for querying user data.
            interval_seconds: How often to update metrics (default: 60s).
        """
        self.db_logger = db_logger
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._enabled = os.getenv("METRICS_ENABLED", "1") == "1"

    async def _update_metrics(self) -> None:
        """Query database and update Prometheus metrics."""
        if not self.db_logger or not self.db_logger.pool:
            logger.debug("Database not available, skipping user stats update")
            return

        try:
            from serving.observability.metrics import (
                USERS_ACTIVE_DAILY,
                USERS_ACTIVE_MONTHLY,
                USERS_TOTAL,
            )

            async with self.db_logger.pool.acquire() as conn:
                # Total registered users
                total_users_row = await conn.fetchrow(
                    "SELECT COUNT(*) as count FROM users WHERE status = 'active'"
                )
                total_users = total_users_row["count"] if total_users_row else 0

                # Daily active users (users who logged in within last 24 hours)
                dau_row = await conn.fetchrow(
                    """
                    SELECT COUNT(DISTINCT id) as count
                    FROM users
                    WHERE status = 'active'
                      AND last_login_at >= NOW() - INTERVAL '24 hours'
                    """
                )
                dau = dau_row["count"] if dau_row else 0

                # Monthly active users (users who logged in within last 30 days)
                mau_row = await conn.fetchrow(
                    """
                    SELECT COUNT(DISTINCT id) as count
                    FROM users
                    WHERE status = 'active'
                      AND last_login_at >= NOW() - INTERVAL '30 days'
                    """
                )
                mau = mau_row["count"] if mau_row else 0

            # Update Prometheus metrics
            USERS_TOTAL.set(total_users)
            USERS_ACTIVE_DAILY.set(dau)
            USERS_ACTIVE_MONTHLY.set(mau)

            logger.debug(f"Updated user stats: total={total_users}, DAU={dau}, MAU={mau}")

        except Exception as exc:
            logger.warning(f"Failed to update user statistics: {exc}")

    async def _run(self) -> None:
        """Background loop that periodically updates metrics."""
        while True:
            await self._update_metrics()
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        """Start the background task."""
        if not self._enabled:
            logger.info("Metrics disabled, skipping user stats collector")
            return

        if self._task is not None:
            logger.warning("User stats collector already running")
            return

        if not self.db_logger:
            logger.warning("Database not available, user stats collector disabled")
            return

        logger.info(f"Starting user stats collector (interval: {self.interval_seconds}s)")
        self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        """Stop the background task."""
        if self._task:
            logger.info("Stopping user stats collector")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
