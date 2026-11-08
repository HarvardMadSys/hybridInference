"""AlertEngine for in-process Slack alerting.

Drains the AlertingLogHandler queue, runs rule-based alerts, and schedules
periodic SQL alerts via APScheduler.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import functools
import heapq
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
from serving.utils.context import MODEL_NOT_FOUND, MODEL_QUEUED

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

#: Most samples one sliding window holds. A window is otherwise sized by its
#: traffic, and the request-log rules see every request -- an anonymous one to a
#: path that does not exist included -- so a flood would grow an hour's window
#: without bound. Past this the oldest sample leaves early: a rule then judges
#: the latest samples, and a rate's card says so. Above the 180,000 an hour
#: holds at 50 requests a second; at roughly 100 bytes a sample, a few tens of
#: megabytes per window at most.
_MAX_WINDOW_SAMPLES = 250_000

#: Distinct request paths ``failed_request_rate`` tallies for its card. A path
#: is caller-chosen, so a scan of random URLs would otherwise make the tally --
#: and the card, built from it on every breached record -- as large as the
#: window. A failure on a path past the cap still counts toward the rate; it is
#: only left out of ``top_paths``, which says how many were.
_MAX_TRACKED_PATHS = 256

#: Width of the blocks the latency rule groups durations into, as a power of two
#: (1024 ms). Selecting a percentile walks the occupied blocks and then one
#: block's distinct durations, so this trades one walk against the other;
#: neither depends on how many samples the window holds, and the result is exact.
_LATENCY_BLOCK_BITS = 10

#: Records the engine processes before it yields the event loop. Taking a queued
#: record does not suspend, so a backlog would otherwise drain in one stretch
#: with every request on this loop waiting behind it.
_DRAIN_YIELD_EVERY = 100

if TYPE_CHECKING:
    from collections.abc import Callable

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from serving.observability.alert_config import (
        AlertConfig,
        AuthIpBlockedConfig,
        ClientErrorBurstConfig,
        CountRule,
        KeyPoolExhaustionBurstConfig,
        LatencyRule,
        PendingPrefixCacheLeakConfig,
        ProviderHourlySpend,
        RateRule,
        RequestLogQueueSaturationConfig,
        StreamFailureRateConfig,
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
# Any other 4xx counts only when an upstream returned it: the request was routed
# and the error path published the upstream's ``provider`` onto the record. The
# gateway's own 4xx is a verdict on the request -- an unrouted path's 404 or
# 405, a body over the limit (413), one that does not validate (422), a
# gateway-issued 401 -- and anyone can send those anonymously. Counting them let
# a stream of requests for ``/v1/<anything>`` page the operator as a service
# failure every cooldown. A relayed upstream failure keeps counting, and that is
# what matters for both statuses that used to be special-cased:
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
# all-users outage. Blanket-excluding 401 is why a local endpoint rejecting the
# gateway's key for an hour never reached this rule.
#
# A 5xx counts whoever answered it: the gateway failing is a service failure.
_FAILED_REQUEST_IGNORED_STATUSES = frozenset({429})


def _is_failed_request(item: dict) -> bool:
    """Return True if a window item counts as a service-side failed request.

    A 5xx counts, except an on-demand upstream's "model queued" 503
    (``req_ctx.mark_model_queued``). A 4xx counts only with upstream
    attribution (``provider``), except the client-driven statuses in
    ``_FAILED_REQUEST_IGNORED_STATUSES`` (429) and gateway model-not-found
    404s (``req_ctx.mark_model_not_found``): the gateway's own 4xx -- an
    unrouted path, an oversized or invalid body, an auth challenge -- is a
    verdict on the request, not a failure of the service. This mirrors the
    DB-query alerter, which excludes the model-not-found error strings (not all
    404s) from ``FAILURE_PREDICATE_SQL``.
    """
    status = item["status"]
    if status < 400:
        return False
    if status in _FAILED_REQUEST_IGNORED_STATUSES:
        return False
    # An on-demand upstream's "model queued" 503: the model is waiting for
    # capacity, nothing failed (``serving.exceptions.UpstreamModelQueued``).
    if item.get("client_error_kind") == MODEL_QUEUED:
        return False
    if status >= 500:
        return True
    if not item.get("provider"):
        return False
    return not (status == 404 and item.get("client_error_kind") == MODEL_NOT_FOUND)


class _Rule(Protocol):
    name: str

    async def on_record(self, record: logging.LogRecord) -> None: ...


class _SlidingWindow:
    """Samples from the last ``window_sec`` seconds, oldest first.

    ``add`` and ``count`` are O(1) amortized -- each sample is appended once and
    evicted once -- so a rule can consult its window on every record. A rule
    that aggregates its samples keeps running totals instead of rescanning
    them: ``on_evict`` receives each sample's value as it leaves. ``items``
    copies the whole window, so it belongs in a breach card, which is built only
    for a breached evaluation, and never on the per-record path.

    At most ``max_samples`` are held (:data:`_MAX_WINDOW_SAMPLES` by default);
    past that the oldest leaves early, and :meth:`capped` says so.
    """

    def __init__(
        self,
        window_sec: int,
        on_evict: Callable[[Any], None] | None = None,
        *,
        max_samples: int | None = None,
    ) -> None:
        self.window_sec = window_sec
        self.max_samples = _MAX_WINDOW_SAMPLES if max_samples is None else max_samples
        self._items: collections.deque[tuple[float, Any]] = collections.deque()
        self._on_evict = on_evict
        # When the latest sample the cap (rather than its age) removed was taken.
        self._capped_at: float | None = None

    def make_room(self, ts: float) -> None:
        """Evict what a sample taken at ``ts`` would push out, before it arrives.

        A caller that admits the sample into bounded state first (a
        :class:`_Tally` with ``max_keys``) needs the departures counted before
        the arrival. Evicting by age before the append rather than after leaves
        the window exactly as it was.
        """
        self._evict(ts)
        while self._items and len(self._items) >= self.max_samples:
            self._capped_at, evicted = self._items.popleft()
            if self._on_evict is not None:
                self._on_evict(evicted)

    def add(self, ts: float, value: Any) -> None:
        self.make_room(ts)
        self._items.append((ts, value))
        # A no-op unless ``window_sec`` is negative, which drops even the new
        # sample, as appending and then evicting always did.
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        items = self._items
        while items and items[0][0] < cutoff:
            _, value = items.popleft()
            if self._on_evict is not None:
                self._on_evict(value)

    def count(self, now: float) -> int:
        self._evict(now)
        return len(self._items)

    def capped(self, now: float) -> bool:
        """Whether the cap removed a sample that is still inside the window by age."""
        return self._capped_at is not None and self._capped_at >= now - self.window_sec

    def items(self, now: float) -> list[Any]:
        self._evict(now)
        return [v for _, v in self._items]

    def is_empty(self, now: float) -> bool:
        return self.count(now) == 0


def _cap_note(window: _SlidingWindow, now: float) -> str:
    """Say that the sample cap, not the clock, bounded a rate's window."""
    if window.capped(now):
        return f"; only the latest {window.max_samples} samples are kept"
    return ""


