"""Periodic alerter for spikes in service-side request failures.

Counts rows in ``api_logs`` where ``status_code >= 500`` or ``error IS NOT NULL``
over a sliding window and raises an alert when the count exceeds a configured
threshold. A cooldown prevents duplicate alerts during sustained incidents.
With both ``SLACK_WEBHOOK_URL`` and ``WECOM_ALERTS_WEBHOOK_URL`` empty, the
detector is paused: it queries nothing, changes no incident and sends nothing.

The webhooks and the five parameters are the ``alerts.runtime`` configuration
group (:mod:`serving.observability.alert_runtime`). One job is registered
whenever the scheduler and the database are available, and it stays registered
when the configuration changes. Each check takes one configuration for its
whole run and checks it is still in force after every query and before it
changes any state, so a check overtaken by a configuration change is dropped
without effect; the cooldown and the open incident carry over to the new
configuration.

Delivery goes through :func:`serving.observability.alerts.alert_slack`, which
sends to the one configured channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from serving.observability import alert_runtime
from serving.observability.alert_history import hooks as alert_history
from serving.observability.alerts import AlertSeverity, alert_on_transition
from serving.utils.email_scheduler import get_scheduler
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg

    from serving.config.settings import Settings
    from serving.observability.alert_runtime import AlertRuntimeConfig

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
# Rate-limit rejections (status 429) are expected user-facing throttling, not a
# service fault, so they are excluded here and never page Slack. The whole status
# is excluded rather than specific error strings: the gateway's own rejections
# (``quota_exceeded`` / ``concurrency_limit_exceeded``, persisted by
# rejection_log.log_rejection) are only two of the sources. Edge/proxy layers in
# front of the gateway also persist 429 rows with their own error codes (e.g.
# ``ip_blocked`` from IP-level rate limiting), and enumerating error strings
# silently re-admits every code nobody thought to list — a burst of edge-blocked
# clients then pages Slack as if it were an outage. Matching on status is exact
# and survives new or reworded rejection codes. This mirrors
# ``_FAILED_REQUEST_IGNORED_STATUSES`` in observability/alert_rules.py, which
# already drops 429 as a class for the metrics-based detector.
#
# Trade-off: a provider-exhaustion 429 (anthropic_messages returns 429 when every
# upstream key is in cooldown) no longer counts here. That is deliberate — it was
# already invisible to the metrics detector for the same reason, and provider
# exhaustion surfaces through the fivexx_rate rule and the circuit_open state
# change instead.
#
# Client disconnects (status 499, error "Client disconnected before the stream
# completed") are excluded for the same reason. When a caller drops a streaming
# connection, completions_stream._finalize_cancelled deliberately logs status 499
# — nginx's "client closed request" convention — precisely so the abort is NOT
# treated as a service fault (it inflates no 5xx metric and records no routing
# failure). But the row carries a non-null ``error``, so without this guard the
# ``error IS NOT NULL`` branch re-admits it and a spike in benign client
# disconnects pages Slack. 499 is assigned nowhere else, so excluding the whole
# status is exact and survives any rewording of the error text. Gateway timeouts
# use 504 (a real service concern) and still count via ``status_code >= 500``.
# ``IS DISTINCT FROM`` (not ``<>``) keeps NULL-status error rows counting.
FAILURE_PREDICATE_SQL = (
    "(status_code >= 500 OR (error IS NOT NULL "
    "AND status_code IS DISTINCT FROM 429 "
    "AND status_code IS DISTINCT FROM 499 "
    "AND error NOT ILIKE 'Model ''%'' not found' "
    "AND error NOT ILIKE 'Embedding model ''%'' not found' "
    "AND error <> 'model_not_found'))"
)

# Module-level SQL so tests can introspect the predicate text.
# Parameterized: $1 = window_minutes (int). Avoids string interpolation, allows plan caching.
FAILED_REQUEST_COUNT_SQL = (
    "SELECT COUNT(*) FROM api_logs "
    "WHERE timestamp > NOW() - make_interval(mins => $1) "
    f"AND {FAILURE_PREDICATE_SQL}"
)

# Failures *and* the window's total, so a sustained low-rate defect is visible.
# An absolute count alone is blind to it: this detector's 200-per-5-minutes
# default is ~57,600/day, and the routing IndexError behind #1361 ran at
# ~700/day -- roughly 1% of all traffic, about 87x below the bar -- for 18 days
# without ever tripping it. $1 = window_minutes (int).
FAILED_REQUEST_RATE_SQL = (
    "SELECT COUNT(*) FILTER (WHERE " + FAILURE_PREDICATE_SQL + ") AS failures, "
    "COUNT(*) AS total FROM api_logs "
    "WHERE timestamp > NOW() - make_interval(mins => $1)"
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


async def count_recent_failures_and_total(
    pool: asyncpg.Pool, window_minutes: int
) -> tuple[int, int]:
    """Return ``(failures, total)`` for the past ``window_minutes`` minutes.

    Args:
        pool: The asyncpg pool to query.
        window_minutes: Sliding-window size in minutes (must be > 0).

    Returns:
        Failing-row count and total row count over the window.

    Raises:
        ValueError: If ``window_minutes`` is not a positive integer.
    """
    if window_minutes <= 0:
        raise ValueError(f"window_minutes must be > 0, got {window_minutes}")
    row = await pool.fetchrow(FAILED_REQUEST_RATE_SQL, window_minutes)
    if row is None:
        return 0, 0
    return int(row["failures"] or 0), int(row["total"] or 0)


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


def _utcnow() -> datetime:
    """Default time source — timezone-aware UTC ``datetime``."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Fixed:
    """A configuration that never changes: for an alerter built with explicit parameters."""

    view: AlertRuntimeConfig
    generation: int = 0


