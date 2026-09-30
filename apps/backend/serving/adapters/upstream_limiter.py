"""Adaptive per-key cap on the gateway's own outbound concurrency (AIMD on 429).

Nothing else in the request path bounds how many generations the gateway keeps
open against one remote account at once. ``key_pool`` reacts *after* a 429 by
muting the key for a cooldown, and ``servers/concurrency`` caps how many requests
one *user* may have in flight — neither knows, or limits, how wide this process
is pushing against a provider. Against a vendor that meters concurrent requests
(rather than requests per minute) that gap is what turns a traffic spike into a
burst of 429s: every request is admitted, the provider refuses the surplus, and
the pool mutes a key that was never unhealthy.

This module keeps a small **AIMD** controller per bucket and makes requests wait
for a slot instead of piling onto the wire:

- ``limit`` starts at the configured initial value and is clamped to
  ``[1, max_limit]``.
- Any upstream 429 attributed to the bucket is multiplicative-decrease-by-one:
  ``limit = max(1, limit - 1)``, and the probe counter restarts.
- Every ``probe_success_interval`` **successful (HTTP 200) responses** the
  controller probes for headroom: ``limit += 1`` (up to ``max_limit``), the
  bucket is marked *probing*, and the counter restarts. A 429 while probing is
  handled by the decrease rule above, which exactly undoes the probe — logged as
  such so an operator can see the ceiling being found rather than a limit
  oscillating for no visible reason.

Only a 200 advances that counter. An error is not evidence the provider has
headroom, so a 4xx, a 5xx, a timeout or a connection failure must never earn a
probe; equally it is not a rate limit, so it does not cancel the progress
already made toward one. Those outcomes leave the counter exactly where it was,
and only a 429 resets it.

**Bucket scope is (provider label, API key), and the two halves matter.** Quota
on a metered plan is an account-and-service property, so the *provider label*
(``_key_pool_provider_label``) is the right grain, not ``endpoint_id`` — that is
minted per model, and three models on one account would each get their own
allowance of the one pool the vendor actually meters. Within a provider each key
is independent: keys are usually separate accounts with separate allowances, so
one exhausted key must neither throttle its siblings nor inherit their headroom.
Keys are identified by a truncated SHA-256 (:func:`key_fingerprint`); the raw
credential is never stored on a bucket and never logged.

**Local endpoints are exempt.** A vLLM/SGLang/Ollama server this deployment runs
does its own admission control and has no account quota to protect, so queueing
in front of it would only add latency to a server that is already scheduling the
work. The host test reuses ``registry._LOCAL_HOSTS`` rather than restating it.
The one queue a local endpoint can have here is the engine hold below, which a
router asks for and which counts something else entirely.

**At the limit a request waits, then fails over.** Waiting is FIFO through a
future queue, bounded by ``acquire_timeout``. On timeout the caller gets
:class:`UpstreamSaturated`, which callers turn into an upstream failure so the
router's existing fallback chain tries another endpoint. It is deliberately not
a 429 and carries no HTTP status: the provider never saw the request, so
attributing a rate-limit to it would mute a healthy key and charge a healthy
endpoint for this gateway's own admission decision (see
``endpoint_health.record_failure``, which exempts it from the breaker for the
same reason).

**A dispatch may wait less.** A router with somewhere better to send a request
than the back of this queue -- an admin-designated offload route -- pushes a
deadline (``req_ctx.UPSTREAM_QUEUE_DEADLINE``) around the dispatch. A waiter
still queued at that deadline leaves exactly as a timed-out one does and raises
:class:`UpstreamQueueWaitExpired`. The deadline can only shorten the wait: one
further out than ``acquire_timeout`` changes nothing.

**A dispatch may be watched.** The same router times how long an engine takes to
start answering (``routing.engine_wait``), and that clock must not run while the
request is still here. A dispatch's watch is told when the request starts waiting
for a slot and when it gets one -- including at once, and from an exempt local
endpoint (``serving.adapters.dispatch_watch``).

**A local engine may be held for.** An engine queues what it cannot schedule yet
where this gateway cannot see it, so a router with an offload route to use
instead can ask for that queue to form here. A dispatch carrying an
:class:`EngineHold` (``req_ctx.UPSTREAM_ENGINE_HOLD``) is sent to its local engine
only while fewer than the hold's limit of the engine's held dispatches have gone
without a first token. The rest wait in arrival order, per engine, whatever the
limiter's own switch says; an engine's line counts against the limit its latest
dispatch brought, so a policy change applies to the whole line at once. A dispatch's queue deadline ends the wait as above,
with :class:`UpstreamQueueWaitExpired`. One with no deadline of its own has
nowhere else to go, so it waits at most the acquire timeout for its turn and is
then sent anyway: the hold never fails a request, it only decides where the
request waits. A place frees when the adapter reports the engine's first output
(``dispatch_watch.report_first_token``), or at the latest when the slot is
released, so a stream the engine is already answering never counts and nothing
caps how many the engine runs at once.

Counters are plain ints under the asyncio single-thread invariant, exactly as
``servers/concurrency._UserSlot`` documents: every mutation below happens in a
block with no ``await`` in it, so it is atomic with respect to other tasks on
the same loop and needs no lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp

from serving.adapters.dispatch_watch import report_queued, report_sent
from serving.config.settings import get_settings
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)


class UpstreamSaturated(Exception):
    """No outbound slot came free for this bucket before the acquire timeout.

    Raised by :meth:`UpstreamConcurrencyLimiter.acquire`. The request never
    reached the provider, so this is a *gateway* admission decision wearing the
    shape of an upstream failure: callers surface it so the router falls back to
    another endpoint, but nothing may read it as a provider verdict.

    Carries no ``status`` / ``status_code`` / ``code`` attribute on purpose.
    ``endpoint_health._http_status_of`` duck-types those, and any value here
    would be a lie about what the upstream said — 429 most of all, which would
    mute the key in ``KeyPool`` and count a rate limit against an endpoint that
    was never asked.
    """


class UpstreamQueueWaitExpired(UpstreamSaturated):
    """The dispatch's own queue-wait deadline passed before a slot came free.

    Raised instead of the plain saturation when the caller pushed a deadline
    sooner than the acquire timeout (``req_ctx.UPSTREAM_QUEUE_DEADLINE``): the
    caller had an offload route to send the request to, so it chose not to wait
    for this queue to drain. A subclass because nothing else changes -- the
    request still never reached the provider, and every ``UpstreamSaturated``
    reader (key rotation, the breaker exemption, the Anthropic surface) must keep
    treating it that way.
    """


def key_fingerprint(api_key: str | None) -> str:
    """Return a short, non-reversible label for *api_key*.

    Buckets are keyed by this, and it is what the limit-change logs name, so the
    raw credential never has to be held or printed. Twelve hex characters of
    SHA-256 is ample: the values only have to be distinct among the handful of
    keys one provider is configured with, and the digest is not a secret the way
    a prefix of the key itself would be.

    A missing or blank key hashes like the empty string rather than raising: an
    endpoint configured without a credential still shares whatever allowance its
    provider meters, and it should share one bucket, not escape the limiter.
    """
    raw = api_key.strip() if isinstance(api_key, str) else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def is_local_endpoint(base_url: str | None) -> bool:
    """Whether *base_url* names an inference server this gateway runs itself.

    Reuses the registry's ``_LOCAL_HOSTS`` — the same set ``_make_provider_id``
    stamps ``:local-<port>`` from — so the limiter's notion of "local" cannot
    drift from the one the rest of the gateway already uses. The import is
    deferred to the call because ``servers.registry`` imports every adapter at
    module scope, and this module is imported *by* those adapters.
    """
    if not base_url:
        return False
    from serving.servers.registry import _LOCAL_HOSTS

    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        # An unparseable base_url is not evidence of a local server; treat it as
        # remote so a malformed route is limited rather than exempted.
        return False
    return host in _LOCAL_HOSTS


def _engine_address(base_url: str | None) -> str:
    """Return the ``host:port`` one local engine is reached at, its hold's key.

    Two routes to the same server share one hold whatever path their base URLs
    carry, since it is the server whose queue is being counted.
    """
    raw = base_url or ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return raw
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{host}:{port}"


class EngineHold:
    """One streaming dispatch's place in line for a local engine.

    A router pushes one around a streaming attempt (``req_ctx.UPSTREAM_ENGINE_HOLD``)
    to have :meth:`UpstreamConcurrencyLimiter.acquire` hold the dispatch while its
    local engine already has ``limit`` held dispatches without a first token.
    The limiter binds the place it grants here, so the adapter's first-token
    report (:meth:`on_first_token`) can give it up while the stream carries on.
    The slot the adapter holds gives it up too, whichever comes first.
    """

    __slots__ = ("_admission", "limit")

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("an engine hold's limit must be a whole number of at least 1")
        self.limit = limit
        self._admission: _EngineAdmission | None = None

    def bind(self, admission: _EngineAdmission) -> None:
        """Record the place the limiter granted this dispatch."""
        self._admission = admission

    def on_first_token(self) -> None:
        """Give the place up: the engine has started answering."""
        admission = self._admission
        if admission is not None:
            admission.release()


@dataclass
class _EngineGate:
    """The held dispatches one local engine was sent that have no first token yet.

    ``pending`` counts the places granted and not yet given up, and waiters
    queue in arrival order. ``limit`` is the one the latest dispatch brought, so
    a policy change applies from the next request on, to the whole line: a
    request queued under the old limit is not left blocking the ones behind it.
    Mutated only from blocks with no ``await``, the invariant :class:`_Bucket`
    documents.
    """

    address: str
    limit: int = 1
    pending: int = 0
    waiters: deque[asyncio.Future[None]] = field(default_factory=deque)
    loop: asyncio.AbstractEventLoop | None = None
    # Bumped when the gate drops its state for a new loop, so a place granted on
    # the old loop is not given back into the new count.
    epoch: int = 0

    def rebind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Adopt *loop*, dropping the places and waiters of a previous one."""
        if self.loop is loop:
            return
        self.loop = loop
        self.waiters.clear()
        self.pending = 0
        self.epoch += 1

    def admits(self, limit: int) -> bool:
        """Adopt *limit* and return whether a dispatch arriving now may go at once.

        The line is served under the new limit first, so the new dispatch goes
        only with room left over, never ahead of a request already waiting.
        """
        self.limit = limit
        self.wake_waiters()
        return self.pending < self.limit

    def grant(self) -> _EngineAdmission:
        """Count one more dispatch sent to the engine and return its place."""
        self.pending += 1
        return _EngineAdmission(self)

    def give_back(self, epoch: int) -> None:
        """Uncount one dispatch granted in *epoch* and let the next one go."""
        if epoch != self.epoch:
            return
        if self.pending > 0:
            self.pending -= 1
        self.wake_waiters()

    def wake_waiters(self) -> None:
        """Send the longest-waiting dispatches on while the engine has room for them.

        The place is counted here, not by the woken task, so that nobody takes it
        between the grant and the waiter resuming -- as ``_Bucket.wake_waiters``
        does.
        """
        while self.waiters and self.pending < self.limit:
            waiter = self.waiters.popleft()
            if waiter.done():
                # Timed out or cancelled in the same tick; it holds no place.
                continue
            self.pending += 1
            waiter.set_result(None)

    def abandon(self, waiter: asyncio.Future[None]) -> None:
        """Drop a waiter that gave up, giving back a place granted in the same tick."""
        # ValueError: not queued any more -- granted (handled just below) or
        # already dropped by a wake.
        with suppress(ValueError):
            self.waiters.remove(waiter)
        if waiter.done() and not waiter.cancelled():
            self.give_back(self.epoch)


