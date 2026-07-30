"""Endpoint health, passive availability, and circuit-breaker state."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from routing.usage_limit import detect_usage_limit
from serving.observability.alerts import AlertSeverity, alert_slack, escape_slack_text
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

# Keep the established logger namespace during this behavior-preserving
# extraction so existing log filters continue to see circuit events.
logger = get_logger("routing.routers")

# Strong references to fire-and-forget Slack alert tasks. asyncio holds only
# weak refs to scheduled tasks, so without this set the GC may cancel an alert
# mid-flight (e.g. when the breaker that scheduled it is dropped). Tasks
# remove themselves via add_done_callback once they finish.
_ALERT_TASKS: set[asyncio.Task[bool]] = set()

# Bound on the number of distinct users tracked per circuit breaker for a
# single failure streak. Caps memory when a long outage spans many callers;
# users already being tracked keep accumulating their failure counts.
_MAX_TRACKED_OFFENDERS = 50
# How many of the top offenders to name explicitly in the circuit-open alert.
_OFFENDERS_IN_ALERT = 10


def _reason_str(s: str) -> str:
    return s if s and len(s) < 64 else "error"


def _iso_utc(epoch: float) -> str:
    """Render an epoch-seconds instant as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _http_status_of(exc: BaseException) -> int | None:
    """Best-effort extract of an upstream HTTP status code from an exception.

    Adapters surface upstream HTTP errors as exceptions that carry the status on
    one of a few attributes depending on the client library (aiohttp's
    ``ClientResponseError`` uses ``.status``; others use ``.status_code`` or
    ``.code``). Duck-type rather than importing the HTTP client into the routing
    layer. Returns ``None`` when no status is present (e.g. a timeout or
    connection error, which is a genuine upstream fault).
    """
    for attr in ("status", "status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val
    # httpx / requests carry the status on a nested response object.
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int) and 100 <= val <= 599:
                return val
    return None