class FailedRequestAlerter:
    """Stateful alerter that fires on failure-count spikes with cooldown.

    State is held in-memory per-replica. In a multi-replica deployment each
    replica maintains its own cooldown window, which can produce up to N
    Slack messages per cooldown interval; this is acceptable for a low-noise
    incident signal and matches the "no new dependencies" constraint.

    The configuration is read at the start of every check: the live
    ``alerts.runtime`` group by default, or fixed parameters when the alerter
    is built with them (tests and one-off callers).
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        threshold: int | None = None,
        window_minutes: int | None = None,
        cooldown_minutes: int | None = None,
        now_fn: Callable[[], datetime] = _utcnow,
        rate_threshold: float = 0.0,
        rate_min_count: int = 0,
    ) -> None:
        """Initialize the alerter with a DB pool and, optionally, fixed parameters.

        Args:
            pool: asyncpg pool used to query ``api_logs``.
            threshold: Strict greater-than threshold; ``count > threshold`` fires.
                Leave the four parameters unset to follow the live
                ``alerts.runtime`` configuration, including its channel switch.
            window_minutes: Sliding-window size for the failure count.
            cooldown_minutes: Minimum interval between alerts.
            now_fn: Injectable clock for deterministic testing.
            rate_threshold: Failing fraction of the window that fires
                independently of ``threshold``. ``0.0`` disables the rate rule
                and restores pure absolute-count behavior.
            rate_min_count: Failures required before the rate rule can fire, so
                a quiet window (1 failure out of 3 requests) does not page.
        """
        self.pool = pool
        self._fixed: _Fixed | None = None
        if threshold is not None:
            from serving.observability.alert_runtime import AlertRuntimeConfig

            # Fixed parameters: the detector runs, as such an alerter always has.
            self._fixed = _Fixed(
                AlertRuntimeConfig(
                    slack_webhook_url="fixed",
                    failed_request_alert_threshold=threshold,
                    failed_request_alert_window_minutes=(
                        window_minutes if window_minutes is not None else 5
                    ),
                    failed_request_alert_cooldown_minutes=cooldown_minutes or 0,
                    failed_request_alert_rate=rate_threshold,
                    failed_request_alert_rate_min_count=rate_min_count,
                )
            )
        self._now_fn = now_fn
        self._last_alert_at: datetime | None = None
        #: Checks dropped because the configuration changed while they ran.
        self.superseded_checks = 0
        #: The configuration of the last check, or ``None`` before the first.
        self.last_state: str | None = None

    def _acquire(self) -> Any:
        if self._fixed is not None:
            return self._fixed
        return alert_runtime.acquire()

    def _current(self, published: Any) -> bool:
        if self._fixed is not None:
            return True
        if alert_runtime.is_current(published):
            return True
        self.superseded_checks += 1
        logger.info(
            "failed_request_alerter: check dropped, the configuration changed while it ran "
            "(generation %d)",
            published.generation,
        )
        return False

    # The parameters of the configuration in force, for callers and tests that
    # read them off the alerter.
    @property
    def threshold(self) -> int:
        """Strict greater-than failure count that fires."""
        return self._acquire().view.failed_request_alert_threshold

    @property
    def window_minutes(self) -> int:
        """Sliding-window size, in minutes."""
        return self._acquire().view.failed_request_alert_window_minutes

    @property
    def cooldown_minutes(self) -> int:
        """Minimum interval between alerts, in minutes."""
        return self._acquire().view.failed_request_alert_cooldown_minutes

    @property
    def rate_threshold(self) -> float:
        """Failing fraction that fires on its own; 0 disables the rule."""
        return self._acquire().view.failed_request_alert_rate

    @property
    def rate_min_count(self) -> int:
        """Failures required before the rate rule applies."""
        return self._acquire().view.failed_request_alert_rate_min_count

    async def run_check(self) -> None:
        """Run one check cycle: count failures, alert if over threshold and cooled down."""
        published = self._acquire()
        config = published.view
        if not config.detector_enabled:
            # Paused: no query, no incident change, no recovery message.
            self.last_state = "paused"
            return
        self.last_state = "running"
        threshold = config.failed_request_alert_threshold
        window_minutes = config.failed_request_alert_window_minutes
        cooldown_minutes = config.failed_request_alert_cooldown_minutes
        rate_threshold = config.failed_request_alert_rate
        rate_min_count = config.failed_request_alert_rate_min_count

        def fence() -> bool:
            return self._current(published)

        total = 0
        try:
            if rate_threshold > 0:
                count, total = await count_recent_failures_and_total(self.pool, window_minutes)
            else:
                count = await count_recent_failures(self.pool, window_minutes)
        except Exception:
            logger.exception("failed_request_alerter: failure-count query failed")
            return
        if not fence():
            return

        # Two independent rules. The absolute count catches a loud spike; the
        # rate catches a sustained defect that never gets near it -- which is
        # the shape that went unalerted for 18 days in #1361.
        rate = (count / total) if total else 0.0
        rate_breached = rate_threshold > 0 and count >= rate_min_count and rate >= rate_threshold

        if count <= threshold and not rate_breached:
            # Healthy. The detector used to stop here, which is why its alert
            # was fire-only; reporting the crossing back down is what closes
            # the incident it opened. The fence is checked again before the
            # tracker changes; the cooldown is cleared only if it passed.
            if not fence():
                return
            await alert_on_transition(
                key="failed_request_rate_db",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded (DB-query detector)",
                context=dict,
                cooldown_sec=0,
                fence=fence,
            )
            # Only an evaluation still current after the transition owns the
            # cooldown: one superseded while it awaited leaves it to the next.
            if fence():
                self._last_alert_at = None
            return

        now = self._now_fn()
        if self._last_alert_at is not None and now - self._last_alert_at < timedelta(
            minutes=cooldown_minutes
        ):
            alert_history.external_decision(
                "failed_request_rate_db",
                reason="cooldown",
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded (DB-query detector)",
                kind="metric",
                context={"count": count, "window_minutes": window_minutes},
            )
            return

        breakdown = await fetch_failure_breakdown(self.pool, window_minutes)
        if not fence():
            return

        # Route through the unified alert sink. We pass cooldown_sec=0 because
        # this class enforces its own cooldown above (datetime-based, with a
        # configurable now_fn for deterministic tests). Using a zero cooldown
        # in the sink avoids surprising interactions between the two cooldown
        # tables for the same dedupe key.
        context: dict = {
            "count": count,
            "window_minutes": window_minutes,
            "threshold": threshold,
            # Which rule fired, so the reader is not left guessing why a count
            # far below `threshold` produced a page.
            "trigger": "rate" if count <= threshold else "count",
        }
        if rate_threshold > 0:
            context["total"] = total
            context["failure_rate"] = round(rate, 4)
            context["rate_threshold"] = rate_threshold
        context.update(breakdown)
        ok = await alert_on_transition(
            key="failed_request_rate_db",
            breached=True,
            severity=AlertSeverity.ERROR,
            title="Failed-request rate exceeded (DB-query detector)",
            context=lambda: context,
            cooldown_sec=0,
            fence=fence,
        )
        if ok:
            # A send admitted under this configuration was delivered: its
            # cooldown stands even if the configuration changed during the post.
            self._last_alert_at = now
            logger.info(
                "failed_request_alerter: posted alert "
                "(count=%d threshold=%d rate=%.4f of %d, configuration generation %d)",
                count,
                threshold,
                rate,
                total,
                published.generation,
            )


def register_alerter_job(
    pool: asyncpg.Pool, settings: Settings | None = None
) -> FailedRequestAlerter | None:
    """Register the failure-rate alerter on the running APScheduler; return it.

    One job, registered whenever the scheduler is running, whatever the
    configuration: with neither ``SLACK_WEBHOOK_URL`` nor
    ``WECOM_ALERTS_WEBHOOK_URL`` set the detector is paused rather than absent,
    so a webhook saved later starts it without a restart once the
    ``alerts.runtime`` group is active. ``SLACK_ALERTS_WEBHOOK_URL`` alone
    still does not enable it. When the scheduler has not been started (rare;
    e.g. DB init failed earlier), the function logs a warning, reports the
    detector unavailable and returns ``None``.

    Args:
        pool: The pool the checks query.
        settings: Unused; kept for callers of the earlier signature. The
            parameters come from the ``alerts.runtime`` configuration in force.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = get_scheduler()
    if scheduler is None:
        logger.warning("failed_request_alerter: scheduler not running, skipping registration")
        alert_runtime.set_detector_state("unavailable")
        return None

    alerter = FailedRequestAlerter(pool=pool)

    scheduler.add_job(
        alerter.run_check,
        trigger=IntervalTrigger(minutes=1),
        id="failed_request_alerter",
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )
    config = alert_runtime.current()
    logger.info(
        "failed_request_alerter: registered (%s; threshold=%d window_min=%d cooldown_min=%d)",
        "running"
        if config.detector_enabled
        else "paused until SLACK_WEBHOOK_URL or WECOM_ALERTS_WEBHOOK_URL is set",
        config.failed_request_alert_threshold,
        config.failed_request_alert_window_minutes,
        config.failed_request_alert_cooldown_minutes,
    )
    alert_runtime.set_detector_state("registered")
    return alerter
