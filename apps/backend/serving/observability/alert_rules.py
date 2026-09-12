"""AlertEngine for in-process Slack alerting.

Drains the AlertingLogHandler queue, runs rule-based alerts, and schedules
periodic SQL alerts via APScheduler.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import functools
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from serving.observability import alerts as _alerts
from serving.observability.alerts import (
    AlertSeverity,
    alert_on_transition,
    sweep_stale_breaches,
)
from serving.utils.context import MODEL_NOT_FOUND

#: A breach is only genuinely stale once its rule's window can no longer hold a
#: breaching sample. One extra window of slack, so an evaluation that lands late
#: does not race the sweep.
_STALE_WINDOW_FACTOR = 2

#: How many distinct offenders an auth alert names per dimension. Enough to act
#: on, short enough that a scanner wave does not turn the card into a wall of
#: text the reader scrolls past.
_OFFENDERS_IN_ALERT = 3

#: Longest request path rendered in an alert. A path is caller-chosen and
#: effectively unbounded, so a source probing a two-kilobyte URL would otherwise
#: push everything else in the message out of view.
_PATH_IN_ALERT_CHARS = 60

#: Distinct values an incident tally keeps per dimension. Past this a value not
#: already tracked is dropped rather than added: an incident stays open for as
#: long as the spike lasts, and a source rotating addresses must not be able to
#: grow the tally for that whole time. Same bound, and the same "(capped)"
#: honesty about the resulting counts, as ``routing/endpoint_health.py``.
_MAX_TRACKED_OFFENDERS = 50

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from serving.observability.alert_config import (
        AlertConfig,
        AuthIpBlockedConfig,
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

# 429 covers quota-exceeded and concurrency-limit rejections — expected user-facing
# rate limiting, not service failures, so excluded from the failure-rate alert.
#
# Neither 404 nor 401 is blanket-excluded, for the same reason in both cases: the
# gateway's own client-driven rejection and a relayed upstream failure share a
# status code, and only the second is a service failure.
#
# 404: an upstream provider can return 404 for a routed completion (bad provider
# model id / endpoint path), which the gateway re-raises — a genuine
# provider/config regression that must alert. Only the gateway's own
# model-not-found 404s (a user asking for an unknown/unauthorized model) are
# excluded, via the per-record marker checked in ``_is_failed_request``.
#
# 401: a gateway-issued auth challenge is normal SPA token-refresh churn (every
# auth failure stays visible in the ``auth_failure`` log records, and the per-IP
# blocklist refuses a repeat offender once it crosses that threshold — not on
# the first attempt; the auth_failure_spike rule that used to page is opt-in,
# see ``alert_config.AuthFailureSpikeConfig``), but an upstream
# 401 means the gateway's *own* configured credential was refused — a 100%-fatal,
# all-users outage. Only the former is excluded, via upstream attribution
# (``provider``) on the record. Blanket-excluding 401 is why a local endpoint
# rejecting the gateway's key for an hour never reached this rule.
_FAILED_REQUEST_IGNORED_STATUSES = frozenset({429})


def _is_failed_request(item: dict) -> bool:
    """Return True if a window item counts as a service-side failed request.

    Excludes the client-driven statuses in ``_FAILED_REQUEST_IGNORED_STATUSES``
    (429), gateway model-not-found 404s (a request for an unknown or unauthorized
    model — tagged via ``req_ctx.mark_model_not_found``), and gateway-issued 401
    auth challenges. Genuine upstream 404s and 401s carry provider attribution /
    no model-not-found tag and still count. This mirrors the DB-query alerter,
    which excludes the model-not-found error strings (not all 404s) from
    ``FAILURE_PREDICATE_SQL``.
    """
    status = item["status"]
    if status < 400:
        return False
    if status in _FAILED_REQUEST_IGNORED_STATUSES:
        return False
    if status == 401 and not item.get("provider"):
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
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
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
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
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
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


def _top_offenders(
    counts: collections.Counter[str],
    *,
    top: int = _OFFENDERS_IN_ALERT,
    capped: bool = False,
) -> str:
    """Render a counter as ``value (n), value (n), +N more``.

    ``capped`` marks a tally that stopped tracking new values, so the trailing
    count is a floor rather than a total and the reader is told not to size the
    incident from it.
    """
    if not counts:
        return "n/a"
    named = counts.most_common(top)
    parts = [f"{value} ({count})" for value, count in named]
    remaining = len(counts) - len(named)
    if remaining > 0:
        parts.append(f"+{remaining} more (capped)" if capped else f"+{remaining} more")
    return ", ".join(parts)


def _auth_failure_entry(record: logging.LogRecord) -> dict[str, Any]:
    """Pull the identifying fields off an ``auth_failure`` record.

    ``account`` is present only for the failures that have one: a key that
    resolves to a real account in a state that refuses it (revoked, expired,
    suspended), which ``servers/auth.py`` attaches under the same bounded
    enrichment budget the rejection log uses. Most auth failures are anonymous
    by construction — nobody was authenticated, which is the failure — so the
    field is absent far more often than not.

    Absent is *unresolved*, though, never proven anonymous, and the difference
    matters most exactly when it is least convenient: the lookup is shed under
    load, which is the spike being alerted on, and answers nothing on a timeout,
    a failed lookup, or with ``auth_failure_identify_caller`` off. A name here is
    evidence; no name is the absence of evidence, and nothing downstream may
    read it as evidence of absence.

    ``forwarded`` says the reported address did not come off the socket, which
    ``ip_source`` states outright. Decided per record rather than by comparing
    the window's reported and peer addresses: a window holding both a direct
    failure from A and a forwarded one from B through peer A has every peer
    address appearing as somebody's reported address, and the comparison then
    hides precisely the forged hop this is for.
    """
    path = getattr(record, "path", None)
    if isinstance(path, str) and len(path) > _PATH_IN_ALERT_CHARS:
        path = path[:_PATH_IN_ALERT_CHARS] + "…"
    user_id = getattr(record, "user_id", None)
    state = getattr(record, "credential_state", None)
    remote_ip = getattr(record, "remote_ip", None)
    peer_ip = getattr(record, "peer_ip", None)
    ip_source = getattr(record, "ip_source", None)
    # Falling back to the addresses differing covers an older record, or one
    # from a path that does not set ``ip_source``: the same fact, just inferred.
    forwarded = ip_source != "socket" if ip_source else bool(peer_ip) and peer_ip != remote_ip
    return {
        "remote_ip": remote_ip,
        "peer_ip": peer_ip,
        "forwarded": forwarded,
        "key_prefix": getattr(record, "key_prefix", None),
        "reason": getattr(record, "reason", None),
        "path": path,
        "account": (f"{user_id} ({state})" if state else str(user_id)) if user_id else None,
    }


def _auth_failure_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe who a set of auth failures came from.

    Shared by the breach card and the recovery card so both answer the same two
    questions — where from, and whose — rather than the recovery naming only
    the rule that closed.
    """
    counts: dict[str, collections.Counter[str]] = {
        field_: collections.Counter(e[field_] for e in entries if e.get(field_))
        for field_ in ("remote_ip", "key_prefix", "reason", "path", "account")
    }
    # Peers of the forwarded records only. Counting every peer and then
    # subtracting the reported addresses would drop the whole line for a window
    # that mixes direct and proxied failures through one socket.
    peers: collections.Counter[str] = collections.Counter(
        e["peer_ip"] for e in entries if e.get("forwarded") and e.get("peer_ip")
    )
    summary: dict[str, Any] = {
        "distinct_ips": len(counts["remote_ip"]),
        "top_ips": _top_offenders(counts["remote_ip"]),
        "top_key_prefixes": _top_offenders(counts["key_prefix"]),
        "failure_reasons": _top_offenders(counts["reason"]),
    }
    # Only when there is something to say. An anonymous scanner wave resolves no
    # account and hits one path, and a row reading "n/a" is worse than no row.
    if counts["account"]:
        summary["known_accounts"] = _top_offenders(counts["account"])
    if counts["path"]:
        summary["top_paths"] = _top_offenders(counts["path"])
    if peers:
        # Some of the addresses above did not come off the socket, so they are
        # only as trustworthy as the proxy that set them -- and a spoofed
        # ``X-Forwarded-For`` is exactly what a source does to spread its
        # failures across the blocklist's buckets. Naming the sockets they
        # actually arrived on is what makes that visible.
        summary["arrived_via_peers"] = _top_offenders(peers)
    return summary


