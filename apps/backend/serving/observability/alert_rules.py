"""AlertEngine for in-process Slack alerting.

Drains the AlertingLogHandler queue, runs rule-based alerts, and schedules
periodic SQL alerts via APScheduler.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from serving.observability import alerts as _alerts
from serving.observability.alerts import (
    AlertSeverity,
    alert_on_transition,
    sweep_stale_breaches,
)
from serving.utils.context import MODEL_NOT_FOUND

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from serving.observability.alert_config import (
        AlertConfig,
        CountRule,
        LatencyRule,
        PendingPrefixCacheLeakConfig,
        ProviderHourlySpend,
        RateRule,
        TrackedTaskFailureRateConfig,
        UserOverrun,
    )
    from serving.observability.log_handler import AlertingLogHandler
    from serving.storage.base import LogStore, OperationalStore

log = logging.getLogger(__name__)


_REQUEST_LOG_LOGGER = "serving.servers.middleware.request_log"

#: How often to look for breaches nothing is evaluating any more. Well under the
#: tracker's own staleness threshold so a stale incident closes promptly rather
#: than at the next multiple of a long interval.
_STALE_SWEEP_INTERVAL_SEC = 60

# 401 is normal SPA token-refresh churn (the auth_failure_spike rule covers
# real auth attacks separately); excluding it from the failed-request rate
# stops admin/refresh sequences from tripping the alert.
# 429 covers quota-exceeded and concurrency-limit rejections — expected user-facing
# rate limiting, not service failures, so excluded from the failure-rate alert.
#
# 404 is NOT blanket-excluded: an upstream provider can return 404 for a routed
# completion (bad provider model id / endpoint path), which the gateway re-raises
# as a 404 — a genuine provider/config regression that must still alert. Only the
# gateway's own model-not-found 404s (a user asking for an unknown/unauthorized
# model) are excluded, via the per-record marker checked in ``_is_failed_request``.
_FAILED_REQUEST_IGNORED_STATUSES = frozenset({401, 429})


def _is_failed_request(item: dict) -> bool:
    """Return True if a window item counts as a service-side failed request.

    Excludes the client-driven statuses in ``_FAILED_REQUEST_IGNORED_STATUSES``
    (401/429) and gateway model-not-found 404s (a request for an unknown or
    unauthorized model — tagged via ``req_ctx.mark_model_not_found``). Genuine
    upstream provider 404s carry no such tag and still count. This mirrors the
    DB-query alerter, which excludes the model-not-found error strings (not all
    404s) from ``FAILURE_PREDICATE_SQL``.
    """
    status = item["status"]
    if status < 400:
        return False
    if status in _FAILED_REQUEST_IGNORED_STATUSES:
        return False
    return not (status == 404 and item.get("client_error_kind") == MODEL_NOT_FOUND)


class _Rule(Protocol):
    name: str

    async def on_record(self, record: logging.LogRecord) -> None: ...


class _SlidingWindow:
    """Simple time-bucketed sliding window holding (ts, value) tuples."""

    def __init__(self, window_sec: int) -> None:
        self.window_sec = window_sec
        self._items: collections.deque[tuple[float, dict]] = collections.deque()

    def add(self, ts: float, value: dict) -> None:
        self._items.append((ts, value))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()

    def items(self, now: float) -> list[dict]:
        self._evict(now)
        return [v for _, v in self._items]


class FailedRequestRateRule:
    """Rule 1: 4xx/5xx error rate over a sliding window."""

    name = "failed_request_rate"

    def __init__(self, cfg: RateRule) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Inspect a request-log record and fire an alert if the failure rate exceeds threshold."""
        if not self._cfg.enabled:
            return
        if record.name != _REQUEST_LOG_LOGGER:
            return
        status = getattr(record, "status_code", None)
        if status is None:
            return
        now = time.time()
        self._window.add(
            now,
            {
                "status": int(status),
                "provider": getattr(record, "provider", None),
                "path": getattr(record, "path", None),
                "client_error_kind": getattr(record, "client_error_kind", None),
            },
        )
        items = self._window.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if _is_failed_request(it))
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            failed_items = [it for it in items if _is_failed_request(it)]
            status_counts: collections.Counter[int] = collections.Counter(
                it["status"] for it in failed_items
            )
            path_counts: collections.Counter[str] = collections.Counter(
                it["path"] for it in failed_items if it["path"]
            )
            prov_counts: collections.Counter[str] = collections.Counter(
                it["provider"] for it in failed_items if it["provider"]
            )
            top_s = ", ".join(f"{s} ({c})" for s, c in status_counts.most_common(3))
            top_paths = ", ".join(f"{p} ({c})" for p, c in path_counts.most_common(3))
            top_p = ", ".join(f"{p} ({c})" for p, c in prov_counts.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} requests, last {self._cfg.window_sec}s)"
                ),
                "top_status_codes": top_s or "n/a",
                "top_paths": top_paths or "n/a",
                "top_providers": top_p or "n/a",
            }

        await alert_on_transition(
            key=self.name,
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title="Failed-request rate exceeded",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class FivexxRateRule:
    """Rule 2: 5xx rate over a sliding window."""

    name = "fivexx_rate"

    def __init__(self, cfg: RateRule) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Inspect a request-log record and fire an alert if the 5xx rate exceeds threshold."""
        if not self._cfg.enabled:
            return
        if record.name != _REQUEST_LOG_LOGGER:
            return
        status = getattr(record, "status_code", None)
        if status is None:
            return
        now = time.time()
        self._window.add(
            now,
            {"status": int(status), "provider": getattr(record, "provider", None)},
        )
        items = self._window.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if it["status"] >= 500)
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            prov_counts: collections.Counter[str] = collections.Counter(
                it["provider"] for it in items if it["status"] >= 500 and it["provider"]
            )
            status_counts: collections.Counter[int] = collections.Counter(
                it["status"] for it in items if it["status"] >= 500
            )
            top_p = ", ".join(f"{p} ({c})" for p, c in prov_counts.most_common(3))
            top_s = ", ".join(f"{s} ({c})" for s, c in status_counts.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} requests, last {self._cfg.window_sec}s)"
                ),
                "top_providers": top_p or "n/a",
                "top_status_codes": top_s or "n/a",
            }

        await alert_on_transition(
            key=self.name,
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title="5xx rate exceeded",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class P95LatencyRule:
    """Rule 3: per-provider p95 latency over a sliding window."""

    name = "p95_latency_per_provider"

    def __init__(self, cfg: LatencyRule) -> None:
        self._cfg = cfg
        self._windows: dict[str, _SlidingWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track per-provider durations and alert when p95 exceeds the configured threshold."""
        if not self._cfg.enabled:
            return
        if record.name != _REQUEST_LOG_LOGGER:
            return
        provider = getattr(record, "provider", None)
        if not provider:
            return
        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is None:
            return
        win = self._windows.setdefault(provider, _SlidingWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"duration_ms": int(duration_ms)})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        sorted_durations = sorted(it["duration_ms"] for it in items)
        idx = int(0.95 * (len(sorted_durations) - 1))
        p95 = sorted_durations[idx]
        threshold = self._cfg.overrides.get(provider, {}).get(
            "threshold_ms", self._cfg.threshold_ms
        )

        def breach_context() -> dict[str, Any]:
            p99_idx = int(0.99 * (len(sorted_durations) - 1))
            p99 = sorted_durations[p99_idx]
            return {
                "provider": provider,
                "p95_ms": p95,
                "p99_ms": p99,
                "samples": len(items),
                "window_sec": self._cfg.window_sec,
                "threshold_ms": threshold,
            }

        await alert_on_transition(
            key=f"p95_latency:{provider}",
            breached=p95 >= threshold,
            severity=AlertSeverity.WARN,
            title=f"p95 latency exceeded for provider {provider}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class AuthFailureSpikeRule:
    """Rule 4: auth-failure count over a sliding window."""

    name = "auth_failure_spike"

    def __init__(self, cfg: CountRule) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track auth-failure events and alert when the count exceeds threshold in-window."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "auth_failure":
            return
        now = time.time()
        self._window.add(
            now,
            {
                "remote_ip": getattr(record, "remote_ip", None),
                "key_prefix": getattr(record, "key_prefix", None),
            },
        )
        items = self._window.items(now)

        def breach_context() -> dict[str, Any]:
            ip_counts: collections.Counter[str] = collections.Counter(
                it["remote_ip"] for it in items if it["remote_ip"]
            )
            key_counts: collections.Counter[str] = collections.Counter(
                it["key_prefix"] for it in items if it["key_prefix"]
            )
            return {
                "count": len(items),
                "window_sec": self._cfg.window_sec,
                "top_ips": (
                    ", ".join(f"{ip} ({c})" for ip, c in ip_counts.most_common(3)) or "n/a"
                ),
                "top_key_prefixes": (
                    ", ".join(f"{p} ({c})" for p, c in key_counts.most_common(3)) or "n/a"
                ),
            }

        await alert_on_transition(
            key="auth_failure_spike",
            breached=len(items) > self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="Auth failure spike",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class PendingPrefixCacheLeakRule:
    """Alert when pending RouteWise prefix-cache evictions exceed a threshold.

    Fires when more than ``threshold_count``
    ``routewise_prefix_cache_entry_evicted`` events arrive within
    ``window_sec``. A sustained crossing means pending prefix-cache state is
    not being consumed before its lifetime or capacity bound is reached.
    """

    name = "prefix_cache_pending_leak"

    def __init__(self, cfg: PendingPrefixCacheLeakConfig) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Update the window from a prefix-cache entry eviction event."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "routewise_prefix_cache_entry_evicted":
            return
        now = time.time()
        self._window.add(
            now,
            {
                "reason": getattr(record, "reason", "unknown"),
                "age_sec": getattr(record, "age_sec", None),
                "idle_sec": getattr(record, "idle_sec", None),
                "pending_count": getattr(record, "pending_count", None),
                "capacity": getattr(record, "capacity", None),
            },
        )
        items = self._window.items(now)

        def breach_context() -> dict[str, Any]:
            reason_counts = collections.Counter(str(item["reason"]) for item in items)
            ages = [item["age_sec"] for item in items if isinstance(item["age_sec"], (int, float))]
            idle_times = [
                item["idle_sec"] for item in items if isinstance(item["idle_sec"], (int, float))
            ]
            pending_counts = [
                item["pending_count"] for item in items if isinstance(item["pending_count"], int)
            ]
            capacities = [item["capacity"] for item in items if isinstance(item["capacity"], int)]
            return {
                "evicted_count": len(items),
                "window_sec": self._cfg.window_sec,
                "ttl_count": reason_counts["ttl"],
                "size_cap_count": reason_counts["size_cap"],
                "max_age_sec": max(ages, default=0),
                "max_idle_sec": max(idle_times, default=0),
                "max_pending_count": max(pending_counts, default=0),
                "capacity": max(capacities, default=0),
            }

        await alert_on_transition(
            key="prefix_cache_pending_leak",
            breached=len(items) > self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="RouteWise pending prefix-cache entries leaking",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class TrackedTaskFailureRateRule:
    """Alert when a tracked task type fails at a sustained rate.

    Reads ``tracked_task_completed`` log records emitted by the
    ``serving.observability.tracked_tasks.tracked_task`` helper and
    keeps a per-``task_name`` sliding window. Fires once the failure
    rate over the window exceeds ``threshold_pct`` and at least
    ``min_samples`` completions are observed.
    """

    name = "tracked_task_failure_rate"

    def __init__(self, cfg: TrackedTaskFailureRateConfig) -> None:
        self._cfg = cfg
        self._windows: dict[str, _SlidingWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Update the per-task-name window from a tracked_task_completed event."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "tracked_task_completed":
            return
        task_name = getattr(record, "task_name", None) or "unknown"
        success = bool(getattr(record, "success", True))
        win = self._windows.setdefault(task_name, _SlidingWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"success": success})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if not it["success"])
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            return {
                "task_name": task_name,
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} tasks, last {self._cfg.window_sec}s)"
                ),
            }

        await alert_on_transition(
            key=f"tracked_task_failure:{task_name}",
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title=f"Tracked-task failure rate exceeded for {task_name}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            now=now,
        )


