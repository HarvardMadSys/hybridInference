"""Endpoint health, passive availability, and circuit-breaker state."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from routing.usage_limit import MIN_ALERT_GAP, detect_usage_limit
from serving.adapters.key_pool import KeyPoolRoleRestricted
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
#
# 403 is deliberately NOT here, even though a gateway-owned endpoint's 403 is the
# same class of fault. Remote providers overload 403 for per-request rejections —
# content policy, safety blocks, region/IP restrictions — where the upstream is
# healthy and correctly refused one prompt, so escalating those would reinstate
# the "one user's bad request opens the circuit for everyone" cascade the
# client-error exemption exists to prevent. Scoping the escalation to
# gateway-owned endpoints needs an ownership signal, and the only one available
# at this call site is the ``endpoint_id`` string, which cannot carry it:
#
# - ``serving.servers.registry._make_provider_id`` stamps ``:local-<port>`` only
#   for hosts in its four-entry ``_LOCAL_HOSTS`` set, so a gateway-owned server on
#   the LAN (``http://10.0.0.5:8000`` -> ``:10-api``) reads as remote.
# - Conversely the identifier is not authoritative: admin runtime routes carry a
#   ``route_id`` that ``_runtime_route_id_for_target`` only checks against other
#   providers' generated forms, so any value that ends in ``:local-<digits>``
#   would be honoured here as gateway-owned whatever its base_url.
#
# Deriving ownership from the endpoint's base_url instead would mean threading it
# (or an explicit ownership flag) through ``record_failure`` at every router,
# RouteWise, and hedging call site — new plumbing on the failure path to widen an
# escalation that is already covered indirectly: 403 is key-specific for
# ``KeyPool``, so an endpoint answering 403 to everything mutes its keys and then
# fails with ``KeyPoolExhausted``, which carries no HTTP status, is not exempt,
# and trips the breaker into its own ``circuit_open`` page. So 403 keeps the
# pre-existing exemption, and only the unambiguous statuses escalate.
_AUTH_MISCONFIG_STATUSES = frozenset({401, 407})

# Cooldown for the upstream-auth page. Matches the circuit-open alert so a
# persistent misconfiguration re-pages on the same cadence.
_AUTH_ALERT_COOLDOWN_SEC = 300

# Minimum spacing between an endpoint's plan-usage pages, in seconds. Same floor
# the parser applies to its suppression deadlines, held here as the epoch-clock
# comparison the breaker needs.
_MIN_ALERT_GAP_SEC = MIN_ALERT_GAP.total_seconds()

# Whether a subscription usage-limit trip pages at all. A deployment that runs on
# subscription plans exhausts them as a matter of course: the page names nothing
# an operator can act on — no key rotation or restart shortens the provider's
# window — and the endpoint re-arms itself when it resets, so such a deployment
# can drop the page entirely. Default True keeps the one-page-per-outage
# behaviour; set from ``state_changes.circuit_open.page_on_usage_limit`` in
# alerts.yaml at startup (see ``set_usage_limit_paging``). Non-usage-limit trips
# page regardless — this gate is scoped to outages the parser recognizes as a
# plan window running dry.
_PAGE_ON_USAGE_LIMIT = True

# Title of the upstream-auth page. Shared by the firing and resolving edges so
# the recovery card reads as the same incident ("Recovered: <title>").
_AUTH_ALERT_TITLE = "Upstream rejected gateway credential"

# Minimum spacing between *scheduling* upstream-auth pages for one endpoint. The
# condition fails 100% of requests, so without this every request would schedule a
# send and pay ``alert_slack``'s snooze lookup only to be dropped by the cooldown
# above. Deliberately far shorter than that cooldown so a page dropped by an
# unreachable sink is retried in seconds rather than after five minutes.
_AUTH_ALERT_SCHEDULE_INTERVAL_SEC = 5.0


def set_usage_limit_paging(enabled: bool) -> None:
    """Set whether subscription usage-limit trips page (see ``_PAGE_ON_USAGE_LIMIT``)."""
    global _PAGE_ON_USAGE_LIMIT
    _PAGE_ON_USAGE_LIMIT = bool(enabled)


def _auth_alert_key(endpoint_id: str) -> str:
    """Return the transition/dedupe key for one endpoint's upstream-auth incident.

    One key per endpoint: a wrong key on one local proxy must not mute or resolve
    another endpoint's rejection. Shared by the firing and resolving edges, since
    the tracker matches them by key. It is also what orders them: ``alert_slack``
    serializes per dedupe key, so one endpoint's page and its recovery cannot
    overtake each other while another endpoint's outage still pages immediately.
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