@dataclass
class _AuthFailureIncident:
    """Running tally of one auth-failure incident, for its recovery card.

    The window the rule alerts on holds the last ``window_sec`` of failures,
    which is the right thing to breach on and the wrong thing to close on: by
    the time the incident resolves that window is empty, which is why the
    recovery card had nothing to say. This accumulates across the incident
    instead, and is reset when its summary is handed over.

    Counters are bounded (:data:`_MAX_TRACKED_OFFENDERS`): an incident lasts as
    long as the spike does, and a source rotating addresses would otherwise let
    it grow for that whole time.
    """

    ips: collections.Counter[str] = field(default_factory=collections.Counter)
    keys: collections.Counter[str] = field(default_factory=collections.Counter)
    reasons: collections.Counter[str] = field(default_factory=collections.Counter)
    accounts: collections.Counter[str] = field(default_factory=collections.Counter)
    total: int = 0
    peak_in_window: int = 0
    capped: bool = False
    opened_at: float | None = None
    last_seen: float | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def open(self, now: float) -> None:
        """Start an incident, or leave a running one running."""
        if self.opened_at is None:
            self.opened_at = now

    def observe(self, entries: list[dict[str, Any]], *, window_count: int, now: float) -> None:
        """Fold failures into the open incident. A no-op when none is open.

        Records arriving while the breach settles are folded in too: the
        in-window count dipping under the threshold is not the incident ending,
        and the addresses still arriving are the ones worth naming.
        """
        if self.opened_at is None:
            return
        self.last_seen = now
        self.peak_in_window = max(self.peak_in_window, window_count)
        for entry in entries:
            self.total += 1
            for counter, key in (
                (self.ips, entry.get("remote_ip")),
                (self.keys, entry.get("key_prefix")),
                (self.reasons, entry.get("reason")),
                (self.accounts, entry.get("account")),
            ):
                if not key:
                    continue
                if key not in counter and len(counter) >= _MAX_TRACKED_OFFENDERS:
                    self.capped = True
                    continue
                counter[key] += 1

    def summarize_and_reset(self, *, window_sec: int) -> dict[str, Any]:
        """Describe the closing incident and clear the tally.

        Empty when no incident was recorded — a resolution the rule never saw
        open (a restart mid-incident, a breach closed by the stale sweep before
        any record landed). Returning nothing leaves the recovery card as it was
        rather than padding it with zeroes that read like measurements.
        """
        if not self.is_open or not self.total:
            self.reset()
            return {}
        duration = int(max(0.0, (self.last_seen or 0.0) - (self.opened_at or 0.0)))
        summary: dict[str, Any] = {
            # Named for what it is: everything counted while the incident was
            # open, not a rate and not a reading of the window, which is empty
            # by now. "(capped)" says the per-dimension tallies stopped taking
            # new values, so the totals below them are floors.
            "failures_in_incident": f"{self.total} (capped)" if self.capped else str(self.total),
            "peak_in_window": f"{self.peak_in_window} per {window_sec}s",
            "incident_duration_sec": duration,
            "distinct_ips": len(self.ips),
            "top_ips": _top_offenders(self.ips, capped=self.capped),
            "top_key_prefixes": _top_offenders(self.keys, capped=self.capped),
            "failure_reasons": _top_offenders(self.reasons, capped=self.capped),
        }
        if self.accounts:
            summary["known_accounts"] = _top_offenders(self.accounts, capped=self.capped)
        self.reset()
        return summary

    def reset(self) -> None:
        for counter in (self.ips, self.keys, self.reasons, self.accounts):
            counter.clear()
        self.total = 0
        self.peak_in_window = 0
        self.capped = False
        self.opened_at = None
        self.last_seen = None