def _is_client_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is a client error that must not trip the breaker."""
    status = _http_status_of(exc)
    if status is None or not (400 <= status < 500):
        return False
    # 408 and 429 signal upstream slowness/overload, not a malformed request.
    return status not in (408, 429)


def _detail_str(s: str | None, *, limit: int = 500) -> str | None:
    """Normalize an upstream error message for inclusion in alerts."""
    if not s:
        return None
    if len(s) > limit * 2:
        s = s[: limit * 2]
    cleaned = " ".join(s.split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _offender_str() -> str | None:
    """Identify the user behind the current request for failure attribution."""
    ctx = req_ctx.get()
    user_id = ctx.get("user_id")
    raw_name = ctx.get("user_name")
    user_name = " ".join(str(raw_name).split()) if raw_name else None
    if user_id and user_name:
        return f"{user_name} ({user_id})"
    if user_id:
        return str(user_id)
    if user_name:
        return user_name
    return None


class _ProviderHealth:
    """Track provider availability via exponentially weighted counters."""

    def __init__(self, provider: str, alpha: float | None = None) -> None:
        self.provider = provider
        env_alpha = os.getenv("ROUTER_HEALTH_EWMA_ALPHA")
        self.alpha = (
            float(env_alpha) if env_alpha is not None else (alpha if alpha is not None else 0.1)
        )
        self.ewma_success = 1.0
        self.ewma_total = 1.0
        self._lock = threading.Lock()

    def record(self, success: bool) -> None:
        inc_s = 1.0 if success else 0.0
        with self._lock:
            self.ewma_success = (1 - self.alpha) * self.ewma_success + self.alpha * inc_s
            self.ewma_total = (1 - self.alpha) * self.ewma_total + self.alpha

    @property
    def availability(self) -> float:
        if self.ewma_total <= 0:
            return 1.0
        return max(0.0, min(1.0, self.ewma_success / self.ewma_total))


class _CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _CircuitBreaker:
    """Simple circuit breaker per endpoint."""

    def __init__(
        self,
        provider: str,
        *,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
        min_availability: float | None = None,
    ) -> None:
        self.provider = provider
        self.state = _CircuitState.CLOSED
        self.failure_threshold = int(
            os.getenv(
                "CIRCUIT_FAILURE_THRESHOLD",
                str(failure_threshold if failure_threshold is not None else 3),
            )
        )
        self.cooldown_seconds = float(
            os.getenv(
                "CIRCUIT_COOLDOWN_SECONDS",
                str(cooldown_seconds if cooldown_seconds is not None else 30.0),
            )
        )
        self.min_availability = float(
            os.getenv(
                "CIRCUIT_MIN_AVAILABILITY",
                str(min_availability if min_availability is not None else 0.7),
            )
        )
        self.consecutive_failures = 0
        self.last_opened: float | None = None
        # Wall-clock epoch until which repeat circuit-open alerts are muted for a
        # subscription usage-limit outage (0.0 = not muted). Committed only once a
        # page is delivered (_send_circuit_alert) and cleared on recovery.
        self._alert_suppressed_until: float = 0.0
        # True while a usage-limit page is being delivered, so a re-trip in that
        # window can't emit a duplicate page before the deadline is committed.
        self._alert_in_flight: bool = False
        # Bumped on every recovery. A page that finishes delivering after the
        # endpoint recovered carries a stale generation and must not restore its
        # now-obsolete mute — see _send_circuit_alert.
        self._recovery_generation: int = 0
        self._offenders: Counter[str] = Counter()
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == _CircuitState.CLOSED:
                return True
            if self.state == _CircuitState.OPEN:
                if self.last_opened is None:
                    return False
                if (time.perf_counter() - self.last_opened) >= self.cooldown_seconds:
                    self.state = _CircuitState.HALF_OPEN
                    return True
                return False
            return True

    def on_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self._offenders.clear()
            # Recovery re-arms alerting: clear the mute and bump the generation so
            # a page still in flight for the ended outage can't restore a stale
            # deadline, and a later usage-limit outage is free to page again.
            self._alert_suppressed_until = 0.0
            self._recovery_generation += 1
            if self.state in (_CircuitState.OPEN, _CircuitState.HALF_OPEN):
                duration_ms = (
                    (time.perf_counter() - self.last_opened) * 1000.0
                    if self.last_opened is not None
                    else None
                )
                self.state = _CircuitState.CLOSED
                self.last_opened = None
                logger.info(
                    "circuit_closed",
                    extra={
                        "event": "circuit_closed",
                        "provider": self.provider,
                        "duration_ms": duration_ms,
                    },
                )

    def on_failure(
        self,
        *,
        availability: float | None = None,
        reason: str = "error",
        detail: str | None = None,
        offender: str | None = None,
    ) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if offender and (
                offender in self._offenders or len(self._offenders) < _MAX_TRACKED_OFFENDERS
            ):
                self._offenders[offender] += 1
            trip = self.consecutive_failures >= self.failure_threshold
            if availability is not None and availability < self.min_availability:
                trip = True
            if not trip:
                return

            prev_state = self.state
            self.state = _CircuitState.OPEN
            self.last_opened = time.perf_counter()
            if prev_state not in (_CircuitState.CLOSED, _CircuitState.HALF_OPEN):
                return

            # A subscription usage-limit outage re-trips on every half-open probe
            # until the provider's window resets. Fire one alert per outage and
            # stay quiet until the parsed reset time, instead of re-paging every
            # few minutes for hours. The deadline is committed only once the page
            # is delivered (see _send_circuit_alert) and cleared on recovery, so a
            # dropped page never mutes an outage that was never announced.
            now_dt = datetime.now(timezone.utc)
            usage_limit = detect_usage_limit(detail, now=now_dt)
            if usage_limit is not None and (
                self._alert_in_flight or now_dt.timestamp() < self._alert_suppressed_until
            ):
                logger.info(
                    "circuit_open_alert_suppressed",
                    extra={
                        "event": "circuit_open_alert_suppressed",
                        "provider": self.provider,
                        "reason": reason or "unknown",
                        "quota_reset_at": (
                            _iso_utc(self._alert_suppressed_until)
                            if self._alert_suppressed_until
                            else None
                        ),
                        "window": usage_limit.window,
                    },
                )
                return

            context: dict[str, Any] = {
                "provider": self.provider,
                "consecutive_failures": self.consecutive_failures,
                "availability": f"{availability:.2f}" if availability is not None else "n/a",
                "reason": reason or "unknown",
            }
            if detail:
                context["upstream_error"] = detail
            reset_epoch: float | None = None
            if usage_limit is not None:
                reset_epoch = usage_limit.reset_at.timestamp()
                context["quota_reset_at"] = usage_limit.reset_at.isoformat()
            offenders = self._format_offenders()
            if offenders:
                context["offending_users"] = offenders
            logger.warning(
                "circuit_open",
                extra={
                    "event": "circuit_open",
                    "provider": self.provider,
                    "consecutive_failures": self.consecutive_failures,
                    "availability": availability,
                    "reason": reason or "unknown",
                    "upstream_error": detail,
                    "offending_users": dict(self._offenders) or None,
                },
            )
            generation = self._recovery_generation
            if reset_epoch is not None:
                self._alert_in_flight = True
            try:
                task = asyncio.ensure_future(
                    self._send_circuit_alert(context, reset_epoch, generation)
                )
            except RuntimeError:
                # No running loop (sync caller / test): nothing was scheduled, so
                # release the in-flight guard we optimistically set.
                self._alert_in_flight = False
            else:
                _ALERT_TASKS.add(task)
                task.add_done_callback(_ALERT_TASKS.discard)

    async def _send_circuit_alert(
        self, context: dict[str, Any], reset_epoch: float | None, generation: int
    ) -> bool:
        """Deliver a circuit-open page; commit usage-limit suppression on success.

        For a usage-limit trip (``reset_epoch`` set) the suppression deadline is
        recorded only after the page is actually delivered, so a dropped page —
        relay/webhook failure, a global snooze, or the alert cooldown — leaves the
        outage un-muted and it re-pages on the next failing probe. The deadline is
        also withheld when the endpoint recovered while the page was in flight
        (``generation`` no longer current), so a stale mute can't silence a later
        outage. The in-flight guard is released in ``finally`` so a raising send
        never wedges the breaker muted.
        """
        delivered = False
        try:
            delivered = await alert_slack(
                AlertSeverity.ERROR,
                "Provider circuit opened",
                context,
                dedupe_key=f"circuit_open:{self.provider}",
                cooldown_sec=300,
            )
        finally:
            if reset_epoch is not None:
                with self._lock:
                    self._alert_in_flight = False
                    if delivered and generation == self._recovery_generation:
                        self._alert_suppressed_until = reset_epoch
        return delivered

    def _format_offenders(self, *, top: int = _OFFENDERS_IN_ALERT) -> str | None:
        """Render failure-streak offenders for an alert, busiest first."""
        if not self._offenders:
            return None
        named = self._offenders.most_common(top)
        parts = [f"{escape_slack_text(user)} x{count}" for user, count in named]
        remaining = len(self._offenders) - len(named)
        if remaining > 0:
            parts.append(f"+{remaining} more")
        return ", ".join(parts)


class EndpointHealthRegistry:
    """Own passive health and circuit state for endpoint identifiers."""

    def __init__(self) -> None:
        self._health: dict[str, _ProviderHealth] = {}
        self._circuits: dict[str, _CircuitBreaker] = {}
        self._lock = threading.RLock()

    def ensure(self, endpoint_id: str) -> None:
        """Register an endpoint without recording an outcome."""
        with self._lock:
            if endpoint_id not in self._health:
                self._health[endpoint_id] = _ProviderHealth(endpoint_id)
            self._ensure_circuit_locked(endpoint_id)

    def _ensure_circuit_locked(self, endpoint_id: str) -> _CircuitBreaker:
        circuit = self._circuits.get(endpoint_id)
        if circuit is None:
            circuit = self._circuits[endpoint_id] = _CircuitBreaker(endpoint_id)
        return circuit

    def allow_request(self, endpoint_id: str) -> bool:
        """Return whether an endpoint's circuit admits a request."""
        with self._lock:
            circuit = self._ensure_circuit_locked(endpoint_id)
        return circuit.allow_request()

    def record_success(self, endpoint_id: str) -> None:
        """Record a successful endpoint request."""
        with self._lock:
            self.ensure(endpoint_id)
            self._health[endpoint_id].record(True)
            self._circuits[endpoint_id].on_success()

    def record_failure(
        self,
        endpoint_id: str,
        *,
        reason: str = "error",
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """Record a failed endpoint request unless it is a client error."""
        if exc is not None and _is_client_error(exc):
            logger.info(
                "client_error_skip_breaker",
                extra={
                    "event": "client_error_skip_breaker",
                    "endpoint_id": endpoint_id,
                    "status": _http_status_of(exc),
                    "detail": _detail_str(detail),
                },
            )
            return
        with self._lock:
            self.ensure(endpoint_id)
            self._health[endpoint_id].record(False)
            availability = self._health[endpoint_id].availability
            self._circuits[endpoint_id].on_failure(
                availability=availability,
                reason=_reason_str(reason),
                detail=_detail_str(detail),
                offender=_offender_str(),
            )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a detached snapshot of endpoint health and circuit state."""
        with self._lock:
            return {
                endpoint_id: {
                    "availability": health.availability,
                    "circuit_state": self._circuits[endpoint_id].state,
                }
                for endpoint_id, health in self._health.items()
            }
