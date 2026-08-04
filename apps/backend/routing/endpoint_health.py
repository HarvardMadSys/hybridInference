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
from serving.exceptions import operator_safe_error
from serving.observability.alerts import AlertSeverity, alert_on_transition, escape_slack_text
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

# Upstream statuses that can only mean the *gateway's* configured credential was
# rejected, whoever the provider is. The client's own credential is validated by
# serving.servers.auth before routing, so by the time an adapter runs the only
# credential in play is the one this deployment configured (models.yaml
# ``api_keys:``). 401 (WWW-Authenticate) and 407 (Proxy-Authenticate) are
# unambiguous credential challenges, so an outage here is 100% fatal for every
# user until an operator fixes the key — never a per-request client mistake.
_AUTH_MISCONFIG_STATUSES = frozenset({401, 407})

# 403 only counts as credential misconfiguration for endpoints this deployment
# owns. Remote providers overload 403 for per-request rejections — content
# policy, safety blocks, region/IP restrictions — where the upstream is healthy
# and correctly refused one prompt; tripping the shared breaker on those would
# reintroduce the "one user's bad request opens the circuit for everyone"
# cascade. A gateway-owned endpoint has no content-policy layer and no per-user
# identity reaching it, so its 403 is an ACL/credential fault, all-users fatal.
_GATEWAY_OWNED_AUTH_STATUSES = frozenset({403})

# Cooldown for the upstream-auth page. Matches the circuit-open alert so a
# persistent misconfiguration re-pages on the same cadence.
_AUTH_ALERT_COOLDOWN_SEC = 300

# Title of the upstream-auth page. Shared by the firing and resolving edges so
# the recovery card reads as the same incident ("Recovered: <title>").
_AUTH_ALERT_TITLE = "Upstream rejected gateway credential"

# Minimum spacing between *scheduling* upstream-auth pages for one endpoint. The
# condition fails 100% of requests, so without this every request would spawn an
# alert task and pay ``alert_slack``'s snooze lookup only to be dropped by the
# cooldown above. Deliberately far shorter than that cooldown so a page dropped
# by an unreachable sink is retried in seconds rather than after five minutes.
_AUTH_ALERT_SCHEDULE_INTERVAL_SEC = 5.0


def _auth_alert_key(endpoint_id: str) -> str:
    """Return the transition/dedupe key for one endpoint's upstream-auth incident.

    One key per endpoint: a wrong key on one local proxy must not mute or resolve
    another endpoint's rejection. Shared by the firing and resolving edges, since
    the tracker matches them by key.
    """
    return f"upstream_auth:{endpoint_id}"


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


def _is_gateway_owned_endpoint(endpoint_id: str) -> bool:
    """Return whether ``endpoint_id`` names an endpoint this deployment runs.

    ``serving.servers.registry._make_provider_id`` stamps ``:local-<port>`` (or
    ``:local`` when the base_url carries no port) for every endpoint whose host
    is in its ``_LOCAL_HOSTS`` set, so the suffix is the existing marker for
    "the operator owns both ends of this connection".

    Matched exactly rather than by prefix, because the same function mints
    ``:{name}-api`` for a *remote* host, where ``name`` is the first label left
    after stripping ``api.``/``llm.``. A remote provider hosted at
    ``api.local.<tld>`` (or ``local.<tld>``) therefore yields ``:local-api``,
    which a ``startswith("local-")`` test would report as gateway-owned — and
    that would escalate the remote provider's per-request content-policy and
    region 403s into the shared breaker, the exact cascade the 403 scoping below
    exists to prevent.
    """
    _, separator, suffix = endpoint_id.rpartition(":")
    if not separator:
        return False
    if suffix == "local":
        return True
    return suffix.startswith("local-") and suffix.removeprefix("local-").isdigit()


def _is_auth_misconfig(status: int | None, endpoint_id: str) -> bool:
    """Return whether ``status`` means this gateway's own credential was rejected.

    Such a rejection is not a client error: the caller never supplies the
    upstream credential, so no request the user could have sent would have
    succeeded. It is a deployment-wide fault and must reach the breaker and an
    operator, which is why it is excluded from the client-error exemption below.
    """
    if status is None:
        return False
    if status in _AUTH_MISCONFIG_STATUSES:
        return True
    return status in _GATEWAY_OWNED_AUTH_STATUSES and _is_gateway_owned_endpoint(endpoint_id)


