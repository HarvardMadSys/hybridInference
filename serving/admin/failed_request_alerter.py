"""Periodic Slack alerter for spikes in service-side request failures.

Counts rows in ``api_logs`` where ``status_code >= 500`` or ``error IS NOT NULL``
over a sliding window and posts to a Slack incoming webhook when the count
exceeds a configured threshold. A cooldown prevents duplicate alerts during
sustained incidents. Empty ``SLACK_WEBHOOK_URL`` disables the feature entirely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from serving.utils.email_scheduler import get_scheduler
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg

    from serving.config.settings import Settings

logger = get_logger(__name__)


# Module-level SQL so tests can introspect the predicate text.
# Client errors (4xx) intentionally excluded — alert is for service-side failures.
# Parameterized: $1 = window_minutes (int). Avoids string interpolation, allows plan caching.
FAILED_REQUEST_COUNT_SQL = (
    "SELECT COUNT(*) FROM api_logs "
    "WHERE timestamp > NOW() - make_interval(mins => $1) "
    "AND (status_code >= 500 OR error IS NOT NULL)"
)


async def count_recent_failures(pool: asyncpg.Pool, window_minutes: int) -> int:
    """Return the count of failed requests in the past ``window_minutes`` minutes.

    A failed request is one with ``status_code >= 500`` or a non-null ``error``.

    Args:
        pool: The asyncpg pool to query.
        window_minutes: Sliding-window size in minutes (must be > 0).

    Returns:
        The integer count of failed rows, or 0 when the query yields ``None``.

    Raises:
        ValueError: If ``window_minutes`` is not a positive integer.
    """
    if window_minutes <= 0:
        raise ValueError(f"window_minutes must be > 0, got {window_minutes}")
    result = await pool.fetchval(FAILED_REQUEST_COUNT_SQL, window_minutes)
    return int(result or 0)


async def post_slack_alert(webhook_url: str, message: str) -> bool:
    """POST ``{"text": message}`` to a Slack incoming webhook.

    Network and HTTP errors are caught and logged; this function never raises.
    The boolean return lets the caller gate state updates (e.g. cooldown
    timestamp) on a successful post.

    Args:
        webhook_url: Slack incoming-webhook URL.
        message: Plain-text message body.

    Returns:
        True when Slack returned a 2xx response, False on any error.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": message})
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "Slack webhook returned non-2xx: status=%d body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception:
        logger.exception("Slack webhook POST failed")
        return False


def _utcnow() -> datetime:
    """Default time source — timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


class FailedRequestAlerter:
    """Stateful alerter that fires on failure-count spikes with cooldown.

    State is held in-memory per-replica. In a multi-replica deployment each
    replica maintains its own cooldown window, which can produce up to N
    Slack messages per cooldown interval; this is acceptable for a low-noise
    incident signal and matches the "no new dependencies" constraint.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        webhook_url: str,
        threshold: int,
        window_minutes: int,
        cooldown_minutes: int,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Initialize the alerter with a DB pool and configuration.

        Args:
            pool: asyncpg pool used to query ``api_logs``.
            webhook_url: Slack incoming-webhook URL.
            threshold: Strict greater-than threshold; ``count > threshold`` fires.
            window_minutes: Sliding-window size for the failure count.
            cooldown_minutes: Minimum interval between alerts.
            now_fn: Injectable clock for deterministic testing.
        """
        self.pool = pool
        self.webhook_url = webhook_url
        self.threshold = threshold
        self.window_minutes = window_minutes
        self.cooldown_minutes = cooldown_minutes
        self._now_fn = now_fn
        self._last_alert_at: datetime | None = None

    def _format_message(self, count: int) -> str:
        """Build the Slack message body for a given failure ``count``."""
        return (
            f":rotating_light: {count} failed requests in past "
            f"{self.window_minutes} minutes (threshold: {self.threshold})."
        )

    async def run_check(self) -> None:
        """Run one check cycle: count failures, alert if over threshold and cooled down."""
        try:
            count = await count_recent_failures(self.pool, self.window_minutes)
        except Exception:
            logger.exception("failed_request_alerter: failure-count query failed")
            return

        if count <= self.threshold:
            return

        now = self._now_fn()
        if self._last_alert_at is not None and now - self._last_alert_at < timedelta(
            minutes=self.cooldown_minutes
        ):
            return

        message = self._format_message(count)
        ok = await post_slack_alert(self.webhook_url, message)
        if ok:
            self._last_alert_at = now
            logger.info(
                "failed_request_alerter: posted Slack alert (count=%d threshold=%d)",
                count,
                self.threshold,
            )


def register_alerter_job(pool: asyncpg.Pool, settings: Settings) -> None:
    """Register the failure-rate alerter on the running APScheduler.

    Empty ``settings.slack_webhook_url`` disables the feature: no job is
    registered and no error is raised. When the scheduler has not been
    started (rare; e.g. DB init failed earlier), the function logs a
    warning and returns without crashing.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    webhook_url = settings.slack_webhook_url.strip()
    if not webhook_url:
        logger.info("Slack alerter disabled (SLACK_WEBHOOK_URL not set)")
        return

    scheduler = get_scheduler()
    if scheduler is None:
        logger.warning("failed_request_alerter: scheduler not running, skipping registration")
        return

    alerter = FailedRequestAlerter(
        pool=pool,
        webhook_url=webhook_url,
        threshold=settings.failed_request_alert_threshold,
        window_minutes=settings.failed_request_alert_window_minutes,
        cooldown_minutes=settings.failed_request_alert_cooldown_minutes,
    )

    scheduler.add_job(
        alerter.run_check,
        trigger=IntervalTrigger(minutes=1),
        id="failed_request_alerter",
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "failed_request_alerter: registered (threshold=%d window_min=%d cooldown_min=%d)",
        settings.failed_request_alert_threshold,
        settings.failed_request_alert_window_minutes,
        settings.failed_request_alert_cooldown_minutes,
    )