class UserCostOverrunJob:
    """Periodic job 8: per-user daily-cost threshold overrun."""

    name = "user_cost_overrun"

    def __init__(self, cfg: UserOverrun, op_store: Any) -> None:
        self._cfg = cfg
        self._op_store = op_store

    async def run(self) -> None:
        """Query users whose daily cost exceeds their role threshold and emit alerts."""
        if not self._cfg.enabled or self._op_store is None:
            return
        try:
            rows = await self._op_store.query_users_over_daily_threshold(
                self._cfg.thresholds_per_role
            )
        except Exception:
            log.exception("user_cost_overrun query failed")
            return
        today = dt.date.today().isoformat()
        for user_id, role, daily_cost in rows:
            # Through the tracker, not straight to the sink: the key embeds the
            # day, so once it rolls over nothing observes this one again and the
            # stale sweep is the only thing that can close its incident. Calling
            # alert_slack directly would leave it open forever.
            await _alerts.alert_on_transition(
                key=f"cost_overrun:{user_id}:{today}",
                breached=True,
                severity=AlertSeverity.WARN,
                title="User cost overrun",
                context=lambda user_id=user_id, role=role, daily_cost=daily_cost: {
                    "user_id": user_id,
                    "role": role,
                    "daily_cost": f"${daily_cost:.2f}",
                    "threshold": f"${self._cfg.thresholds_per_role.get(role, 0):.2f}",
                },
                cooldown_sec=self._cfg.cooldown_sec,
            )