def _is_client_error(exc: BaseException, endpoint_id: str = "") -> bool:
    """Return whether ``exc`` is a client error that must not trip the breaker.

    ``endpoint_id`` decides the ambiguous 403 case; the default treats the
    endpoint as remote, i.e. keeps a 403 exempt.
    """
    status = _http_status_of(exc)
    if status is None or not (400 <= status < 500):
        return False
    # 408 and 429 signal upstream slowness/overload, not a malformed request.
    if status in (408, 429):
        return False
    # Auth statuses reject the gateway's credential, not the user's request.
    return not _is_auth_misconfig(status, endpoint_id)


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


def _fire_and_forget(coro: Any) -> None:
    """Schedule an alert delivery without holding up the failing request.

    Keeps a strong reference for the task's lifetime (asyncio holds only weak
    ones) and closes the coroutine when there is no loop to run it on, so a sync
    caller — a unit test, or teardown — does not leak a never-awaited coroutine.
    """
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:
        coro.close()
        return
    _ALERT_TASKS.add(task)
    task.add_done_callback(_ALERT_TASKS.discard)


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
        # HTTP status of the most recent recorded failure, or None when no
        # failure has been recorded (or the failure carried no status, e.g. a
        # timeout). Sticky across recovery: purely a diagnostic breadcrumb,
        # ``consecutive_auth_rejections`` is what says whether it is still true.
        self.last_error_status: int | None = None
        # Length of the current run of gateway-credential rejections. Non-zero
        # means every request to this endpoint is being refused for a reason no
        # user can affect, which is what /health/deep reports as degraded.
        self.consecutive_auth_rejections = 0
        self._lock = threading.Lock()

    def record(self, success: bool) -> bool:
        """Record an outcome; return whether it ended a credential-rejection run.

        That return is the upstream-auth incident's one healthy edge: the caller
        turns it into the ``resolved`` transition that closes the page, exactly
        as ``_CircuitBreaker.on_success`` closes ``circuit_open``.
        """
        inc_s = 1.0 if success else 0.0
        with self._lock:
            self.ewma_success = (1 - self.alpha) * self.ewma_success + self.alpha * inc_s
            self.ewma_total = (1 - self.alpha) * self.ewma_total + self.alpha
            if success and self.consecutive_auth_rejections:
                # A single accepted request proves the credential works again.
                self.consecutive_auth_rejections = 0
                return True
            return False

    def note_failure(self, *, status: int | None, auth_misconfig: bool) -> tuple[int, bool]:
        """Record the failing status; report the auth-rejection run and its end.

        Returns ``(run_length, run_ended)``: the current length of the
        credential-rejection run, and whether *this* failure is what ended a
        non-empty one, which the caller needs to resolve the incident exactly
        once rather than on every subsequent failure.
        """
        with self._lock:
            if status is not None:
                self.last_error_status = status
            if auth_misconfig:
                self.consecutive_auth_rejections += 1
                return self.consecutive_auth_rejections, False
            # Any other kind of failure ends the auth run: whatever is wrong
            # now, the credential was accepted far enough to fail otherwise.
            ended = self.consecutive_auth_rejections > 0
            self.consecutive_auth_rejections = 0
            return 0, ended

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
        # Recovery generation of the usage-limit page currently being delivered
        # (None when none). A re-trip of the *same* generation is a duplicate and
        # is suppressed while the page sends; once recovery bumps the generation
        # this guard is stale, so a new outage's page is not blocked by it.
        self._alert_in_flight_generation: int | None = None
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
                # The breaker already knew the outage was over and only logged
                # it. Reporting it is what lets the incident close instead of
                # sitting open until the principal quota runs out.
                #
                # With no running loop (sync teardown) nothing else will close
                # this incident: a circuit has one healthy edge and state alerts
                # are never swept, because silence is not recovery.
                _fire_and_forget(
                    alert_on_transition(
                        key=f"circuit_open:{self.provider}",
                        breached=False,
                        severity=AlertSeverity.ERROR,
                        title="Provider circuit opened",
                        context=dict,
                        cooldown_sec=300,
                        kind="state",
                    )
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
            # While the endpoint is inside a known usage-limit outage — a page for
            # the current generation is mid-delivery, or its mute deadline has not
            # passed — stay silent no matter how *this* failure presents. A
            # half-open re-trip often surfaces differently from the original 429
            # (e.g. KeyPoolExhausted once the key's 429-backoff exceeds the circuit
            # cooldown, or a timeout); those must not resurrect the storm. The
            # in-flight guard is scoped to the generation, so a recovery (which
            # bumps it) does not let a stale in-flight page mute the *next* outage;
            # recovery also clears the deadline, re-arming the endpoint.
            in_flight_current = self._alert_in_flight_generation == self._recovery_generation
            if in_flight_current or now_dt.timestamp() < self._alert_suppressed_until:
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
                        "window": usage_limit.window if usage_limit is not None else "active",
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
                self._alert_in_flight_generation = generation
            try:
                task = asyncio.ensure_future(
                    self._send_circuit_alert(context, reset_epoch, generation)
                )
            except RuntimeError:
                # No running loop (sync caller / test): nothing was scheduled, so
                # release the in-flight guard we optimistically set (unless a newer
                # generation already claimed it).
                if self._alert_in_flight_generation == generation:
                    self._alert_in_flight_generation = None
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
            # Route the breach through the transition tracker so the incident
            # opens (and later closes via on_success's breached=False) on the
            # control plane; its return is whether a page was actually sent.
            delivered = await alert_on_transition(
                key=f"circuit_open:{self.provider}",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=lambda: context,
                cooldown_sec=300,
                kind="state",
            )
        finally:
            if reset_epoch is not None:
                with self._lock:
                    # Only clear the guard if this page still owns it; after a
                    # recovery a newer generation's page may have claimed it.
                    if self._alert_in_flight_generation == generation:
                        self._alert_in_flight_generation = None
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
        # Monotonic instant each endpoint last had an upstream-auth page
        # scheduled, throttling the scheduling itself (see
        # ``_AUTH_ALERT_SCHEDULE_INTERVAL_SEC``).
        self._auth_alert_at: dict[str, float] = {}
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
            auth_run_ended = self._health[endpoint_id].record(True)
            if auth_run_ended:
                # Re-arm scheduling: a later outage must page on its first
                # rejection instead of serving out a window this one opened.
                self._auth_alert_at.pop(endpoint_id, None)
            self._circuits[endpoint_id].on_success()
        if auth_run_ended:
            # Outside the lock, like the failure path: alert delivery must not
            # hold the lock every other endpoint's health accounting needs.
            self._resolve_auth_misconfig(endpoint_id)

    def record_failure(
        self,
        endpoint_id: str,
        *,
        reason: str = "error",
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """Record a failed endpoint request unless it is a client error."""
        status = _http_status_of(exc) if exc is not None else None
        # Checked before the client-error exemption: an auth rejection sits in the
        # 4xx range but is a deployment fault, so it must not be exempted.
        auth_misconfig = _is_auth_misconfig(status, endpoint_id)
        if not auth_misconfig and exc is not None and _is_client_error(exc, endpoint_id):
            logger.info(
                "client_error_skip_breaker",
                extra={
                    "event": "client_error_skip_breaker",
                    "endpoint_id": endpoint_id,
                    "status": status,
                    "detail": _detail_str(detail),
                },
            )
            return
        # Some callers (e.g. the RouteWise hedging paths) pass only ``exc``; the
        # router path passes an explicit ``detail``. Derive an operator-safe detail
        # from the exception when absent so usage-limit detection and the alert
        # text work uniformly regardless of call site.
        if detail is None and exc is not None:
            detail = operator_safe_error(exc)
        safe_detail = _detail_str(detail)
        with self._lock:
            self.ensure(endpoint_id)
            health = self._health[endpoint_id]
            health.record(False)
            auth_rejections, auth_run_ended = health.note_failure(
                status=status, auth_misconfig=auth_misconfig
            )
            if auth_run_ended:
                self._auth_alert_at.pop(endpoint_id, None)
            availability = health.availability
            self._circuits[endpoint_id].on_failure(
                availability=availability,
                reason=_reason_str(reason),
                detail=safe_detail,
                offender=_offender_str(),
            )
        if auth_misconfig:
            # Reported outside the registry lock: alert scheduling must not hold
            # the lock every other endpoint's health accounting needs.
            self._report_auth_misconfig(
                endpoint_id,
                status=status,
                detail=safe_detail,
                consecutive=auth_rejections,
            )
        elif auth_run_ended:
            # A different failure ended the run: the credential was accepted far
            # enough to fail for another reason, so the *credential* incident is
            # over even though the endpoint is not well. What is wrong now is the
            # breaker's ``circuit_open`` incident to report, and leaving this one
            # open would contradict the ``consecutive_auth_rejections`` reset that
            # /health/deep reads.
            self._resolve_auth_misconfig(endpoint_id)

    def _report_auth_misconfig(
        self,
        endpoint_id: str,
        *,
        status: int | None,
        detail: str | None,
        consecutive: int,
    ) -> None:
        """Log and page for an upstream rejection of this gateway's credential.

        Fired on the *first* rejection rather than waiting for the breaker to
        trip, because there is no partial version of this failure: every request
        to the endpoint is refused until an operator rotates or fixes the key.

        Routed through ``alert_on_transition`` as a *state* alert, exactly like
        the breaker's own ``circuit_open``: this is a condition the process
        already tracks, with one healthy edge (the first accepted request — see
        ``_resolve_auth_misconfig``) and no meaningful staleness, since silence is
        not evidence the credential works. A fire-only ``alert_slack`` would open
        an incident nothing could ever close, holding principal quota until it is
        exhausted and real outages start being suppressed.

        The log line is emitted for every rejection — it is the audit trail, and
        it replaces an equally frequent INFO line — while the page is throttled.
        """
        logger.warning(
            "upstream_auth_misconfig",
            extra={
                "event": "upstream_auth_misconfig",
                "endpoint_id": endpoint_id,
                "status": status,
                "consecutive_auth_rejections": consecutive,
                "upstream_error": detail,
            },
        )
        now = time.monotonic()
        with self._lock:
            last_scheduled = self._auth_alert_at.get(endpoint_id)
            if (
                last_scheduled is not None
                and (now - last_scheduled) < _AUTH_ALERT_SCHEDULE_INTERVAL_SEC
            ):
                return
            self._auth_alert_at[endpoint_id] = now
        context: dict[str, Any] = {
            "endpoint_id": endpoint_id,
            "status": status,
            "consecutive_auth_rejections": consecutive,
            "impact": "every request to this endpoint is rejected; no caller can work around it",
            "likely_cause": "gateway-configured api_keys wrong, expired, or revoked",
        }
        if detail:
            context["upstream_error"] = detail
        _fire_and_forget(
            alert_on_transition(
                key=_auth_alert_key(endpoint_id),
                breached=True,
                severity=AlertSeverity.ERROR,
                title=_AUTH_ALERT_TITLE,
                context=lambda: context,
                cooldown_sec=_AUTH_ALERT_COOLDOWN_SEC,
                kind="state",
            )
        )

    def _resolve_auth_misconfig(self, endpoint_id: str) -> None:
        """Close the upstream-auth incident now that the credential works again.

        This is the state incident's only healthy edge, so it is what stops the
        page from staying open forever: the endpoint just did the thing the page
        said no caller could make it do. Called on the transition out of a
        rejection run — an accepted request, or a failure of any other kind —
        never on every success, so the resolution is emitted once per outage.

        With no running loop (sync teardown) nothing else will close this
        incident: state alerts report one healthy edge and are never swept,
        because silence is not recovery. Same limitation, and the same reason for
        it, as ``_CircuitBreaker.on_success``.
        """
        logger.info(
            "upstream_auth_recovered",
            extra={
                "event": "upstream_auth_recovered",
                "endpoint_id": endpoint_id,
            },
        )
        _fire_and_forget(
            alert_on_transition(
                key=_auth_alert_key(endpoint_id),
                breached=False,
                severity=AlertSeverity.ERROR,
                title=_AUTH_ALERT_TITLE,
                context=dict,
                cooldown_sec=_AUTH_ALERT_COOLDOWN_SEC,
                kind="state",
            )
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a detached snapshot of endpoint health and circuit state."""
        with self._lock:
            return {
                endpoint_id: {
                    "availability": health.availability,
                    "circuit_state": self._circuits[endpoint_id].state,
                    "last_error_status": health.last_error_status,
                    "consecutive_auth_rejections": health.consecutive_auth_rejections,
                }
                for endpoint_id, health in self._health.items()
            }