class AuthFailureSpikeRule:
    """Rule 4: auth-failure count over a sliding window.

    **Disabled by default** (``rules.auth_failure_spike.enabled``): a spike of
    bad keys is internet background noise, and the per-IP blocklist in
    ``utils/auth_failure_blocklist.py`` already refuses a repeat offender
    without paging anyone. Nothing here suppresses the ``auth_failure`` log
    records themselves — they are emitted at the auth sites in
    ``servers/auth.py``, independently of this rule, so a disabled rule costs
    an operator no evidence. A deployment that wants the page sets
    ``enabled: true`` in its alerts.yaml.

    When it does page, the card answers the two questions the operator opens it
    with — *where from* and *whose* — rather than a count they cannot act on.
    The breach card describes the window that breached; the recovery card
    describes the incident that just closed, via
    :class:`_AuthFailureIncident`, because the window it breached on is empty by
    the time it resolves and a bare "Recovered" names nothing to go and look at.
    """

    name = "auth_failure_spike"

    def __init__(self, cfg: CountRule) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)
        self._incident = _AuthFailureIncident()

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track auth-failure events and alert when the count exceeds threshold in-window."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "auth_failure":
            return
        now = time.time()
        entry = _auth_failure_entry(record)
        self._window.add(now, entry)
        items = self._window.items(now)
        breached = len(items) > self._cfg.threshold_count
        if breached and not self._incident.is_open:
            # Seeded from the whole breaching window rather than from the one
            # record that crossed the threshold: those failures *are* the
            # incident, and counting only what came after would report a total
            # smaller than the peak it also reports, from a card that named
            # none of the sources the breach was raised on.
            self._incident.open(now)
            arrived = items
        else:
            arrived = [entry]
        self._incident.observe(arrived, window_count=len(items), now=now)

        def breach_context() -> dict[str, Any]:
            return {
                "count": len(items),
                "window_sec": self._cfg.window_sec,
                **_auth_failure_summary(items),
            }

        def resolution_context() -> dict[str, Any]:
            return self._incident.summarize_and_reset(window_sec=self._cfg.window_sec)

        await alert_on_transition(
            key="auth_failure_spike",
            breached=breached,
            severity=AlertSeverity.WARN,
            title="Auth failure spike",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
            resolution_context=resolution_context,
        )