class _EngineAdmission:
    """One place granted at a local engine, given back exactly once."""

    __slots__ = ("_epoch", "_gate")

    def __init__(self, gate: _EngineGate) -> None:
        self._gate: _EngineGate | None = gate
        self._epoch = gate.epoch

    def release(self) -> None:
        """Give the place back; later calls do nothing."""
        gate = self._gate
        if gate is None:
            return
        self._gate = None
        gate.give_back(self._epoch)


@dataclass
class _Bucket:
    """AIMD state and waiter queue for one (provider, key) pair.

    ``in_flight`` and the AIMD counters are mutated only from blocks containing
    no ``await``, so on a single-threaded event loop they are atomic with
    respect to other tasks and need no lock — the same invariant
    ``servers/concurrency._UserSlot`` relies on.
    """

    provider: str
    fingerprint: str
    limit: int
    max_limit: int
    probe_success_interval: int
    in_flight: int = 0
    # Successful (HTTP 200) responses since the last probe or 429. Nothing else
    # touches it: every other outcome leaves it unchanged, and a 429 resets it.
    successes_since_probe: int = 0
    # True between a probe raising the limit and the next 429 or probe. Only
    # used to label the log line when a 429 undoes the probe it just granted.
    probing: bool = False
    waiters: deque[asyncio.Future[None]] = field(default_factory=deque)
    # The loop the queued futures belong to. Futures are bound to the loop that
    # created them, so a bucket that outlives its loop (tests build a fresh loop
    # per case; a process could rebuild its loop on restart-in-place) must drop
    # its waiter state rather than hand out futures nobody can ever resolve.
    loop: asyncio.AbstractEventLoop | None = None

    def rebind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Adopt *loop*, discarding waiter state bound to a previous one.

        ``in_flight`` is reset alongside the queue: a slot taken on a loop that
        is gone can never be released, so carrying the count forward would
        permanently shrink the bucket. The learned ``limit`` is kept — it
        describes the provider, not the loop.
        """
        if self.loop is loop:
            return
        if self.loop is not None:
            logger.debug(
                "upstream_limit_loop_rebound",
                extra={
                    "event": "upstream_limit_loop_rebound",
                    "provider": self.provider,
                    "key_fingerprint": self.fingerprint,
                    # What is being discarded, not what remains.
                    "waiting": len(self.waiters),
                    "in_flight": self.in_flight,
                },
            )
        self.loop = loop
        self.waiters.clear()
        self.in_flight = 0

    def has_free_slot(self) -> bool:
        """Whether a slot can be taken *now* without jumping the queue.

        Queued waiters block the fast path even when a slot is free: they were
        promised FIFO service, and a newly arriving request that grabbed the
        slot ahead of them would starve the oldest one under sustained load.
        """
        return not self.waiters and self.in_flight < self.limit

    def take_slot(self) -> None:
        """Reserve one in-flight slot for the caller."""
        self.in_flight += 1

    def complete(self, status_code: int | None) -> None:
        """Give a slot back and fold *status_code* into the AIMD state.

        Exactly two outcomes move anything: a 429 shrinks the limit, and an HTTP
        200 counts toward the next probe. Everything else — a 4xx, a 5xx, the 0
        sentinel for a timeout or connection failure, a neutral ``None`` release
        whose outcome is unknown, even a 2xx that is not 200 — leaves both alone.
        An error says nothing about whether the provider had room for one more
        concurrent request, so it must neither earn a probe nor undo the
        successes already counted toward one.
        """
        if self.in_flight > 0:
            self.in_flight -= 1
        if status_code == 429:
            self._decrease()
        elif status_code == 200:
            self._count_success()
        self.wake_waiters()

    def _decrease(self) -> None:
        """Apply the multiplicative-decrease half of AIMD (here: minus one)."""
        old = self.limit
        self.limit = max(1, self.limit - 1)
        # A probe that raised the limit by one is exactly undone by a decrease of
        # one, so there is no separate rollback — but say so in the log, or the
        # pair reads as an unexplained limit flapping up and down.
        reverted_probe = self.probing
        self.probing = False
        self.successes_since_probe = 0
        # A bucket already at the floor logs nothing, unless it is a probe being
        # taken back: that one is a state change an operator should see even
        # though the number did not move.
        if old != self.limit or reverted_probe:
            self._log_limit_change(old, "429_reverted_probe" if reverted_probe else "429")

    def _count_success(self) -> None:
        """Count one HTTP 200 and probe for headroom every N of them."""
        self.successes_since_probe += 1
        if self.successes_since_probe < self.probe_success_interval:
            return
        self.successes_since_probe = 0
        old = self.limit
        self.limit = min(self.max_limit, self.limit + 1)
        self.probing = True
        if old != self.limit:
            self._log_limit_change(old, "probe")

    def _log_limit_change(self, old_limit: int, reason: str) -> None:
        """Emit the one structured line that records a limit moving."""
        logger.warning(
            "upstream_limit_changed",
            extra={
                "event": "upstream_limit_changed",
                "provider": self.provider,
                "key_fingerprint": self.fingerprint,
                "old_limit": old_limit,
                "new_limit": self.limit,
                "in_flight": self.in_flight,
                "waiting": len(self.waiters),
                "reason": reason,
            },
        )

    def wake_waiters(self) -> None:
        """Hand slots to the longest-waiting callers while the bucket has room.

        The slot is reserved here, not by the woken task: between resolving the
        future and the waiter resuming, another ``acquire`` could otherwise take
        the very slot this grant promised.
        """
        while self.waiters and self.in_flight < self.limit:
            waiter = self.waiters.popleft()
            if waiter.done():
                # Timed out or cancelled in the same tick; it holds no slot.
                continue
            self.in_flight += 1
            waiter.set_result(None)

    def abandon(self, waiter: asyncio.Future[None]) -> None:
        """Drop a waiter that gave up, returning a slot granted in the same tick.

        ``asyncio.wait_for`` can fire its timeout after :meth:`wake_waiters` has
        already resolved the future — the grant and the timeout land in the same
        iteration and the timeout wins the race to resume. That slot is reserved
        in a name that will never use it, so it has to go back, which in turn may
        wake the next waiter.
        """
        # ValueError: not queued any more — either already granted (handled
        # just below) or already dropped by a previous wake.
        with suppress(ValueError):
            self.waiters.remove(waiter)
        if waiter.done() and not waiter.cancelled():
            self.complete(None)


class UpstreamSlot:
    """A claim on one outbound in-flight slot, released exactly once.

    ``release`` is idempotent because a streaming request unwinds through
    several paths — normal completion, mid-stream error, client disconnect — and
    more than one of them can run for a single request. A second release must be
    a no-op rather than decrementing another request's slot.

    A slot with no bucket is the form handed back when the limiter is disabled
    or the endpoint is local. It holds the dispatch's place at a local engine
    when a hold granted one (:class:`EngineHold`), and otherwise nothing at all.
    """

    __slots__ = ("_admission", "_bucket", "_released")

    def __init__(
        self,
        bucket: _Bucket | None,
        admission: _EngineAdmission | None = None,
    ) -> None:
        self._bucket = bucket
        self._admission = admission
        self._released = bucket is None and admission is None

    @property
    def held(self) -> bool:
        """Whether this slot still holds capacity (False once released or inert)."""
        return not self._released

    def release(self, *, status_code: int | None = None) -> None:
        """Return the slot and report the upstream outcome.

        Args:
            status_code: HTTP status the upstream answered with, 0 for a
                non-HTTP failure, or None when the outcome is unknown and must
                not move the AIMD state.
        """
        if self._released:
            return
        self._released = True
        admission = self._admission
        self._admission = None
        if admission is not None:
            # Already given up if the engine started answering; a stream that
            # ended first gives it up here.
            admission.release()
        bucket = self._bucket
        self._bucket = None
        if bucket is not None:
            bucket.complete(status_code)


# Inert slot for every call the limiter declines to police. Shared because it
# carries no state: ``release`` on it is a no-op whoever holds it.
_INERT_SLOT = UpstreamSlot(None)


class UpstreamConcurrencyLimiter:
    """Per-(provider, key) outbound concurrency limiter with AIMD tuning.

    One instance is shared process-wide (see :func:`get_upstream_limiter`);
    tests construct their own with explicit tunables instead of touching the
    environment.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        initial_limit: int = 8,
        max_limit: int = 64,
        probe_success_interval: int = 100,
        acquire_timeout: float = 30.0,
    ) -> None:
        self._enabled = enabled
        self._max_limit = max(1, int(max_limit))
        self._initial_limit = min(self._max_limit, max(1, int(initial_limit)))
        self._probe_success_interval = max(1, int(probe_success_interval))
        self._acquire_timeout = float(acquire_timeout)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._gates: dict[str, _EngineGate] = {}

    @classmethod
    def from_settings(cls) -> UpstreamConcurrencyLimiter:
        """Build a limiter from the ``UPSTREAM_CONCURRENCY_*`` settings."""
        settings = get_settings()
        return cls(
            enabled=settings.upstream_concurrency_enabled,
            initial_limit=settings.upstream_concurrency_initial_limit,
            max_limit=settings.upstream_concurrency_max_limit,
            probe_success_interval=settings.upstream_concurrency_probe_success_interval,
            acquire_timeout=settings.upstream_concurrency_acquire_timeout_sec,
        )

    @property
    def enabled(self) -> bool:
        """Whether this limiter polices anything at all."""
        return self._enabled

    # The four tunables below are exposed read-only so an operator reading the
    # live state can see what the numbers are being measured against — a limit
    # of 8 means nothing without knowing whether that is the starting value or
    # the ceiling. They are the resolved values, already clamped by __init__, so
    # they reflect what the controller actually enforces rather than what the
    # environment asked for.

    @property
    def initial_limit(self) -> int:
        """Limit a bucket starts at, before any AIMD feedback moves it."""
        return self._initial_limit

    @property
    def max_limit(self) -> int:
        """Ceiling a probe will never raise a bucket past."""
        return self._max_limit

    @property
    def probe_success_interval(self) -> int:
        """HTTP 200s a bucket must count before it probes for headroom."""
        return self._probe_success_interval

    @property
    def acquire_timeout(self) -> float:
        """Seconds a request waits for a slot before :class:`UpstreamSaturated`."""
        return self._acquire_timeout

    async def acquire(
        self,
        provider: str,
        api_key: str | None,
        *,
        base_url: str | None = None,
    ) -> UpstreamSlot:
        """Take an outbound slot for *provider*/*api_key*, waiting if saturated.

        Args:
            provider: Operator-facing provider label, as produced by
                ``openai_compat._key_pool_provider_label``. Quota is metered per
                account/service, so this — not ``endpoint_id`` — is the grain.
            api_key: The credential the request will actually be sent with, so a
                pooled adapter buckets by the key it just acquired rather than
                by the route. Never stored; only its fingerprint is.
            base_url: The endpoint's base URL, used to exempt local inference
                servers from the limit, and to find the engine a held dispatch
                waits for.

        Returns:
            A slot the caller must release once the request (for a stream, the
            whole generation) is done.

        Raises:
            UpstreamQueueWaitExpired: The dispatch's own queue-wait deadline
                passed first (see :meth:`_queue_wait`), or passed while it was
                held for its local engine (see :meth:`_admit_to_engine`).
            UpstreamSaturated: No slot came free within the acquire timeout.
        """
        if is_local_endpoint(base_url):
            return await self._admit_to_engine(base_url)
        if not self._enabled:
            report_sent()
            return _INERT_SLOT

        loop = asyncio.get_running_loop()
        bucket = self._bucket_for(provider, api_key)
        bucket.rebind_loop(loop)

        if bucket.has_free_slot():
            bucket.take_slot()
            report_sent()
            return UpstreamSlot(bucket)

        wait, deadline_bound = self._queue_wait()
        if deadline_bound and wait <= 0:
            # The deadline had already passed when this dispatch reached the
            # queue -- a pooled adapter rotating onto a sibling key after waiting
            # out the first one. Joining the queue only to leave it again would
            # take a place in line from nobody, so leave straight away.
            raise self._queue_wait_expired(bucket)

        report_queued()
        waiter: asyncio.Future[None] = loop.create_future()
        bucket.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, wait)
        except asyncio.TimeoutError:
            bucket.abandon(waiter)
            if deadline_bound:
                raise self._queue_wait_expired(bucket) from None
            logger.warning(
                "upstream_concurrency_saturated",
                extra={
                    "event": "upstream_concurrency_saturated",
                    "provider": bucket.provider,
                    "key_fingerprint": bucket.fingerprint,
                    "limit": bucket.limit,
                    "in_flight": bucket.in_flight,
                    "waiting": len(bucket.waiters),
                    "timeout_sec": self._acquire_timeout,
                },
            )
            raise UpstreamSaturated(
                f"No outbound slot for provider {bucket.provider!r} "
                f"(limit={bucket.limit}) within {self._acquire_timeout:g}s"
            ) from None
        except BaseException:
            # Cancellation (client disconnect, router timeout) unwinds the same
            # way, but surfaces as itself rather than as saturation.
            bucket.abandon(waiter)
            raise
        # Woken: ``wake_waiters`` already reserved the slot in our name.
        report_sent()
        return UpstreamSlot(bucket)

    async def _admit_to_engine(self, base_url: str | None) -> UpstreamSlot:
        """Send a dispatch to its local engine, holding it here first if it must wait.

        A dispatch with no :class:`EngineHold` goes straight to the engine, with
        an inert slot. One with a hold sets the engine's limit to its own, and
        goes while fewer than that many of the engine's held dispatches lack a
        first token. Otherwise it waits its turn: until its queue deadline, when
        it has one, or else for at most the acquire timeout, after which it is
        sent anyway, past the limit.

        Raises:
            UpstreamQueueWaitExpired: The dispatch's queue deadline passed while
                it was held.
        """
        hold = req_ctx.get().get(req_ctx.UPSTREAM_ENGINE_HOLD)
        if not isinstance(hold, EngineHold):
            report_sent()
            return _INERT_SLOT

        loop = asyncio.get_running_loop()
        gate = self._gate_for(base_url)
        gate.rebind_loop(loop)
        if gate.admits(hold.limit):
            return self._send_to_engine(gate.grant(), hold)

        wait, deadline_bound = self._hold_wait()
        if deadline_bound and wait <= 0:
            raise self._engine_hold_expired(gate)

        report_queued()
        waiter: asyncio.Future[None] = loop.create_future()
        gate.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, wait)
        except asyncio.TimeoutError:
            gate.abandon(waiter)
            if deadline_bound:
                raise self._engine_hold_expired(gate) from None
            # Its turn has not come, and nothing else can take this request:
            # send it on, over the limit, to queue at the engine as it would
            # have with no hold at all.
            logger.warning(
                "engine_hold_overflowed",
                extra={
                    "event": "engine_hold_overflowed",
                    "engine": gate.address,
                    "limit": gate.limit,
                    "pending": gate.pending,
                    "waiting": len(gate.waiters),
                    "timeout_sec": self._acquire_timeout,
                },
            )
            return self._send_to_engine(gate.grant(), hold)
        except BaseException:
            # Cancellation (client disconnect, router timeout) leaves the line
            # the same way, and surfaces as itself.
            gate.abandon(waiter)
            raise
        # Woken: ``wake_waiters`` already counted this dispatch's place.
        return self._send_to_engine(_EngineAdmission(gate), hold)

    @staticmethod
    def _send_to_engine(admission: _EngineAdmission, hold: EngineHold) -> UpstreamSlot:
        """Bind a granted place to its dispatch's hold and let the request go."""
        hold.bind(admission)
        report_sent()
        return UpstreamSlot(None, admission)

    def _hold_wait(self) -> tuple[float, bool]:
        """Return how long a held dispatch waits for its turn, and whether it then leaves.

        A dispatch with a queue deadline waits until that deadline and then
        leaves for the offload route it was armed for (``True``), however far
        away the deadline is: without the hold it would have waited that long at
        the engine for its first token. One without a deadline waits the acquire
        timeout and is then sent anyway (``False``).
        """
        deadline = req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return self._acquire_timeout, False
        return float(deadline) - time.monotonic(), True

    @staticmethod
    def _engine_hold_expired(gate: _EngineGate) -> UpstreamQueueWaitExpired:
        """Build the error for a held dispatch whose queue deadline came first.

        Logged at debug only, as :meth:`_queue_wait_expired` is: the router
        armed the deadline for an offload route and logs sending it there.
        """
        logger.debug(
            "engine_hold_wait_expired",
            extra={
                "event": "engine_hold_wait_expired",
                "engine": gate.address,
                "limit": gate.limit,
                "pending": gate.pending,
                "waiting": len(gate.waiters),
            },
        )
        return UpstreamQueueWaitExpired(
            f"Local engine {gate.address!r} still had {gate.pending} requests without a "
            f"first token (limit={gate.limit}) at the dispatch's queue-wait deadline"
        )

    def _queue_wait(self) -> tuple[float, bool]:
        """Return how long this dispatch may queue, and whether its deadline binds.

        The acquire timeout is the ceiling. A dispatch's deadline can only bring
        the end of the wait forward: one further out than the timeout is ignored,
        and the wait then ends as plain saturation, exactly as it would without
        a deadline.
        """
        deadline = req_ctx.get().get(req_ctx.UPSTREAM_QUEUE_DEADLINE)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return self._acquire_timeout, False
        remaining = float(deadline) - time.monotonic()
        if remaining < self._acquire_timeout:
            return remaining, True
        return self._acquire_timeout, False

    @staticmethod
    def _queue_wait_expired(bucket: _Bucket) -> UpstreamQueueWaitExpired:
        """Build the error for a dispatch whose own deadline ended its wait.

        Logged at debug only: the caller armed the deadline because it has an
        offload route to send the request to, and it logs that decision itself.
        The ``upstream_concurrency_saturated`` warning stays reserved for a wait
        that ran out with nowhere better to go.
        """
        logger.debug(
            "upstream_queue_wait_expired",
            extra={
                "event": "upstream_queue_wait_expired",
                "provider": bucket.provider,
                "key_fingerprint": bucket.fingerprint,
                "limit": bucket.limit,
                "in_flight": bucket.in_flight,
                "waiting": len(bucket.waiters),
            },
        )
        return UpstreamQueueWaitExpired(
            f"No outbound slot for provider {bucket.provider!r} "
            f"(limit={bucket.limit}) before the dispatch's queue-wait deadline"
        )

    def _bucket_for(self, provider: str, api_key: str | None) -> _Bucket:
        """Return the bucket for one (provider, key), creating it on first use."""
        label = provider.strip() if isinstance(provider, str) and provider.strip() else "unknown"
        fingerprint = key_fingerprint(api_key)
        bucket = self._buckets.get((label, fingerprint))
        if bucket is None:
            bucket = _Bucket(
                provider=label,
                fingerprint=fingerprint,
                limit=self._initial_limit,
                max_limit=self._max_limit,
                probe_success_interval=self._probe_success_interval,
            )
            self._buckets[(label, fingerprint)] = bucket
        return bucket

    def _gate_for(self, base_url: str | None) -> _EngineGate:
        """Return the hold for one local engine, creating it on first use."""
        address = _engine_address(base_url)
        gate = self._gates.get(address)
        if gate is None:
            gate = _EngineGate(address=address)
            self._gates[address] = gate
        return gate

    def engine_snapshot(self) -> dict[str, dict[str, int]]:
        """Return each held-for local engine's live count, keyed by ``host:port``.

        ``pending`` is the held dispatches sent to the engine without a first
        token yet, and ``waiting`` the ones still held here. Like
        :meth:`snapshot`, an engine appears once a held dispatch has created it.
        """
        return {
            address: {"pending": gate.pending, "waiting": len(gate.waiters)}
            for address, gate in self._gates.items()
        }

    def snapshot(self) -> dict[tuple[str, str], dict[str, int | bool]]:
        """Return each bucket's live AIMD state, keyed by (provider, fingerprint).

        Read by tests, by debugging, and by the admin
        ``/admin/upstream-concurrency`` view. A bucket exists only once traffic
        has created it, so an idle gateway snapshots empty — that is the true
        answer, not a missing one.

        Nothing here is derived from the credential beyond the fingerprint the
        bucket was already keyed by: the raw key is never held on a bucket, so
        it cannot leak through this method.
        """
        return {
            key: {
                "limit": bucket.limit,
                "in_flight": bucket.in_flight,
                "waiting": len(bucket.waiters),
                "successes_since_probe": bucket.successes_since_probe,
                "probing": bucket.probing,
            }
            for key, bucket in self._buckets.items()
        }