#: What :meth:`_Tally.add` hands back for a key it did not admit.
_UNTRACKED = object()


class _Tally:
    """Per-key sample counts kept in step with a window, ranked as a rescan ranks them.

    ``Counter(window).most_common(n)`` breaks a tie by which key appears first
    in the window. Each key here holds the arrival numbers of its samples,
    oldest first, so its oldest surviving arrival is exactly that, and
    :meth:`most_common` returns what a rescan would -- the same keys, counts and
    order -- for a pass over the distinct keys rather than over every sample.

    ``max_keys`` bounds a key space the caller does not control. A new key that
    arrives while that many are tracked is not admitted: its samples are
    counted in ``untracked`` instead, and a tracked key frees its slot when its
    last sample leaves the window.
    """

    __slots__ = ("_keys", "_max_keys", "untracked")

    def __init__(self, max_keys: int | None = None) -> None:
        self._keys: dict[Any, tuple[Any, collections.deque[int]]] = {}
        self._max_keys = max_keys
        self.untracked = 0

    def add(self, key: Any, arrival: int) -> Any:
        """Count one sample of ``key``; return the token that :meth:`discard` takes."""
        entry = self._keys.get(key)
        if entry is None:
            if self._max_keys is not None and len(self._keys) >= self._max_keys:
                self.untracked += 1
                return _UNTRACKED
            entry = self._keys[key] = (key, collections.deque())
        entry[1].append(arrival)
        # The key object already held, not the caller's equal copy: the samples
        # of one path then share one string, however long and however many.
        return entry[0]

    def discard(self, token: Any) -> None:
        """Forget the oldest sample counted under ``token``."""
        if token is _UNTRACKED:
            self.untracked -= 1
            return
        arrivals = self._keys[token][1]
        arrivals.popleft()
        if not arrivals:
            del self._keys[token]

    def __len__(self) -> int:
        return len(self._keys)

    def most_common(self, n: int) -> list[tuple[Any, int]]:
        """The ``n`` most frequent keys, a tie going to the key that appeared first."""
        ranked = heapq.nsmallest(n, self._keys.values(), key=_rank)
        return [(key, len(arrivals)) for key, arrivals in ranked]


def _rank(entry: tuple[Any, collections.deque[int]]) -> tuple[int, int]:
    return (-len(entry[1]), entry[1][0])


