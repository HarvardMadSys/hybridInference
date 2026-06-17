"""Periodic Slack alerter for spikes in service-side request failures.

Counts rows in ``api_logs`` where ``status_code >= 500`` or ``error IS NOT NULL``
over a sliding window and posts to a Slack incoming webhook when the count
exceeds a configured threshold. A cooldown prevents duplicate alerts during
sustained incidents. Empty ``SLACK_WEBHOOK_URL`` disables the feature entirely.

Slack delivery now goes through :func:`serving.observability.alerts.alert_slack`,
the unified sink for the new alerting framework. The legacy
:func:`post_slack_alert` helper is kept as a thin wrapper for backward
compatibility (existing call-sites and tests) and delegates to the framework's
``_post_to_slack``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx  # retained for backward-compat import symbol used by tests

from serving.observability.alerts import AlertSeverity, alert_slack
from serving.utils.email_scheduler import get_scheduler
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg

    from serving.config.settings import Settings

logger = get_logger(__name__)


# Predicate shared by the count and breakdown queries.
#
# Client errors (4xx) intentionally excluded — alert is for service-side
# failures. The ``error IS NOT NULL`` branch would otherwise re-admit 4xx rows
# that log an error message. Gateway-generated "model not found" responses (404)
# are user-driven — a request for an unknown or unauthorized model — not an
# incident, so they are excluded here and never reach Slack. Three exact forms
# are persisted:
#   - ``Model '<id>' not found``           (completions handler synthetic log)
#   - ``Embedding model '<id>' not found`` (embeddings handler)
#   - ``model_not_found``                  (rejection-log code for
#                                           /anthropic/v1/messages)
# The match is anchored (no leading ``%``) so it only drops these gateway rows.
# An *upstream* provider 404 is logged via format_exception_for_db as the full
# exception text (e.g. ``404, message='Not Found', url=...``); that is a genuine
# service-side failure and must still alert, so a broad ``%not found%`` would be
# wrong — it would silence provider/config regressions.
#
# Per-user quota/concurrency rejections (``quota_exceeded`` /
# ``concurrency_limit_exceeded``, persisted by rejection_log.log_rejection as
# 429s with a non-null ``error``) are expected user-facing rate limiting, not a
# service fault, so they are excluded here and never page Slack. The exclusions
# live on the error branch, so a genuine 5xx still counts via ``status_code >= 500``.
FAILURE_PREDICATE_SQL = (
    "(status_code >= 500 OR (error IS NOT NULL "
    "AND error NOT ILIKE 'Model ''%'' not found' "
    "AND error NOT ILIKE 'Embedding model ''%'' not found' "
    "AND error <> 'model_not_found' "
    "AND error <> 'quota_exceeded' "
    "AND error <> 'concurrency_limit_exceeded'))"
)

# Module-level SQL so tests can introspect the predicate text.
# Parameterized: $1 = window_minutes (int). Avoids string interpolation, allows plan caching.
FAILED_REQUEST_COUNT_SQL = (
    "SELECT COUNT(*) FROM api_logs "
    "WHERE timestamp > NOW() - make_interval(mins => $1) "
    f"AND {FAILURE_PREDICATE_SQL}"
)

# Returns top status codes, providers, models, and a sample error message for
# the same failure window. $1 = window_minutes (int). Shares FAILURE_PREDICATE_SQL
# with the count query so the two never drift.
FAILED_REQUEST_BREAKDOWN_SQL = f"""
SELECT
    string_agg(DISTINCT status_code::text, ', ' ORDER BY status_code::text) FILTER (WHERE status_code IS NOT NULL) AS status_codes,
    string_agg(DISTINCT provider, ', ' ORDER BY provider) FILTER (WHERE provider IS NOT NULL) AS providers,
    string_agg(DISTINCT model_id, ', ' ORDER BY model_id) FILTER (WHERE model_id IS NOT NULL) AS models,
    (array_agg(error ORDER BY timestamp DESC) FILTER (WHERE error IS NOT NULL))[1] AS sample_error
FROM api_logs
WHERE timestamp > NOW() - make_interval(mins => $1)
AND {FAILURE_PREDICATE_SQL}
"""


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


async def fetch_failure_breakdown(pool: asyncpg.Pool, window_minutes: int) -> dict:
    """Return a breakdown dict with status codes, providers, models, and a sample error.

    Returns an empty dict on any query error so callers can degrade gracefully.
    """
    try:
        row = await pool.fetchrow(FAILED_REQUEST_BREAKDOWN_SQL, window_minutes)
        if row is None:
            return {}
        result = {}
        if row["status_codes"]:
            result["status_codes"] = row["status_codes"]
        if row["providers"]:
            result["providers"] = row["providers"]
        if row["models"]:
            result["models"] = row["models"]
        if row["sample_error"]:
            result["sample_error"] = row["sample_error"][:200]
        return result
    except Exception:
        logger.exception("failed_request_alerter: breakdown query failed")
        return {}


async def post_slack_alert(webhook_url: str, message: str) -> bool:
    """POST ``{"text": message}`` to a Slack incoming webhook.

    This wrapper performs its own ``httpx`` POST so that existing callers and
    test patch-points continue to work.  Network and HTTP errors are caught
    and logged; this function never raises.

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

        breakdown = await fetch_failure_breakdown(self.pool, self.window_minutes)

        # Route through the unified alert sink. We pass cooldown_sec=0 because
        # this class enforces its own cooldown above (datetime-based, with a
        # configurable now_fn for deterministic tests). Using a zero cooldown
        # in the sink avoids surprising interactions between the two cooldown
        # tables for the same dedupe key.
        context: dict = {
            "count": count,
            "window_minutes": self.window_minutes,
            "threshold": self.threshold,
        }
        context.update(breakdown)
        ok = await alert_slack(
            AlertSeverity.ERROR,
            "Failed-request rate exceeded (DB-query detector)",
            context,
            dedupe_key="failed_request_rate_db",
            cooldown_sec=0,
        )
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