_LIMITER: UpstreamConcurrencyLimiter | None = None


def get_upstream_limiter() -> UpstreamConcurrencyLimiter:
    """Return the process-wide limiter, building it from settings on first use."""
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = UpstreamConcurrencyLimiter.from_settings()
    return _LIMITER


def reset_upstream_limiter(limiter: UpstreamConcurrencyLimiter | None = None) -> None:
    """Replace (or, with no argument, drop) the process-wide limiter.

    Test hook. Passing a limiter installs it; passing nothing clears the
    singleton so the next :func:`get_upstream_limiter` rebuilds from settings.
    """
    global _LIMITER
    _LIMITER = limiter


async def acquire_upstream_slot(
    provider: str,
    api_key: str | None,
    *,
    base_url: str | None = None,
) -> UpstreamSlot:
    """Acquire a slot from the process-wide limiter. See :meth:`acquire`."""
    return await get_upstream_limiter().acquire(provider, api_key, base_url=base_url)


def outcome_status(exc: BaseException | None) -> int | None:
    """Map how a request ended onto the status the AIMD controller should see.

    - ``None`` (no exception) is a success: 200, the only value that counts
      toward the next probe.
    - An HTTP status error reports its own status, which is the only way the
      429 that drives the decrease rule ever arrives.
    - Any other ``Exception`` is a non-HTTP failure: 0, the same sentinel
      ``KeyPool`` uses.
    - A ``BaseException`` that is not an ``Exception`` (cancellation, generator
      close) releases neutrally with ``None``.

    Only the first two move any AIMD state; see :meth:`_Bucket.complete`.
    """
    if exc is None:
        return 200
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status
    if isinstance(exc, Exception):
        return 0
    return None


@asynccontextmanager
async def upstream_slot(
    provider: str,
    api_key: str | None,
    *,
    base_url: str | None = None,
) -> AsyncIterator[UpstreamSlot]:
    """Hold an outbound slot for the duration of the block.

    For call sites whose whole upstream interaction is already one ``async
    with`` — the slot can be chained onto it (``async with
    upstream_slot(...), session.post(...) as resp:``) without restructuring the
    body. The outcome is derived from how the block ended (see
    :func:`outcome_status`), so every exit path — including a client disconnect
    cancelling mid-stream — hands the slot back exactly once.

    Raises:
        UpstreamSaturated: no slot came free within the acquire timeout.
    """
    slot = await acquire_upstream_slot(provider, api_key, base_url=base_url)
    try:
        yield slot
    except BaseException as exc:
        slot.release(status_code=outcome_status(exc))
        raise
    else:
        slot.release(status_code=200)
    finally:
        # Idempotent; only reached without a release if the block exited in a
        # way neither branch above saw.
        slot.release()