class AuthIpBlockedRule:
    """Alert when the auth-failure blocklist starts refusing a source.

    Fires on ``auth_ip_blocked``, which
    ``utils/auth_failure_blocklist.py`` emits once per blocking transition --
    not on the failures leading up to it, which is
    :class:`AuthFailureSpikeRule` and is off by default. One record means one
    bucket just began being refused in ``servers/auth.py``, ahead of any key
    lookup.

    One incident, not one per address: a per-bucket key would give every blocked
    source its own incident, and since a resolution bypasses the send cooldown
    (``alerts.alert_slack``), a wave would post a firing *and* a recovery per
    bucket.

    What that costs, and why it is the right trade: within ``cooldown_sec``
    only the *first* breach message is delivered, so the message names the
    block that opened the incident rather than a running total. The context
    therefore describes that one block and points at
    ``GET /admin/auth-blocks``, which is a live full list and strictly better
    than a snapshot Slack would have frozen. ``blocks_in_window`` is the count
    at the moment the message was built -- deliberately named so it cannot be
    read as a total for the wave.

    With the default ``threshold_count: 1`` a single block is already a breach;
    raising it pages only once a window holds that many. See
    :class:`AuthIpBlockedConfig`.
    """

    name = "auth_ip_blocked"

    def __init__(self, cfg: AuthIpBlockedConfig) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track blocking transitions and alert when the count reaches threshold in-window."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "auth_ip_blocked":
            return
        now = time.time()
        self._window.add(
            now,
            {
                "ip_bucket": getattr(record, "ip_bucket", None),
                "block_seconds": getattr(record, "block_seconds", None),
            },
        )
        items = self._window.items(now)

        def breach_context() -> dict[str, Any]:
            # The record being evaluated, not a top-N over the window: the
            # cooldown delivers only the first breach message, so an aggregate
            # built here would either be a total nobody receives or, worse, a
            # "1 of 12" that reads as the whole picture. The live list is one
            # request away, and the context says where.
            return {
                "ip_bucket": getattr(record, "ip_bucket", None) or "unknown",
                "block_seconds": getattr(record, "block_seconds", None) or "n/a",
                "blocks_in_window": len(items),
                "window_sec": self._cfg.window_sec,
                "all_active_blocks": "GET /admin/auth-blocks",
                # Spelled out because the remedy is counter-intuitive: the
                # block is consulted before the presented key is read, so
                # repairing a stale credential does not lift it.
                "remedy": (
                    "if this is a deployment-owned caller (monitor, CI job, service "
                    "account), repair its credential and then clear the block via "
                    "/admin/auth-blocks -- a corrected key does not lift it"
                ),
            }

        await alert_on_transition(
            key="auth_ip_blocked",
            # ``>=``, not ``>``: here threshold_count is "how many blocks are a
            # breach", and its default of 1 has to make a single block one.
            # AuthFailureSpikeRule reads its threshold the other way -- as how
            # much background noise to tolerate -- hence the strict ``>`` there.
            breached=len(items) >= self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="Auth-failure blocklist refusing a source",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
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
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
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
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


#: Attached only once :meth:`UserCostOverrunJob._hold_unobservable` has
#: *confirmed* the account no longer resolves. Claiming this of an account that
#: is in fact active — the case where an operator simply raised the cap — is a
#: false statement on an incident card, so the confirmation is not optional.
_NOTE_UNOBSERVABLE = (
    "user or key is no longer active; figures are the last observed "
    "today, held open until the UTC day rolls over"
)

#: The same hold, taken without confirmation because the re-check itself failed.
#: Deliberately says nothing about the account's status: that is precisely what
#: could not be established this sweep.
_NOTE_UNCONFIRMED = (
    "this account could not be re-checked this sweep; figures are the last "
    "observed today and may be out of date"
)


def _quota_card(
    user_id: str,
    role: str,
    spend: float,
    quota_usd: float,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the alert context for one account that has consumed its quota."""
    # Clamped at zero rather than signed. The predicate that selects these rows
    # includes the gate's optimistic pre-charge, so an account is already being
    # refused at $19.99 against a $20.00 cap — and rendering that as
    # "over_by: $-0.01" on a card titled "quota consumed" reads as a
    # contradiction rather than as the boundary case it is. At or below the cap
    # the honest word is "at cap".
    over = spend - quota_usd
    card = {
        "user_id": user_id,
        "role": role,
        "spend": f"${spend:.2f}",
        # The account's own cap, not a role threshold — two users with the same
        # role routinely have different ones, so the card has to carry the
        # number that was actually enforced on this one.
        "quota": f"${quota_usd:.2f}",
        "over_by": f"${over:.2f}" if over > 0 else "$0.00 (at cap)",
    }
    if note:
        card["note"] = note
    return card


class UserCostOverrunJob:
    """Periodic job 8: users who have consumed their daily cost quota.

    Reports accounts the quota gate has started refusing — spend measured
    against ``api_keys.quota_daily_cost_usd``, the number actually enforced.
    It used to compare spend against ``thresholds_per_role`` from
    ``alerts.yaml``, which could not fire in either direction: the gate caps
    spend at the *key's* limit, so a role threshold above that limit is
    unreachable, and one below it names users nothing ever refused. Caps vary
    per key even within one role (pro keys sit at 20/40/80/160/200 here), so no
    single per-role number could have been right for them anyway.
    """

    name = "user_cost_overrun"

    def __init__(self, cfg: UserOverrun, op_store: Any) -> None:
        self._cfg = cfg
        self._op_store = op_store
        # Users already reported today, and the figures they were reported
        # with. See ``_hold_unobservable`` for the one narrow case in which a
        # user who leaves the result set mid-day keeps being asserted.
        self._latched_day: str | None = None
        self._latched: dict[str, tuple[str, float, float]] = {}

    async def run(self) -> None:
        """Alert once per user per UTC day on accounts that hit their own cap."""
        if not self._cfg.enabled or self._op_store is None:
            return
        try:
            rows = await self._op_store.query_users_at_daily_quota()
        except Exception:
            log.exception("user_cost_overrun query failed")
            return

        # UTC, not ``date.today()``: the counter this reads rolls at UTC
        # midnight, and the requirement is one alert per user per UTC day. On a
        # host in any other zone the local date would split or merge days
        # against the data, dropping or doubling an alert at the seam.
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if self._latched_day != today:
            self._latched_day = today
            self._latched = {}

        current = {user_id: (role, spend, quota) for user_id, role, spend, quota in rows}

        departed = {
            user_id: value for user_id, value in self._latched.items() if user_id not in current
        }
        held = await self._hold_unobservable(departed)
        self._latched.update(current)

        # (role, spend, quota, note) for every card this sweep asserts. Fresh
        # rows carry no note; held ones carry the reason they are being held.
        cards: dict[str, tuple[str, float, float, str | None]] = {
            user_id: (role, spend, quota_usd, None)
            for user_id, (role, spend, quota_usd) in current.items()
        }
        cards.update(held)

        for user_id, (role, spend, quota_usd, note) in cards.items():
            # Through the tracker, not straight to the sink: the key embeds the
            # day, so once it rolls over nothing observes this one again and the
            # stale sweep is the only thing that can close its incident. Calling
            # alert_slack directly would leave it open forever.
            await _alerts.alert_on_transition(
                key=f"cost_overrun:{user_id}:{today}",
                breached=True,
                severity=AlertSeverity.WARN,
                title=f"Daily cost quota consumed by {user_id}",
                # ``partial`` rather than a lambda over loop variables: the
                # callable outlives this iteration, and binding by argument is
                # what keeps every card holding its own user's numbers.
                context=functools.partial(_quota_card, user_id, role, spend, quota_usd, note),
                cooldown_sec=self._cfg.cooldown_sec,
                stale_after=self._cfg.check_interval_sec * _STALE_WINDOW_FACTOR,
            )

    async def _hold_unobservable(
        self,
        departed: dict[str, tuple[str, float, float]],
    ) -> dict[str, tuple[str, float, float, str]]:
        """Decide, per departed user, whether the incident ended or went blind.

        A user reported earlier today who is no longer in the result set left
        for one of two reasons, and they need opposite handling:

        * **They recovered.** Overwhelmingly the common case, and usually a
          direct response to *this alert*: an operator raises the cap, a routine
          edit visible in ``admin_audit_log``. (The counter resetting at UTC
          midnight lands here too, in the narrow window where the database has
          rolled the day and this host's clock has not yet cleared the latch.)
          Either way the incident is genuinely over and must be allowed to
          close.
        * **They became unobservable.** The user was suspended or their key
          revoked, so the enforcer resolves no cap for them at all — and this
          query, which joins exactly as the enforcer does, stops seeing them.
          Nothing will evaluate that incident again, so the stale sweep would
          post "Recovered (no recent samples)" for an account that is, if
          anything, in worse standing than when it was reported.

        Re-asserting *every* departure without distinguishing the two — the
        first version of this job — turned each remediation into repeat breach
        cards quoting a cap the operator had already raised, annotated "user or
        key is no longer active", which in that case is simply false. It also
        held the incident open until the day rolled instead of closing it a
        sweep or two after the fix.

        ``get_quota_context_for_user`` settles it directly and cheaply: it is
        the same active-user-plus-active-unexpired-key lookup the grant door
        resolves a cap through, one indexed row per departed user, and only for
        users already reported today (single digits in practice). Rows back
        means the account still resolves, so it recovered; only an empty answer
        justifies holding.

        Args:
            departed: Users latched earlier today that this sweep did not see,
                mapped to the figures they were last reported with.

        Returns:
            The subset to keep asserting, each with the note its card carries.
            Recovered users are dropped from the latch as a side effect.
        """
        held: dict[str, tuple[str, float, float, str]] = {}
        for user_id, (role, spend, quota_usd) in departed.items():
            try:
                still_resolves = bool(await self._op_store.get_quota_context_for_user(user_id))
            except Exception:
                # Cannot tell recovered from unobservable. Hold — a false
                # "Recovered" on a capped-out account is the worse card — but
                # say only that, never that the account is inactive.
                log.exception("user_cost_overrun could not re-check %s", user_id)
                held[user_id] = (role, spend, quota_usd, _NOTE_UNCONFIRMED)
                continue
            if still_resolves:
                # Recovered. Drop the latch and assert nothing, which lets the
                # incident close exactly where it closed before this job
                # latched at all: the stale sweep, roughly two check intervals
                # after the fix.
                del self._latched[user_id]
                continue
            held[user_id] = (role, spend, quota_usd, _NOTE_UNOBSERVABLE)
        return held


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
                stale_after=self._cfg.check_interval_sec * _STALE_WINDOW_FACTOR,
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

    def _build_rules(self) -> None:
        self._rules.append(FailedRequestRateRule(self._config.rules.failed_request_rate))
        self._rules.append(FivexxRateRule(self._config.rules.fivexx_rate))
        self._rules.append(P95LatencyRule(self._config.rules.p95_latency_per_provider))
        self._rules.append(AuthFailureSpikeRule(self._config.rules.auth_failure_spike))
        self._rules.append(AuthIpBlockedRule(self._config.rules.auth_ip_blocked))
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