class ProviderHourlySpendJob:
    """Periodic job 9: per-provider hourly spend over budget."""

    name = "provider_hourly_spend"

    def __init__(self, cfg: ProviderHourlySpend, log_store: Any) -> None:
        self._cfg = cfg
        self._log_store = log_store

    async def run(self) -> None:
        """Query per-provider hourly spend and emit alerts when budgets are exceeded."""
        if not self._cfg.enabled or self._log_store is None:
            return
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        hour_iso = now.isoformat()
        try:
            spends = await self._log_store.query_provider_hourly_spend(hour_iso)
        except Exception:
            log.exception("provider_hourly_spend query failed")
            return
        for provider, spend in spends.items():
            budget = self._cfg.budgets.get(provider)
            if budget is None or spend < budget:
                continue
            # As with the daily cost job: the key embeds the hour, so the stale
            # sweep is the only thing that can ever close this incident and the
            # tracker has to know about it.
            await _alerts.alert_on_transition(
                key=f"provider_spend:{provider}:{hour_iso}",
                breached=True,
                severity=AlertSeverity.WARN,
                title=f"Provider hourly spend exceeded budget for {provider}",
                context=lambda provider=provider, spend=spend, budget=budget: {
                    "provider": provider,
                    "hourly_spend": f"${spend:.2f}",
                    "budget": f"${budget:.2f}",
                    "hour": hour_iso,
                },
                cooldown_sec=self._cfg.cooldown_sec,
            )