class _TalliedWindow:
    """A window whose marked samples are tallied, per dimension, as they come and go.

    A sample is *marked* when it carries keys -- a failed request with its
    status, path and provider -- and unmarked when it only counts toward the
    window's size, as a request that succeeded does. ``marked`` and every tally
    stay in step with the window, so a rule reads its rate, and its card's
    top-N, without rescanning anything.
    """

    def __init__(self, window_sec: int, *tallies: _Tally) -> None:
        self.window = _SlidingWindow(window_sec, self._forget)
        self.tallies = tallies
        self.marked = 0
        self._arrivals = 0

    def add(self, now: float, keys: tuple[Any, ...] | None) -> int:
        """Append a sample and return how many the window holds.

        ``keys`` is ``None`` for an unmarked sample; otherwise it holds one key
        per tally, ``None`` where the sample has nothing to count in that one.
        """
        tokens = None
        if keys is not None:
            # An unhashable key fails here, before anything is counted, rather
            # than partway through the tallies.
            hash(keys)
            # Departures first, so a capped tally admits against the window
            # this sample joins, not the one it pushes out.
            self.window.make_room(now)
            self._arrivals += 1
            tokens = tuple(
                None if key is None else tally.add(key, self._arrivals)
                for tally, key in zip(self.tallies, keys, strict=True)
            )
            self.marked += 1
        self.window.add(now, tokens)
        return self.window.count(now)

    def _forget(self, tokens: tuple[Any, ...] | None) -> None:
        if tokens is None:
            return
        self.marked -= 1
        for tally, token in zip(self.tallies, tokens, strict=True):
            if token is not None:
                tally.discard(token)


class _LatencyWindow:
    """One provider's request durations over the window, for :class:`P95LatencyRule`.

    Two running structures replace sorting the window on every record:

    * the samples at or above the threshold, counted as they come and go. They
      decide the breach exactly: the window's value at sorted index ``i``
      reaches the threshold precisely when no more than ``i`` samples fall
      below it.
    * exact counts per duration, grouped in blocks of
      ``2**_LATENCY_BLOCK_BITS`` ms, from which the card's p95 and p99 are
      selected exactly: walk the occupied blocks to the one holding the rank,
      then that block's distinct durations. Neither walk grows with the number
      of samples.
    """

    def __init__(self, window_sec: int) -> None:
        self.window = _SlidingWindow(window_sec, self._forget)
        self._blocks: dict[int, dict[int, int]] = {}
        self._block_sizes: dict[int, int] = {}
        self._threshold: Any = None
        self._at_or_above = 0

    def add(self, now: float, duration_ms: int) -> int:
        """Append a duration and return how many the window holds."""
        above = self._threshold is not None and duration_ms >= self._threshold
        block = duration_ms >> _LATENCY_BLOCK_BITS
        counts = self._blocks.setdefault(block, {})
        counts[duration_ms] = counts.get(duration_ms, 0) + 1
        self._block_sizes[block] = self._block_sizes.get(block, 0) + 1
        if above:
            self._at_or_above += 1
        self.window.add(now, duration_ms)
        return self.window.count(now)

    def _forget(self, duration_ms: int) -> None:
        if self._threshold is not None and duration_ms >= self._threshold:
            self._at_or_above -= 1
        block = duration_ms >> _LATENCY_BLOCK_BITS
        counts = self._blocks[block]
        if counts[duration_ms] > 1:
            counts[duration_ms] -= 1
        else:
            del counts[duration_ms]
        if self._block_sizes[block] > 1:
            self._block_sizes[block] -= 1
        else:
            del self._block_sizes[block]
            del self._blocks[block]

    def at_or_above(self, threshold: Any) -> int:
        """How many durations in the window are at or above ``threshold``."""
        if threshold != self._threshold:
            # Recounted only when the threshold in force changes, which for a
            # configuration loaded at startup is once per provider.
            at_or_above = sum(
                count
                for counts in self._blocks.values()
                for duration, count in counts.items()
                if duration >= threshold
            )
            self._threshold = threshold
            self._at_or_above = at_or_above
        return self._at_or_above

    def kth_smallest(self, k: int) -> int:
        """The duration at index ``k`` of the window sorted in ascending order."""
        for block in sorted(self._block_sizes):
            size = self._block_sizes[block]
            if k >= size:
                k -= size
                continue
            counts = self._blocks[block]
            for duration in sorted(counts):
                if k < counts[duration]:
                    return duration
                k -= counts[duration]
        raise IndexError(k)