def _is_auth_misconfig(status: int | None) -> bool:
    """Return whether ``status`` means this gateway's own credential was rejected.

    Such a rejection is not a client error: the caller never supplies the
    upstream credential, so no request the user could have sent would have
    succeeded. It is a deployment-wide fault and must reach the breaker and an
    operator, which is why it is excluded from the client-error exemption below.

    Provider-independent by construction — see ``_AUTH_MISCONFIG_STATUSES`` for
    why only the unambiguous credential challenges qualify.
    """
    return status is not None and status in _AUTH_MISCONFIG_STATUSES


def _is_client_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is a client error that must not trip the breaker."""
    status = _http_status_of(exc)
    if status is None or not (400 <= status < 500):
        return False
    # 408 and 429 signal upstream slowness/overload, not a malformed request.
    if status in (408, 429):
        return False
    # Auth statuses reject the gateway's credential, not the user's request.
    return not _is_auth_misconfig(status)


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


def _fire_and_forget(coro: Any) -> bool:
    """Schedule an alert delivery without holding up the failing request.

    Keeps a strong reference for the task's lifetime (asyncio holds only weak
    ones) and closes the coroutine when there is no loop to run it on, so a sync
    caller — a unit test, or teardown — does not leak a never-awaited coroutine.

    Returns whether the delivery was actually scheduled, so a caller that set up
    state for the send (the breaker's in-flight guard) can roll it back.

    A *running* loop is required explicitly rather than inferred from
    ``ensure_future`` raising, because it does not raise whenever a loop object is
    merely current: it attaches the task to a loop nothing will ever run, and in
    the main thread ``asyncio.get_event_loop()`` will even create that loop
    (DeprecationWarning "There is no current event loop") instead of raising. The
    schedule then reports success while the delivery silently never happens, and
    the caller's rollback is skipped.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return False
    task = loop.create_task(coro)
    _ALERT_TASKS.add(task)
    task.add_done_callback(_ALERT_TASKS.discard)
    return True


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
        # Number of gateway-credential rejections since the last *accepted*
        # request. Non-zero means every request to this endpoint is being
        # refused for a reason no user can affect, which is what /health/deep
        # reports as degraded. Only a success clears it (see ``note_failure``).
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

    def note_failure(self, *, status: int | None, auth_misconfig: bool) -> int:
        """Record the failing status; return the credential-rejection run length.

        A failure never ends the run — only ``record(True)`` does, because only an
        accepted request is evidence that the credential works. No failure is:

        - One with no HTTP status never reached the upstream's auth layer at all,
          and that is the *expected* steady state of a real all-keys-rejected
          outage rather than an edge case: 401 is a key-specific status for
          ``KeyPool``, so the rejections mute every key and subsequent requests
          raise ``KeyPoolExhausted`` from ``acquire()`` before anything is sent.
          Connection resets and timeouts have the same shape.
        - One carrying some *other* status is no proof either: nothing here can
          tell an upstream that authenticated the request and then failed from a
          proxy or load balancer that answered before auth was ever evaluated.

        Treating either as recovery would announce "Recovered" in the middle of
        the outage — and for a muted key pool, precisely during its worst part. So
        this mirrors ``circuit_open``, whose only healthy edge is
        ``_CircuitBreaker.on_success``.
        """
        with self._lock:
            if status is not None:
                self.last_error_status = status
            if auth_misconfig:
                self.consecutive_auth_rejections += 1
            return self.consecutive_auth_rejections

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
        # ``time.monotonic()`` instant of the last *delivered* usage-limit page for
        # this endpoint (None = never), enforcing ``_MIN_ALERT_GAP_SEC`` between
        # them. Monotonic, not wall-clock: this only ever measures elapsed time and
        # is never rendered or compared against a provider timestamp, so it must
        # not be steppable by NTP — a backward step would otherwise mute plan pages
        # for the step plus four hours, with no escape short of a restart. None
        # rather than 0.0 because monotonic zero is host boot, so a 0.0 sentinel
        # would suppress the first page on any host up for less than four hours.
        # Deliberately NOT cleared by ``on_success``: it caps how often a plan
        # exhaustion may page, and is not state about the current outage, so a
        # recovery — including one served by a different key in the same pool —
        # must not hand back the right to page again immediately.
        self._usage_limit_alerted_at: float | None = None
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
            # deadline, and a later outage is free to page again. ``_usage_limit_
            # alerted_at`` is deliberately left alone — a plan-usage page still
            # owes the rest of its ``_MIN_ALERT_GAP_SEC``, precisely because a
            # single success is what a flapping key pool produces.
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
            # stay quiet until then, instead of re-paging every few minutes for
            # hours. The deadline is committed only once the page is delivered
            # (see _send_circuit_alert) and cleared on recovery, so a dropped page
            # never mutes an outage that was never announced.
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
            muted = in_flight_current or now_dt.timestamp() < self._alert_suppressed_until
            # A recovery clears that deadline, which is right for an endpoint that
            # came back — but any pooled key with quota left also reads as a
            # recovery, so a plan exhaustion behind a key pool can clear its own
            # mute and re-page on the next streak. Rate-limit the *plan usage* page
            # itself on a clock no recovery touches, so the cap holds however the
            # outage flaps. Scoped to usage-limit trips: an endpoint that breaks
            # for some other reason minutes later is a different incident that
            # still pages immediately.
            rate_limited = usage_limit is not None and (
                self._usage_limit_alerted_at is not None
                and (time.monotonic() - self._usage_limit_alerted_at) < _MIN_ALERT_GAP_SEC
            )
            # A deployment that has turned plan-usage paging off (see
            # ``_PAGE_ON_USAGE_LIMIT``) takes the same path as a held-back page:
            # silent, and with the mute deadline armed below so the re-trips that
            # carry no usage marker stay silent too.
            paging_off = usage_limit is not None and not _PAGE_ON_USAGE_LIMIT
            if muted or rate_limited or paging_off:
                if usage_limit is not None:
                    # We recognized a plan exhaustion and are holding its page
                    # back. Re-arm the reason-agnostic mute anyway, because the
                    # same outage keeps re-tripping in *other* shapes: once every
                    # pooled key sits in its 429 backoff, ``KeyPool.acquire``
                    # raises ``KeyPoolExhausted`` before a request is even sent,
                    # and that text carries no usage marker, so it is neither
                    # ``muted`` (a recovery cleared the deadline) nor
                    # ``rate_limited`` (it does not parse as a usage limit). Held
                    # back and un-muted, those probes would page at the 300s alert
                    # cooldown — worse than what this floor exists to fix. ``max``
                    # so a deadline already further out is never shortened.
                    self._alert_suppressed_until = max(
                        self._alert_suppressed_until,
                        usage_limit.suppress_until.timestamp(),
                    )
                logger.info(
                    "circuit_open_alert_suppressed",
                    extra={
                        "event": "circuit_open_alert_suppressed",
                        "provider": self.provider,
                        "reason": reason or "unknown",
                        # The mute deadline, named as such: it is what this
                        # endpoint is waiting on, not a provider quota claim (the
                        # alert card carries the same value under the same name).
                        "alert_muted_until": (
                            _iso_utc(self._alert_suppressed_until)
                            if self._alert_suppressed_until
                            else None
                        ),
                        "window": usage_limit.window if usage_limit is not None else "active",
                        # Which gate held: this outage's own mute, the floor
                        # under plan-usage pages that a recovery cannot reset, or
                        # plan-usage paging being off for this deployment.
                        "gate": (
                            "muted"
                            if muted
                            else "min_alert_gap"
                            if rate_limited
                            else "usage_limit_paging_off"
                        ),
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
            suppress_epoch: float | None = None
            if usage_limit is not None:
                suppress_epoch = usage_limit.suppress_until.timestamp()
                # Two different facts, so two fields: when the provider says its
                # quota turns over (omitted when it said nothing rather than
                # guessed at), and when this page can repeat.
                if usage_limit.reset_at is not None:
                    context["quota_reset_at"] = usage_limit.reset_at.isoformat()
                context["alert_muted_until"] = usage_limit.suppress_until.isoformat()
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
            if suppress_epoch is not None:
                self._alert_in_flight_generation = generation
            # Scheduling goes through the shared helper so that when there is no
            # running loop (sync caller / test) the unscheduled coroutine is closed
            # rather than surfacing later as a never-awaited RuntimeWarning.
            scheduled = _fire_and_forget(
                self._send_circuit_alert(context, suppress_epoch, generation)
            )
            # Nothing was scheduled, so release the in-flight guard we
            # optimistically set — unless a newer generation already claimed it.
            if not scheduled and self._alert_in_flight_generation == generation:
                self._alert_in_flight_generation = None

    async def _send_circuit_alert(
        self, context: dict[str, Any], suppress_epoch: float | None, generation: int
    ) -> bool:
        """Deliver a circuit-open page; commit usage-limit suppression on success.

        For a usage-limit trip (``suppress_epoch`` set) the suppression deadline is
        recorded only after the page is actually delivered, so a dropped page —
        relay/webhook failure, a global snooze, or the alert cooldown — leaves the
        outage un-muted and it re-pages on the next failing probe. The deadline is
        also withheld when the endpoint recovered while the page was in flight
        (``generation`` no longer current), so a stale mute can't silence a later
        outage. The in-flight guard is released in ``finally`` so a raising send
        never wedges the breaker muted.

        The plan-usage rate limiter is committed on the same delivered-page
        evidence but *without* the generation check: it caps how often this
        endpoint's plan exhaustion may page, and a page that went out went out
        whether or not the endpoint has recovered since.
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
            if suppress_epoch is not None:
                with self._lock:
                    # Only clear the guard if this page still owns it; after a
                    # recovery a newer generation's page may have claimed it.
                    if self._alert_in_flight_generation == generation:
                        self._alert_in_flight_generation = None
                    if delivered:
                        self._usage_limit_alerted_at = time.monotonic()
                        if generation == self._recovery_generation:
                            self._alert_suppressed_until = suppress_epoch
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
            # Reported after the accounting, like the failure path: the report only
            # logs and schedules, and the delivery it schedules must not run while
            # holding the lock every other endpoint's health accounting needs.
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
        if exc is not None and isinstance(exc, KeyPoolRoleRestricted):
            # The endpoint holds no key this caller's *tier* may spend, but it is
            # still serving the tiers that own those keys — nothing was even sent
            # upstream. Counting it would let a burst of lower-tier traffic open
            # the circuit and strip reserved capacity from the callers it was
            # reserved for, re-tripping on every half-open probe. Filtered here
            # rather than per-router so the FixedRouter, RouteWise and hedging
            # paths all inherit it.
            logger.info(
                "role_restricted_skip_breaker",
                extra={
                    "event": "role_restricted_skip_breaker",
                    "endpoint_id": endpoint_id,
                    "detail": _detail_str(detail or operator_safe_error(exc)),
                },
            )
            return
        status = _http_status_of(exc) if exc is not None else None
        # Checked before the client-error exemption: an auth rejection sits in the
        # 4xx range but is a deployment fault, so it must not be exempted.
        auth_misconfig = _is_auth_misconfig(status)
        if not auth_misconfig and exc is not None and _is_client_error(exc):
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
            auth_rejections = health.note_failure(status=status, auth_misconfig=auth_misconfig)
            availability = health.availability
            self._circuits[endpoint_id].on_failure(
                availability=availability,
                reason=_reason_str(reason),
                detail=safe_detail,
                offender=_offender_str(),
            )
        # No ``else`` branch: a failure of any other kind leaves both the rejection
        # run and any open incident alone (see ``note_failure``) — whatever is
        # failing now is the breaker's ``circuit_open`` incident to report.
        if auth_misconfig:
            # Reported after the accounting: the report only logs and schedules the
            # page, and the delivery it schedules must not run while holding the
            # lock every other endpoint's health accounting needs.
            self._report_auth_misconfig(
                endpoint_id,
                status=status,
                detail=safe_detail,
                consecutive=auth_rejections,
            )

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
        said no caller could make it do. Called only on the *first* accepted
        request after a rejection run, not on every success, so the resolution is
        emitted once per outage — and never on a failure, since no failure proves
        the credential was accepted (see ``_ProviderHealth.note_failure``).

        Ordering against the page it closes is the sink's job: ``alert_slack``
        publishes an in-flight marker per dedupe key before it awaits anything,
        and a resolution waits on that marker instead of racing past it. Getting
        that wrong would land this recovery *before* the page, leaving an incident
        open whose only healthy edge is already spent.

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
                # Never called: ``alert_on_transition`` builds its own context
                # for a resolution, so this only has to be a valid callable.
                context=lambda: {},
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