class AlertEngine:
    """Drains structured log records, fans out to rules, runs periodic SQL jobs."""

    def __init__(
        self,
        *,
        handler: AlertingLogHandler,
        config: AlertConfig,
        scheduler: AsyncIOScheduler | None,
        op_store: OperationalStore | None,
        log_store: LogStore | None,
    ) -> None:
        self._handler = handler
        self._config = config
        self._scheduler = scheduler
        self._op_store = op_store
        self._log_store = log_store
        self._task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._rules: list[_Rule] = []
        self._scheduled_jobs: list[Any] = []

    def is_running(self) -> bool:
        """Return True if the drain task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Build rules, schedule periodic jobs, and start the drain task."""
        self._build_rules()
        self._align_stale_window()
        self._schedule_periodic_jobs()
        self._task = asyncio.create_task(self._drain(), name="AlertEngine.drain")
        log.info(
            "AlertEngine started with %d rules and %d periodic jobs",
            len(self._rules),
            len(self._scheduled_jobs),
        )

    async def stop(self) -> None:
        """Cancel the drain task and remove scheduled jobs."""
        import contextlib as _cl

        if self._task and not self._task.done():
            self._task.cancel()
            with _cl.suppress(asyncio.CancelledError):
                await self._task
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            with _cl.suppress(asyncio.CancelledError):
                await self._sweep_task
        self._sweep_task = None
        for job in self._scheduled_jobs:
            try:
                job.remove()
            except Exception:
                log.exception("failed to remove alert job")
        self._scheduled_jobs.clear()
        self._task = None

    def _align_stale_window(self) -> None:
        """Keep the stale sweep from closing an incident its rule still holds.

        A rule's breach is computed over ``window_sec`` of request records, so
        a firing key that stops being observed is only genuinely stale once that
        window can no longer contain a breaching sample. With the shipped
        config ``failed_request_rate.window_sec`` is 3600 while the tracker's
        default staleness is 900, which would report recovery 45 minutes before
        the breached samples aged out.
        """
        windows = [
            rule.window_sec
            for rule in vars(self._config.rules).values()
            if getattr(rule, "enabled", False) and getattr(rule, "window_sec", None)
        ]
        intervals = [
            cfg.check_interval_sec
            for cfg in (
                self._config.cost.user_overrun,
                self._config.cost.provider_hourly_spend,
            )
            if getattr(cfg, "enabled", False) and getattr(cfg, "check_interval_sec", None)
        ]
        longest = max([*windows, *intervals, 0])
        if longest:
            # One extra window of slack, so an evaluation that lands late does
            # not race the sweep.
            _alerts.set_stale_after(longest * 2)

    def _build_rules(self) -> None:
        self._rules.append(FailedRequestRateRule(self._config.rules.failed_request_rate))
        self._rules.append(FivexxRateRule(self._config.rules.fivexx_rate))
        self._rules.append(P95LatencyRule(self._config.rules.p95_latency_per_provider))
        self._rules.append(AuthFailureSpikeRule(self._config.rules.auth_failure_spike))
        # No concurrency-exhausted rule: a user exhausting their per-user quota
        # or concurrency limit is expected user-facing rate limiting (429), not a
        # service fault, so it must never page Slack.
        self._rules.append(PendingPrefixCacheLeakRule(self._config.rules.prefix_cache_pending_leak))
        self._rules.append(TrackedTaskFailureRateRule(self._config.rules.tracked_task_failure_rate))

    def _schedule_periodic_jobs(self) -> None:
        if self._scheduler is None:
            # The sweep is the only thing that closes an incident whose rule
            # stopped being evaluated, and it must not depend on the optional
            # Postgres-backed scheduler — a deployment without one would leave
            # every such incident open forever. It runs on its own task.
            self._sweep_task = asyncio.ensure_future(self._sweep_forever())
            return
        from apscheduler.triggers.interval import IntervalTrigger

        if self._op_store is not None and self._config.cost.user_overrun.enabled:
            cost_job = UserCostOverrunJob(self._config.cost.user_overrun, self._op_store)
            scheduled = self._scheduler.add_job(
                cost_job.run,
                trigger=IntervalTrigger(seconds=self._config.cost.user_overrun.check_interval_sec),
                id="alert_user_cost_overrun",
                replace_existing=True,
            )
            self._scheduled_jobs.append(scheduled)

        if self._log_store is not None and self._config.cost.provider_hourly_spend.enabled:
            spend_job = ProviderHourlySpendJob(
                self._config.cost.provider_hourly_spend, self._log_store
            )
            scheduled = self._scheduler.add_job(
                spend_job.run,
                trigger=IntervalTrigger(
                    seconds=self._config.cost.provider_hourly_spend.check_interval_sec
                ),
                id="alert_provider_hourly_spend",
                replace_existing=True,
            )
            self._scheduled_jobs.append(scheduled)

        # Record-driven rules resolve when the metric recovers, but two classes
        # of breach never re-evaluate on their own: a rule whose traffic stops
        # entirely, and the budget jobs above, whose incident key embeds the day
        # or hour so the previous period is simply never observed again. Both
        # would leave incidents open forever. This sweep is what closes them.
        scheduled = self._scheduler.add_job(
            self._sweep_stale_breaches,
            trigger=IntervalTrigger(seconds=_STALE_SWEEP_INTERVAL_SEC),
            id="alert_stale_breach_sweep",
            replace_existing=True,
        )
        self._scheduled_jobs.append(scheduled)

    async def _sweep_stale_breaches(self) -> None:
        """Resolve incidents whose rule or period stopped producing evaluations."""
        await sweep_stale_breaches()

    async def _sweep_forever(self) -> None:
        """Run the stale-breach sweep without an external scheduler."""
        while True:
            try:
                await asyncio.sleep(_STALE_SWEEP_INTERVAL_SEC)
                await sweep_stale_breaches()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failing sweep must not take the alert engine down with it.
                log.exception("stale breach sweep failed")

    async def _drain(self) -> None:
        try:
            while True:
                record = await self._handler.queue.get()
                for rule in self._rules:
                    try:
                        await rule.on_record(record)
                    except Exception:
                        log.exception("rule %s raised", rule.name)
        except asyncio.CancelledError:
            raise