class FailedRequestRateRule:
    """Rule 1: failed-request rate over a sliding window.

    A failure is a 5xx, or a 4xx an upstream returned (:func:`_is_failed_request`);
    every request with a status counts toward the total.

    The rate and the card's top-N come from tallies the window keeps in step as
    requests arrive and age out (:class:`_TalliedWindow`), so a record costs the
    same however many the window holds, and the card is only built for a
    breached evaluation.
    """

    name = "failed_request_rate"

    def __init__(self, cfg: RateRule) -> None:
        self._cfg = cfg
        self._statuses = _Tally()
        self._paths = _Tally(max_keys=_MAX_TRACKED_PATHS)
        self._providers = _Tally()
        self._window = _TalliedWindow(cfg.window_sec, self._statuses, self._paths, self._providers)

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
        item = {
            "status": int(status),
            "provider": getattr(record, "provider", None),
            "path": getattr(record, "path", None),
            "client_error_kind": getattr(record, "client_error_kind", None),
        }
        # Only a failure is tallied, by what the card names: its status, and its
        # path and provider when it has them.
        keys = (
            (item["status"], item["path"] or None, item["provider"] or None)
            if _is_failed_request(item)
            else None
        )
        samples = self._window.add(now, keys)
        if samples < self._cfg.min_samples:
            return
        failed = self._window.marked
        pct = (failed / samples) * 100.0

        def breach_context() -> dict[str, Any]:
            top_s = ", ".join(f"{s} ({c})" for s, c in self._statuses.most_common(3))
            top_paths = ", ".join(f"{p} ({c})" for p, c in self._paths.most_common(3))
            if self._paths.untracked:
                # A tally that stopped admitting paths says so, rather than pass
                # for the whole picture.
                note = f"+{self._paths.untracked} on other paths (capped)"
                top_paths = f"{top_paths}, {note}" if top_paths else note
            top_p = ", ".join(f"{p} ({c})" for p, c in self._providers.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {samples} requests, "
                    f"last {self._cfg.window_sec}s{_cap_note(self._window.window, now)})"
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
    """Rule 2: 5xx rate over a sliding window, from running tallies like rule 1."""

    name = "fivexx_rate"

    def __init__(self, cfg: RateRule) -> None:
        self._cfg = cfg
        self._providers = _Tally()
        self._statuses = _Tally()
        self._window = _TalliedWindow(cfg.window_sec, self._providers, self._statuses)

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
        status_code = int(status)
        provider = getattr(record, "provider", None)
        keys = (provider or None, status_code) if status_code >= 500 else None
        samples = self._window.add(now, keys)
        if samples < self._cfg.min_samples:
            return
        failed = self._window.marked
        pct = (failed / samples) * 100.0

        def breach_context() -> dict[str, Any]:
            top_p = ", ".join(f"{p} ({c})" for p, c in self._providers.most_common(3))
            top_s = ", ".join(f"{s} ({c})" for s, c in self._statuses.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {samples} requests, "
                    f"last {self._cfg.window_sec}s{_cap_note(self._window.window, now)})"
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
    """Rule 3: per-provider p95 latency over a sliding window.

    Exact, without sorting the window per record: see :class:`_LatencyWindow`.
    """

    name = "p95_latency_per_provider"

    def __init__(self, cfg: LatencyRule) -> None:
        self._cfg = cfg
        self._windows: dict[str, _LatencyWindow] = {}

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
        win = self._windows.get(provider)
        if win is None:
            win = self._windows[provider] = _LatencyWindow(self._cfg.window_sec)
        now = time.time()
        samples = win.add(now, int(duration_ms))
        if samples < self._cfg.min_samples:
            return
        idx = int(0.95 * (samples - 1))
        threshold = self._cfg.overrides.get(provider, {}).get(
            "threshold_ms", self._cfg.threshold_ms
        )
        # p95 >= threshold, without sorting: the sorted window's value at idx
        # reaches the threshold exactly when no more than idx samples are below.
        breached = samples - win.at_or_above(threshold) <= idx

        def breach_context() -> dict[str, Any]:
            context: dict[str, Any] = {
                "provider": provider,
                "p95_ms": win.kth_smallest(idx),
                "p99_ms": win.kth_smallest(int(0.99 * (samples - 1))),
                "samples": samples,
                "window_sec": self._cfg.window_sec,
                "threshold_ms": threshold,
            }
            if win.window.capped(now):
                context["note"] = (
                    f"only the latest {win.window.max_samples} samples in the window are kept"
                )
            return context

        await alert_on_transition(
            key=f"p95_latency:{provider}",
            breached=breached,
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
    counts: collections.Counter[str] | _Tally,
    *,
    top: int = _OFFENDERS_IN_ALERT,
    capped: bool = False,
) -> str:
    """Render a counter, or a window's running tally, as ``value (n), value (n), +N more``.

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
        count = self._window.count(now)
        breached = count > self._cfg.threshold_count
        if breached and not self._incident.is_open:
            # Seeded from the whole breaching window rather than from the one
            # record that crossed the threshold: those failures *are* the
            # incident, and counting only what came after would report a total
            # smaller than the peak it also reports, from a card that named
            # none of the sources the breach was raised on.
            self._incident.open(now)
            arrived = self._window.items(now)
        else:
            arrived = [entry]
        self._incident.observe(arrived, window_count=count, now=now)

        def breach_context() -> dict[str, Any]:
            # The only full pass over the window. Deferred (below), so it runs
            # when a breach is actually sent or opens a history episode, not on
            # every failure while one is breached: a source that keeps failing
            # fills this window, and paid for a pass over it per failure.
            return {
                "count": count,
                "window_sec": self._cfg.window_sec,
                **_auth_failure_summary(self._window.items(now)),
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
            defer_context=True,
        )


class AuthIpBlockedRule:
    """Alert when the auth-failure blocklist starts refusing a source.

    Fires on ``auth_ip_blocked`` for resolved sources and the coarse unresolved
    bucket, which uses the ``unresolved-global`` value in ``ip_bucket``;
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

    No recovery is ever announced (``announce_resolution=False``), because this
    rule only ever observes *transitions* and its silence is therefore
    guaranteed rather than informative. ``utils/auth_failure_blocklist.py``
    emits one ``auth_ip_blocked`` record per new block and then returns early
    for a bucket that is already blocked ("Already blocked ... Not a fresh
    transition"), while the block itself stands for
    ``auth_failure_block_duration_sec`` -- 86400s by default, far longer than
    this rule's staleness bound of ``window_sec * _STALE_WINDOW_FACTOR`` (600s).
    So the records stop long before the block does, and every close this rule
    can produce -- the settling edge as much as the sweep's "Recovered (no
    recent samples)" card -- is an artifact of the rule going quiet, never
    evidence that the source was unblocked. Posting it is pure noise on top of
    reading as though the block had lifted, which is the opposite of the truth
    while the source is still being refused. The incident still closes silently
    in the tracker, so the key does not stay firing forever.
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
        count = self._window.count(now)

        def breach_context() -> dict[str, Any]:
            # The record being evaluated, not a top-N over the window: the
            # cooldown delivers only the first breach message, so an aggregate
            # built here would either be a total nobody receives or, worse, a
            # "1 of 12" that reads as the whole picture. The live list is one
            # request away, and the context says where.
            unresolved_global = getattr(record, "ip_bucket", None) == "unresolved-global"
            return {
                "ip_bucket": getattr(record, "ip_bucket", None) or "unknown",
                "block_seconds": getattr(record, "block_seconds", None) or "n/a",
                "blocks_in_window": count,
                "window_sec": self._cfg.window_sec,
                "all_active_blocks": (
                    "process-local unresolved block; not listed by GET /admin/auth-blocks"
                    if unresolved_global
                    else "GET /admin/auth-blocks"
                ),
                # Spelled out because the remedy is counter-intuitive: the
                # block is consulted before the presented key is read, so
                # repairing a stale credential does not lift it.
                "remedy": (
                    "inspect client-IP resolution and trusted-proxy configuration; "
                    "this process-local unresolved block expires automatically"
                    if unresolved_global
                    else (
                        "if this is a deployment-owned caller (monitor, CI job, service "
                        "account), repair its credential and then clear the block via "
                        "/admin/auth-blocks -- a corrected key does not lift it"
                    )
                ),
            }

        await alert_on_transition(
            key="auth_ip_blocked",
            # ``>=``, not ``>``: here threshold_count is "how many blocks are a
            # breach", and its default of 1 has to make a single block one.
            # AuthFailureSpikeRule reads its threshold the other way -- as how
            # much background noise to tolerate -- hence the strict ``>`` there.
            breached=count >= self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="Auth-failure blocklist refusing a source",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
            # Close the incident, announce nothing: the records stop when the
            # blocking transitions stop, not when the block lifts. See the
            # class docstring.
            announce_resolution=False,
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
        count = self._window.count(now)

        def breach_context() -> dict[str, Any]:
            # A pass over the window, made only for a breached evaluation.
            items = self._window.items(now)
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
                "evicted_count": count,
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
            breached=count > self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="RouteWise pending prefix-cache entries leaking",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class RequestLogQueueSaturationRule:
    """Alert when the bounded request-log queue discards report rows.

    Reads the ``request_log_queue_saturated`` warning emitted by
    ``CompletionsLogger.schedule_log`` when the fixed worker pool falls behind
    and drop-oldest evicts an ``api_logs`` row. The producer emits the warning
    on the first loss, every 100th, and at least once per 60 seconds while
    drops continue, so this rule receives a bounded event stream rather than
    one record per dropped payload; its sliding window and ``cooldown_sec``
    turn a sustained stall into a rate-limited page.

    This is the alerting surface the ``request_log`` tracked-task failure rate
    cannot provide: a dropped row never becomes a failed write, and the queue's
    cumulative ``dropped_count`` is process-local. The record carries only the
    cumulative count and the queue bound, never the payload or request text.
    A drop is report-data loss; quota increments are separately scheduled
    background tasks, not rows on this queue. The context says both so the page
    is not read as billing-ledger corruption.
    """

    name = "request_log_queue_saturation"

    def __init__(self, cfg: RequestLogQueueSaturationConfig) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track loss warnings and alert when the window reaches the threshold."""
        if not self._cfg.enabled:
            return
        # Structured attribute, not message text, for the reason documented on
        # ClientErrorBurstRule: this engine's root handler also sees its own
        # log output, and none of those records carries an ``event``.
        if getattr(record, "event", None) != "request_log_queue_saturated":
            return
        now = time.time()
        self._window.add(
            now,
            {
                "dropped_total": getattr(record, "dropped_total", None),
                "queue_maxsize": getattr(record, "queue_maxsize", None),
            },
        )
        count = self._window.count(now)

        def breach_context() -> dict[str, Any]:
            # A pass over the window, made only for a breached evaluation.
            items = self._window.items(now)
            dropped_totals = [
                item["dropped_total"] for item in items if isinstance(item["dropped_total"], int)
            ]
            bounds = [
                item["queue_maxsize"] for item in items if isinstance(item["queue_maxsize"], int)
            ]
            return {
                "events_in_window": count,
                "dropped_total": max(dropped_totals) if dropped_totals else "unknown",
                "queue_maxsize": max(bounds) if bounds else "unknown",
                "window_sec": self._cfg.window_sec,
                "note": (
                    "api_logs report rows were discarded. Quota-authoritative "
                    "increments are separate background tasks, not rows on this "
                    "queue, so this eviction did not drop them; usage and "
                    "task/grant cost reports that read api_logs undercount "
                    "these rows"
                ),
            }

        await alert_on_transition(
            key=self.name,
            # ``>=``: unlike the burst rules that tolerate background noise,
            # one discarded report row is already a loss. threshold_count is
            # the number of rate-limited warnings that constitute a breach.
            breached=count >= self._cfg.threshold_count,
            severity=AlertSeverity.WARN,
            title="Request-log queue saturated; report rows dropped",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            # Its own window, not the longest rule's: a 60-second rule whose
            # traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
            # Warnings only exist while rows are being dropped, so their
            # absence is not evidence the queue recovered; close the incident
            # silently rather than posting a recovery the rule never measured.
            announce_resolution=False,
        )


class TrackedTaskFailureRateRule:
    """Alert when a tracked task type fails at a sustained rate.

    Reads ``tracked_task_completed`` log records emitted by the
    ``serving.observability.tracked_tasks.tracked_task`` helper and
    keeps a per-``task_name`` sliding window. Fires once the failure
    rate over the window exceeds ``threshold_pct`` and at least
    ``min_samples`` completions are observed.

    Several of those tasks run once per request (``key_last_used``,
    ``cost_increment``), so the failures are a running count kept in step with
    the window rather than a rescan of it per record.
    """

    name = "tracked_task_failure_rate"

    def __init__(self, cfg: TrackedTaskFailureRateConfig) -> None:
        self._cfg = cfg
        self._windows: dict[str, _TalliedWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Update the per-task-name window from a tracked_task_completed event."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "tracked_task_completed":
            return
        task_name = getattr(record, "task_name", None) or "unknown"
        success = bool(getattr(record, "success", True))
        win = self._windows.get(task_name)
        if win is None:
            win = self._windows[task_name] = _TalliedWindow(self._cfg.window_sec)
        now = time.time()
        # A failure is a marked sample with nothing further to tally.
        samples = win.add(now, None if success else ())
        if samples < self._cfg.min_samples:
            return
        failed = win.marked
        pct = (failed / samples) * 100.0

        def breach_context() -> dict[str, Any]:
            return {
                "task_name": task_name,
                "rate": (
                    f"{pct:.1f}% ({failed} of {samples} tasks, "
                    f"last {self._cfg.window_sec}s{_cap_note(win.window, now)})"
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


class ClientErrorBurstRule:
    """Alert when relayed client errors that bypass the circuit breaker pile up.

    Fires when more than ``threshold_count`` ``client_error_skip_breaker``
    events arrive within ``window_sec``. ``EndpointHealth.record_failure``
    (``routing/endpoint_health.py``) emits that event on its way *past* the
    breaker: a 4xx other than 408/429 says the request was malformed, not that
    the endpoint is sick, so counting it against the breaker would trip a
    healthy endpoint on one bad caller.

    That exemption is correct, and it is precisely why this rule is needed --
    a user-visible 400 storm registers as nothing anywhere else. ``fivexx_rate``
    cannot see it (not a 5xx), ``failed_request_rate`` cannot reach its
    percentage threshold on the volume one wedged conversation produces, and
    ``circuit_open`` is by design never reached. One client replaying a poisoned
    historical tool call produced 306 failures over 14 days here without a
    single page.

    **No upstream ``detail`` on the card.** The record carries one, and it is
    the most tempting field on it, but ``_detail_str`` only truncates and
    normalizes whitespace -- it does not redact, and a relayed provider error
    routinely quotes the offending request back (a tool call's arguments, a
    message body). The endpoint and status are what an operator acts on; the
    text is one log query away and stays out of Slack.

    **Distinct callers.** Each event carries an opaque ``caller`` (one per API
    key, grant or anonymous address; ``unknown`` where none was set). A breach
    also needs ``min_distinct_callers`` of them in the window, so a deployment
    can refuse to let one key page it; the card says how many there were.
    """

    name = "client_error_burst"

    def __init__(self, cfg: ClientErrorBurstConfig) -> None:
        self._cfg = cfg
        # Tallied as they come and go: a caller replaying a malformed request
        # drives this window, and a card rebuilt from a rescan on every
        # breached record would cost that caller nothing and the loop plenty.
        self._endpoints = _Tally()
        self._statuses = _Tally()
        # Callers are chosen by whoever sends the requests, so their tally is
        # bounded; past the bound the count is a floor, which only ever makes
        # more callers than any sensible minimum.
        self._callers = _Tally(max_keys=_MAX_TRACKED_OFFENDERS)
        self._window = _TalliedWindow(
            cfg.window_sec, self._endpoints, self._statuses, self._callers
        )

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track breaker-exempt client errors and alert when they burst in-window."""
        if not self._cfg.enabled:
            return
        # Selects on the structured attribute, never on the formatted message.
        # The alerting handler is installed on the *root* logger
        # (``servers/bootstrap.py``), so every record this module and
        # ``observability/alerts.py`` emit is fed straight back through this
        # method. None of them attaches an ``extra``, so none carries an
        # ``event`` attribute at all -- which is what makes a feedback loop
        # impossible here rather than merely unlikely. A substring match on
        # ``record.getMessage()`` would not have that property: the alert path
        # logs failures that quote rule names and keys.
        if getattr(record, "event", None) != "client_error_skip_breaker":
            return
        now = time.time()
        endpoint_id = getattr(record, "endpoint_id", None)
        status = getattr(record, "status", None)
        caller = getattr(record, "caller", None)
        count = self._window.add(
            now,
            (
                str(endpoint_id) if endpoint_id else None,
                None if status is None else str(status),
                str(caller) if caller else "unknown",
            ),
        )
        callers = len(self._callers)
        callers_capped = self._callers.untracked > 0

        def breach_context() -> dict[str, Any]:
            return {
                "count": count,
                "window_sec": self._cfg.window_sec,
                "distinct_callers": f"{callers}+ (capped)" if callers_capped else callers,
                "top_endpoints": _top_offenders(self._endpoints),
                "top_status_codes": _top_offenders(self._statuses),
                # Spelled out because the absence of a circuit-open page next to
                # this one is the expected behaviour, not a second fault: these
                # requests were routed past the breaker on purpose.
                "note": (
                    "these are client errors relayed from upstream; they bypass the "
                    "circuit breaker by design, so no circuit-open alert will follow. "
                    "A single caller replaying one malformed request can produce all "
                    "of them -- check the error detail in the logs for "
                    "client_error_skip_breaker before suspecting the endpoint"
                ),
            }

        await alert_on_transition(
            key="client_error_burst",
            # Strict ``>``: threshold_count is how many are tolerated, as in
            # PendingPrefixCacheLeakRule and AuthFailureSpikeRule. (AuthIpBlocked
            # reads its threshold the other way only because a default of 1 has
            # to make one block a breach.)
            breached=(
                count > self._cfg.threshold_count
                and (callers_capped or callers >= self._cfg.min_distinct_callers)
            ),
            # WARN, not ERROR: every request counted here was refused for being
            # malformed, which may be entirely the caller's doing. It needs a
            # human to look; it is not yet evidence the gateway is broken.
            severity=AlertSeverity.WARN,
            title="Client-error burst relayed from upstream",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            # Its own window, not the longest rule's: a 60-second rule
            # whose traffic stops should close on its own timescale.
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class KeyPoolExhaustionBurstRule:
    """Alert when one endpoint's key-pool refusals pile up past the breaker.

    Fires when more than ``threshold_count`` ``key_pool_exhausted_skip_breaker``
    events arrive for a single endpoint within ``window_sec``. The breaker
    exemption is intentional: exhaustion happens before an upstream dispatch, so
    it is not endpoint failure evidence. Provider-wide 402/403 incidents are also
    exempt as client errors, which makes this rule their alerting surface after
    the circuit no longer opens on the exhaustion echo.

    **Per endpoint**, in both the count and the incident key, because the page it
    stands in for -- ``circuit_open:<endpoint>`` -- was per endpoint. One shared
    window and key let an endpoint already in breach spend the cooldown for every
    other: a second endpoint whose account went dry minutes later joined the open
    incident and was not reported for up to an hour, and two quiet endpoints
    could add up to a breach neither had. Scoped to the ``endpoint_id`` the
    registry logs, not a provider or key-pool identity the record does not carry,
    so routes sharing one provider's keys page separately.
    """

    name = "key_pool_exhaustion_burst"

    def __init__(self, cfg: KeyPoolExhaustionBurstConfig) -> None:
        self._cfg = cfg
        self._windows: dict[str, _SlidingWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track breaker-exempt key-pool refusals per endpoint and alert on a burst."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "key_pool_exhausted_skip_breaker":
            return
        # ``record_failure`` always names the endpoint; the fallback only keeps a
        # malformed record from raising or from sharing a key with a real one.
        endpoint_id = str(getattr(record, "endpoint_id", None) or "unknown")
        now = time.time()
        self._drop_idle_windows(now)
        win = self._windows.get(endpoint_id)
        if win is None:
            win = self._windows[endpoint_id] = _SlidingWindow(self._cfg.window_sec)
        win.add(now, None)
        count = win.count(now)

        def breach_context() -> dict[str, Any]:
            return {
                "endpoint_id": endpoint_id,
                "count": count,
                "window_sec": self._cfg.window_sec,
                "note": (
                    "key-pool exhaustion happens before an upstream dispatch and bypasses "
                    "the circuit breaker by design. Provider-wide 402/403 incidents can "
                    "produce these refusals without a circuit-open alert; inspect "
                    "key-pool mutes and recent upstream statuses in logs. Endpoints "
                    "sharing these keys are counted and paged separately"
                ),
            }

        await alert_on_transition(
            key=f"key_pool_exhaustion_burst:{endpoint_id}",
            breached=count > self._cfg.threshold_count,
            severity=AlertSeverity.ERROR,
            title=f"Key-pool exhaustion burst on {endpoint_id}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )

    def _drop_idle_windows(self, now: float) -> None:
        """Forget endpoints with nothing left in their window.

        Routes are added and removed at runtime, so a window per endpoint ever
        seen would only grow. An empty one holds nothing a fresh window would
        not, and any incident it opened is closed by the next evaluation or by
        the stale sweep, which tracks the key, not this window.
        """
        idle = [key for key, win in self._windows.items() if win.is_empty(now)]
        for key in idle:
            del self._windows[key]


class StreamFailureRateRule:
    """Alert when one model's streams keep dying mid-flight.

    Fires when more than ``threshold_count`` ``stream_failed`` events arrive for
    a single model within ``window_sec``. ``servers/routers/completions_stream.py``
    emits that event from the ``except Exception`` handler that ends a streaming
    response after the client has already begun receiving it.

    A **count** per model, not a percentage -- see ``StreamFailureRateConfig``:
    that codepath counts nothing that finished, so there is no denominator to
    take a percentage of.

    A threshold this low is safe because aborts do not reach the emitting
    handler. A client disconnect and a ``TimeoutMiddleware`` deadline both
    surface as ``asyncio.CancelledError`` / ``GeneratorExit``; those derive from
    ``BaseException``, not ``Exception``, so they fall through to the
    ``_finalize_cancelled`` handler that follows and log nothing here. Every
    event this rule counts is a stream that failed on its own.
    """

    name = "stream_failure_rate"

    def __init__(self, cfg: StreamFailureRateConfig) -> None:
        self._cfg = cfg
        # Per model: the window, with the error types it holds tallied.
        self._windows: dict[str, _TalliedWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Track per-model stream failures and alert when they burst in-window."""
        if not self._cfg.enabled:
            return
        # Structured attribute, not a substring of the message, for the reason
        # given at length in ClientErrorBurstRule.on_record: the alert engine's
        # handler sees its own log output, and no record it emits carries an
        # ``event`` attribute.
        if getattr(record, "event", None) != "stream_failed":
            return
        model = getattr(record, "model", None) or "unknown"
        # Per model, so one sick model cannot be masked by -- or hidden behind
        # -- the rest of the deployment's traffic, and so the cooldown is spent
        # per model rather than on whichever one failed first.
        win = self._windows.get(str(model))
        if win is None:
            win = self._windows[str(model)] = _TalliedWindow(self._cfg.window_sec, _Tally())
        now = time.time()
        error_type = getattr(record, "error_type", None)
        count = win.add(now, (str(error_type) if error_type else None,))
        (error_types,) = win.tallies

        def breach_context() -> dict[str, Any]:
            # Exception class names only. The message is deliberately not
            # carried: a relayed upstream error can quote the caller's own
            # request back, and this goes to Slack.
            return {
                "model": str(model),
                "count": count,
                "window_sec": self._cfg.window_sec,
                "top_error_types": _top_offenders(error_types),
            }

        await alert_on_transition(
            key=f"stream_failure_rate:{model}",
            # Strict ``>``, as in ClientErrorBurstRule above.
            breached=count > self._cfg.threshold_count,
            # ERROR, unlike the client-error burst: the stream was accepted,
            # started, and then broke in the user's hands. That is the gateway's
            # fault however it started.
            severity=AlertSeverity.ERROR,
            title=f"Streaming failures for model {model}",
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
    # refused at $19.991 against a $20.00 cap — and rendering that as
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
        today = dt.datetime.now(dt.UTC).date().isoformat()
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
        now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
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
        # The tracked-task rule cannot see a dropped row: no write was attempted.
        # This reads the queue's own loss warning. See RequestLogQueueSaturationRule.
        self._rules.append(
            RequestLogQueueSaturationRule(self._config.rules.request_log_queue_saturation)
        )
        # The two symptoms nothing above can see: a relayed-4xx storm that is
        # routed past the circuit breaker on purpose, and streams that break
        # after the response has already started. See ClientErrorBurstRule and
        # StreamFailureRateRule.
        self._rules.append(ClientErrorBurstRule(self._config.rules.client_error_burst))
        self._rules.append(KeyPoolExhaustionBurstRule(self._config.rules.key_pool_exhaustion_burst))
        self._rules.append(StreamFailureRateRule(self._config.rules.stream_failure_rate))

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
        processed = 0
        try:
            while True:
                record = await self._handler.queue.get()
                for rule in self._rules:
                    try:
                        await rule.on_record(record)
                    except Exception:
                        log.exception("rule %s raised", rule.name)
                processed += 1
                if processed >= _DRAIN_YIELD_EVERY:
                    # ``get`` only suspends on an empty queue, and a rule only
                    # when it sends, so without this a backlog would be drained
                    # in one stretch with every request on the loop behind it.
                    processed = 0
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
