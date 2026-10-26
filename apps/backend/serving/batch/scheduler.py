"""In-process batch worker: drains queued batch items through their targets.

One loop per gateway process claims items from the shared store
(``serving.storage.batch_store``) and runs each in a task of its own. Claiming
is per model and continuous: every model with room under its cap is topped up
whenever the loop runs, and the loop runs as soon as a batch is created in this
process (:meth:`BatchScheduler.notify`), whenever one of its items finishes,
and otherwise every few seconds while any batch has work (less often while
none has) -- so a batch drains at its models' pace, and a busy model never holds
up an idle one.

A model's cap is deployment-wide: ``claim_items`` counts the live items of every
process against it. It is the model's own cap
(:class:`~serving.batch.model_limits.BatchModelLimits`) or
``BATCH_MODEL_CONCURRENCY``, less the interactive requests in flight on the
model while ``BATCH_YIELD_TO_INTERACTIVE`` is on -- every process's, read from
the shared view (:class:`~serving.batch.interactive_load.SharedInteractiveLoad`)
while ``BATCH_SHARED_INTERACTIVE_LOAD`` is on, else this process's own. When the
shared view cannot be read, no new item starts that round.
``BATCH_INTERACTIVE_RESERVE`` keeps that many more slots of every cap free.
Users take turns before batches do, and ``BATCH_USER_MAX_CONCURRENCY`` caps one
user's running items across the deployment -- the batch budget of a user. Claimed items carry a lease the loop keeps
extending; when a process dies its leases run out and any process claims those
items again.

Before every attempt the item's owner is authenticated afresh from the stored
key (:func:`~serving.servers.auth.authenticate_stored_key`): a revoked key or
closed account fails the batch, a spent daily quota pauses the owner's batches
until it renews, and a role below ``BATCH_MIN_ROLE`` pauses them for a while. The item then takes a
slot of the owner's batch quota -- ``BATCH_ROLE_CONCURRENCY_<ROLE>``, counted in
this process by a limiter of the worker's own, apart from the interactive
per-user limiter, so batch work and the owner's own requests never compete for
one budget -- its attempt is marked as dispatched, and it runs through :func:`~serving.batch.dispatch.dispatch_item`
on its target. A failure that may pass (a timeout, a 429, a 5xx) is retried
with exponential backoff, a bounded number of times, and only such failures
count toward cooling a model down; a caller error (any other 4xx) fails the
item at once.

The same loop also does the background work batches need besides inference,
each in at most one task per process at a time: validating uploaded input
files (:mod:`serving.batch.validation`), sealing finished file batches
(:mod:`serving.batch.delivery`), delivering webhooks
(:mod:`serving.batch.webhooks`), and, every sweep, expiry, retention and file
collection.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import random
import secrets
import socket
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from serving.batch.config import batch_config, may_use_batch
from serving.batch.delivery import seal_file_batch
from serving.batch.dispatch import dispatch_item
from serving.batch.interactive_load import INTERACTIVE_LOAD, InteractiveLoad
from serving.batch.validation import validate_file_batch
from serving.batch.webhooks import deliver_due_events
from serving.servers.auth import (
    StoredKeyQuotaExceeded,
    StoredKeyRejected,
    authenticate_stored_key,
)
from serving.servers.concurrency import UserConcurrencyLimiter, canonical_model_id
from serving.storage.batch_store import TARGET_CHAT
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from serving.batch.config import BatchConfig
    from serving.batch.dispatch import DispatchResult
    from serving.batch.interactive_load import SharedInteractiveLoad

logger = get_logger(__name__)


@dataclass(frozen=True)
class SchedulerTimings:
    """The worker's intervals and bounds. Durations are in seconds."""

    #: Longest the loop waits before looking for work again while some batch
    #: has work (it is woken sooner by a new batch or a finished item) ...
    poll_seconds: float = 2.0
    #: ... and while none has.
    idle_poll_seconds: float = 15.0
    #: How often expired batches are settled and old ones purged.
    sweep_seconds: float = 60.0
    #: How long a claim holds an item before another process may take it over.
    lease_seconds: int = 300
    #: How often the leases of this process's running items are extended.
    heartbeat_seconds: float = 60.0
    #: Attempts an item gets; a retryable failure on the last one is final.
    max_attempts: int = 8
    #: Delay before a retry: doubles with every attempt, up to the cap, and
    #: is jittered down by up to half.
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 900.0
    #: Consecutive retryable failures on a model before it is cooled down,
    #: and how long it then gets no new claims.
    cooldown_failures: int = 5
    cooldown_seconds: float = 180.0
    #: Delay before an item whose owner's concurrency slots were all taken is
    #: claimed again.
    slot_retry_seconds: float = 3.0
    #: Delay before an item handed back on its owner's account may be claimed
    #: again: the owner could not be authenticated (a store error), or was
    #: refused, in which case the batch is failed or paused and that verdict
    #: governs -- the delay only matters if recording it failed.
    owner_retry_seconds: float = 15.0
    #: How long the batches of an owner whose role no longer qualifies stay
    #: paused before the role is checked again.
    role_pause_seconds: float = 300.0
    #: How long one attempt may run before it is abandoned as a timeout. The
    #: adapters bound each upstream call already; this bounds an attempt that
    #: never returns, which would otherwise hold its slot, and with the
    #: heartbeat its lease, forever.
    attempt_timeout_seconds: float = 3600.0
    #: How long running items get to finish when the worker stops.
    stop_grace_seconds: float = 5.0
    #: Pauses between the tries at recording an item's result when the store
    #: fails: one more try than there are pauses.
    record_retry_seconds: tuple[float, ...] = (0.5, 2.0)
    #: Work lease of a batch being validated or sealed.
    work_lease_seconds: int = 300
    #: How often webhook deliveries are looked for.
    webhook_poll_seconds: float = 5.0


@dataclass
class _InFlight:
    """One claimed item this process is running."""

    item_id: int
    model_id: str
    attempt: int
    task: asyncio.Task[None] | None = None
    # The item has been handed back to the store (finished, scheduled for
    # retry or released), so it is no longer this process's to release.
    settled: bool = False
    # Its upstream call began: a release now leaves its cost unknown.
    dispatched: bool = False


def _default_worker_id() -> str:
    """Return an id naming this process in claims; unique per worker instance."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


async def _role_quota() -> dict[str, int]:
    """The batch quota of each role, read live like every batch setting."""
    return dict(batch_config().role_concurrency)


class BatchScheduler:
    """Claims and runs this process's share of the queued batch items."""

    def __init__(
        self,
        *,
        app: Any,
        services: Any,
        store: Any,
        limits: Any,
        load: InteractiveLoad | None = None,
        shared_load: SharedInteractiveLoad | None = None,
        share_interactive_load: bool = False,
        file_store: Any = None,
        webhook_store: Any = None,
        decrypt_secret: Callable[[str], str] | None = None,
        worker_id: str | None = None,
        timings: SchedulerTimings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set the worker up; nothing runs until :meth:`start`.

        Args:
            app: The FastAPI application the items' requests run in.
            services: The application's services.
            store: The batch store (``serving.storage.batch_store.BatchStore``).
            limits: The per-model caps
                (:class:`~serving.batch.model_limits.BatchModelLimits`).
            load: Interactive requests in flight per model; the process's own
                count by default.
            shared_load: The deployment-wide interactive load view, or None to
                yield to this process's own load only.
            share_interactive_load: Build that view over *store*, under this
                worker's id, when ``shared_load`` is not given.
            file_store: The batch file store, for validation and collection.
            webhook_store: The webhook store, for deliveries.
            decrypt_secret: Decrypts a stored webhook signing secret.
            worker_id: The name this process claims items under; unique by
                default.
            timings: Intervals and bounds; the defaults by default.
            clock: Monotonic clock for the worker's own schedule.
        """
        self._app = app
        self._services = services
        self._store = store
        self._limits = limits
        # The batch quota of each owner, by role: a limiter of its own, never
        # the interactive one (see the module docstring).
        self._role_slots = UserConcurrencyLimiter(_role_quota)
        self._load = load if load is not None else INTERACTIVE_LOAD
        self._shared_load = shared_load
        self._file_store = file_store
        self._webhook_store = webhook_store
        self._decrypt_secret = decrypt_secret
        self._worker_id = worker_id or _default_worker_id()
        if shared_load is None and share_interactive_load:
            from serving.batch.interactive_load import SharedInteractiveLoad

            self._shared_load = SharedInteractiveLoad(
                store, process_id=self._worker_id, local=self._load
            )
        self._timings = timings or SchedulerTimings()
        self._clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._background: dict[str, asyncio.Task[Any]] = {}
        self._stopping = False
        self._in_flight: dict[int, _InFlight] = {}
        self._running: Counter[str] = Counter()
        self._failure_streak: Counter[str] = Counter()
        self._cooldown_until: dict[str, float] = {}
        self._next_sweep = 0.0
        self._next_heartbeat = 0.0
        self._next_webhooks = 0.0
        self._enabled: bool | None = None
        self._shared_unavailable = False

    @property
    def worker_id(self) -> str:
        """The name this process claims under."""
        return self._worker_id

    def start(self) -> None:
        """Start the loop. Idempotent while it runs."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="batch_scheduler", context=contextvars.Context()
        )
        if self._shared_load is not None and (self._publisher is None or self._publisher.done()):
            self._publisher = asyncio.create_task(
                self._publish_load(), name="batch_load_publisher", context=contextvars.Context()
            )

    def notify(self) -> None:
        """Wake the loop now, e.g. because a batch was just created."""
        self._wake.set()

    async def stop(self) -> None:
        """Stop claiming, let running items finish briefly, release the rest.

        A round in progress completes first, so no claim is dropped half-way.
        Items still running when the grace period ends are cancelled and
        released without using up their attempt, so another process picks them
        up at once instead of waiting out their leases; one whose upstream call
        had begun is recorded with an unknown cost.
        """
        self._stopping = True
        self._wake.set()
        deadline = self._clock() + self._timings.stop_grace_seconds
        loop_task, self._task = self._task, None
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
            if self._shared_load is not None:
                with contextlib.suppress(Exception):
                    await self._shared_load.withdraw()
        background = list(self._background.values())
        self._background.clear()
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if loop_task is None and not self._in_flight:
            return
        if loop_task is not None:
            _, pending = await asyncio.wait({loop_task}, timeout=self._remaining(deadline))
            for task in pending:
                task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)

        entries = list(self._in_flight.values())
        tasks = [e.task for e in entries if e.task is not None and not e.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._remaining(deadline))
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        unsettled = [e for e in entries if not e.settled]
        outcomes = await asyncio.gather(
            *(self._release(e, dispatched=e.dispatched) for e in unsettled),
            return_exceptions=True,
        )
        for entry, outcome in zip(unsettled, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "batch item %s could not be released at shutdown; its lease will expire",
                    entry.item_id,
                    extra={"event": "batch_release_failed", "error_type": type(outcome).__name__},
                )
        logger.info(
            "batch worker %s stopped (%d running item(s) released)",
            self._worker_id,
            len(unsettled),
            extra={"event": "batch_worker_stopped"},
        )

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    # -- the loop -------------------------------------------------------------

    async def _run(self) -> None:
        logger.info(
            "batch worker %s started", self._worker_id, extra={"event": "batch_worker_started"}
        )
        self._next_heartbeat = self._clock() + self._timings.heartbeat_seconds
        while not self._stopping:
            try:
                delay = await self._round()
            except Exception:
                logger.exception(
                    "batch worker round failed", extra={"event": "batch_worker_round_failed"}
                )
                delay = self._timings.idle_poll_seconds
            try:
                await self._heartbeat_if_due()
            except Exception:
                logger.warning(
                    "batch lease heartbeat failed",
                    extra={"event": "batch_heartbeat_failed"},
                    exc_info=True,
                )
            await self._sleep(delay)

    async def _publish_load(self) -> None:
        """Keep this process's interactive counts in the shared view."""
        shared = self._shared_load
        if shared is None:
            return
        while not self._stopping:
            try:
                await shared.publish()
            except Exception:
                logger.warning(
                    "batch: publishing interactive load failed",
                    extra={"event": "batch_load_publish_failed"},
                    exc_info=True,
                )
            await asyncio.sleep(shared.publish_seconds)

    async def _sleep(self, delay: float) -> None:
        if self._stopping:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        self._wake.clear()

    async def _round(self) -> float:
        """Claim what every model has room for; return how long to wait next."""
        cfg = batch_config()
        self._note_enabled(cfg.enabled)
        if not cfg.enabled:
            return self._timings.idle_poll_seconds
        now = self._clock()
        if now >= self._next_sweep:
            self._next_sweep = now + self._timings.sweep_seconds
            await self._sweep(cfg)
        self._spawn_background(cfg, now)
        models = await self._store.active_models()
        if not models:
            return self._timings.idle_poll_seconds
        interactive: dict[str, int] | None = None
        if cfg.yield_to_interactive and cfg.shared_interactive_load and self._shared_load:
            interactive = await self._shared_load.read()
            if interactive is None:
                if not self._shared_unavailable:
                    logger.warning(
                        "batch: the shared interactive load cannot be read; "
                        "starting no batch request until it can",
                        extra={"event": "batch_shared_load_unavailable"},
                    )
                self._shared_unavailable = True
                return self._timings.poll_seconds
            if self._shared_unavailable:
                logger.info(
                    "batch: the shared interactive load is readable again",
                    extra={"event": "batch_shared_load_available"},
                )
            self._shared_unavailable = False
        for model_id in models:
            if self._stopping:
                break
            try:
                await self._top_up(model_id, cfg, now, interactive)
            except Exception:
                logger.warning(
                    "batch claim failed for model %s",
                    model_id,
                    extra={"event": "batch_claim_failed", "model": model_id},
                    exc_info=True,
                )
        return self._timings.poll_seconds

    def _note_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        if self._enabled is not None or not enabled:
            logger.info(
                "batch inference %s; worker %s %s",
                "enabled" if enabled else "disabled",
                self._worker_id,
                "claiming" if enabled else "claims nothing",
                extra={"event": "batch_worker_enabled" if enabled else "batch_worker_disabled"},
            )
        self._enabled = enabled

    async def _sweep(self, cfg: BatchConfig) -> None:
        try:
            counts = await self._store.sweep(retention_days=cfg.retention_days)
        except Exception:
            logger.warning(
                "batch sweep failed", extra={"event": "batch_sweep_failed"}, exc_info=True
            )
            return
        if self._file_store is not None:
            try:
                counts.update(await self._file_store.collect_garbage())
            except Exception:
                logger.warning(
                    "batch file collection failed",
                    extra={"event": "batch_file_gc_failed"},
                    exc_info=True,
                )
        try:
            for batch_id in await self._store.stale_validations():
                await self._store.fail_validation(
                    batch_id=batch_id, worker_id=None, errors={"object": "list", "data": []}
                )
                counts["validation_aborted"] = counts.get("validation_aborted", 0) + 1
        except Exception:
            logger.warning("batch: settling abandoned validations failed", exc_info=True)
        if self._webhook_store is not None:
            with contextlib.suppress(Exception):
                await self._webhook_store.fail_exhausted()
                await self._webhook_store.purge_old_events()
        try:
            backlog = await self._store.seal_backlog()
        except Exception:
            backlog = 0
        if backlog:
            logger.info(
                "batch: %d batch(es) waiting to be sealed",
                backlog,
                extra={"event": "batch_seal_backlog", "count": backlog},
            )
        if counts and any(counts.values()):
            logger.info(
                "batch sweep: %s",
                ", ".join(f"{name}={n}" for name, n in sorted(counts.items())),
                extra={"event": "batch_sweep"},
            )

    def _spawn_background(self, cfg: BatchConfig, now: float) -> None:
        """Start each kind of background work that is not already running here."""
        if self._stopping:
            return
        jobs: dict[str, Callable[[], Awaitable[Any]]] = {}
        if self._file_store is not None:
            jobs["validate"] = lambda: self._work_phase("validating", cfg)
        jobs["seal"] = lambda: self._work_phase("sealing", cfg)
        if self._webhook_store is not None and now >= self._next_webhooks:
            self._next_webhooks = now + self._timings.webhook_poll_seconds
            jobs["webhooks"] = lambda: deliver_due_events(
                webhook_store=self._webhook_store,
                worker_id=self._worker_id,
                allow_private=cfg.webhook_allow_private,
                decrypt=self._decrypt_secret or (lambda value: value),
            )
        for name, factory in jobs.items():
            current = self._background.get(name)
            if current is not None and not current.done():
                continue
            task = asyncio.create_task(
                self._guarded(name, factory), name=f"batch_{name}", context=contextvars.Context()
            )
            self._background[name] = task

    async def _guarded(self, name: str, factory: Callable[[], Awaitable[Any]]) -> None:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "batch %s work failed", name, extra={"event": f"batch_{name}_failed"}, exc_info=True
            )

    async def _work_phase(self, phase: str, cfg: BatchConfig) -> None:
        """Validate or seal what is waiting, one batch at a time."""
        for _ in range(10):
            if self._stopping:
                return
            leased = await self._store.claim_batch_work(
                phase=phase,
                worker_id=self._worker_id,
                lease_seconds=self._timings.work_lease_seconds,
            )
            if not leased:
                return
            batch = leased[0]
            if phase == "validating":
                await validate_file_batch(
                    store=self._store,
                    file_store=self._file_store,
                    batch=batch,
                    worker_id=self._worker_id,
                    services=self._services,
                    cfg=cfg,
                    lease_seconds=self._timings.work_lease_seconds,
                )
                self.notify()
            else:
                sealed = await seal_file_batch(
                    store=self._store,
                    batch=batch,
                    worker_id=self._worker_id,
                    lease_seconds=self._timings.work_lease_seconds,
                )
                if sealed is None:
                    logger.warning(
                        "batch %s: sealing lost its lease; another worker will seal it",
                        batch["id"],
                        extra={"event": "batch_seal_lost"},
                    )

    async def _top_up(
        self, model_id: str, cfg: BatchConfig, now: float, interactive: dict[str, int] | None
    ) -> None:
        """Claim as many of *model_id*'s items as its cap leaves room for."""
        if self._cooling_down(model_id, now):
            return
        cap = cfg.default_model_concurrency
        if self._limits is not None:
            cap = await self._limits.limit_for(model_id, cfg.default_model_concurrency)
        if cfg.yield_to_interactive:
            canonical = self._canonical(model_id)
            if interactive is not None:
                cap -= interactive.get(canonical, 0)
            else:
                cap -= self._load.in_flight(canonical)
            cap -= cfg.interactive_reserve
        room = cap - self._running[model_id]
        if room <= 0:
            return
        items = await self._store.claim_items(
            model_id=model_id,
            worker_id=self._worker_id,
            limit=room,
            max_running=cap,
            lease_seconds=self._timings.lease_seconds,
            user_max_running=cfg.user_max_concurrency,
        )
        if not items:
            return
        logger.debug("batch: claimed %d item(s) of model %s", len(items), model_id)
        for item in items:
            entry = self._track(item)
            if self._stopping:
                # Stopping while the claim was in flight: hand them straight back.
                await self._release(entry)
                self._forget(entry)
                continue
            entry.task = asyncio.create_task(
                self._run_item(entry, item),
                name=f"batch_item:{entry.item_id}",
                context=contextvars.Context(),
            )
            entry.task.add_done_callback(lambda _task, entry=entry: self._forget(entry))

    def _canonical(self, model_id: str) -> str:
        """Return the canonical id interactive load is counted under for *model_id*."""
        router = getattr(self._services, "router", None)
        embeddings = getattr(self._services, "embedding_adapters", None)
        return (
            canonical_model_id(model_id, router=router, embedding_adapters=embeddings) or model_id
        )

    def _cooling_down(self, model_id: str, now: float) -> bool:
        until = self._cooldown_until.get(model_id)
        if until is None:
            return False
        if now < until:
            return True
        del self._cooldown_until[model_id]
        logger.info(
            "batch: model %s cooled down; claiming again",
            model_id,
            extra={"event": "batch_model_cooldown_over", "model": model_id},
        )
        return False

    async def _heartbeat_if_due(self) -> None:
        now = self._clock()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + self._timings.heartbeat_seconds
        item_ids = [e.item_id for e in self._in_flight.values() if not e.settled]
        if item_ids:
            await self._store.heartbeat(
                worker_id=self._worker_id,
                item_ids=item_ids,
                lease_seconds=self._timings.lease_seconds,
            )

    # -- bookkeeping ------------------------------------------------------------

    def _track(self, item: dict[str, Any]) -> _InFlight:
        entry = _InFlight(
            item_id=int(item["id"]),
            model_id=item["model_id"],
            attempt=int(item.get("attempt_seq") or item["attempts"]),
        )
        self._in_flight[entry.item_id] = entry
        self._running[entry.model_id] += 1
        return entry

    def _forget(self, entry: _InFlight) -> None:
        if self._in_flight.get(entry.item_id) is entry:
            del self._in_flight[entry.item_id]
        self._running[entry.model_id] -= 1
        if self._running[entry.model_id] <= 0:
            del self._running[entry.model_id]
        # A slot on the model is free again: let the loop fill it now.
        self.notify()

    def _model_failed(self, model_id: str) -> None:
        streak = self._failure_streak[model_id] + 1
        if streak < self._timings.cooldown_failures:
            self._failure_streak[model_id] = streak
            return
        self._failure_streak.pop(model_id, None)
        self._cooldown_until[model_id] = self._clock() + self._timings.cooldown_seconds
        logger.info(
            "batch: model %s failed %d times in a row; no new claims for %.0fs",
            model_id,
            streak,
            self._timings.cooldown_seconds,
            extra={
                "event": "batch_model_cooldown",
                "model": model_id,
                "consecutive_failures": streak,
                "cooldown_sec": self._timings.cooldown_seconds,
            },
        )

    def _model_succeeded(self, model_id: str) -> None:
        self._failure_streak.pop(model_id, None)

    def _backoff(self, attempt: int) -> float:
        ceiling = min(
            self._timings.backoff_cap_seconds,
            self._timings.backoff_base_seconds * 2.0 ** min(max(attempt - 1, 0), 32),
        )
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    # -- one item -----------------------------------------------------------------

    async def _run_item(self, entry: _InFlight, item: dict[str, Any]) -> None:
        try:
            await self._attempt(entry, item)
        except Exception:
            # Unless it was handed back already, the item is left running in
            # the store: its lease runs out, it is claimed again, and that
            # claim counts as another attempt.
            logger.exception(
                "batch item %s (batch %s) failed in the worker%s",
                entry.item_id,
                item.get("batch_id"),
                "" if entry.settled else "; left to its lease",
                extra={"event": "batch_item_error", "model": entry.model_id},
            )

    async def _attempt(self, entry: _InFlight, item: dict[str, Any]) -> None:
        cfg = batch_config()
        if not cfg.enabled:
            await self._release(entry)
            return
        attempts = int(item["attempts"])
        if attempts > self._timings.max_attempts:
            # Claimed again after its last attempt never reported back (a
            # crashed worker): give up with the last error there is.
            error = item.get("error") or {
                "code": "upstream_error",
                "message": f"The request did not complete in {self._timings.max_attempts} attempts.",
            }
            await self._complete(
                entry, error=error, status_code=error.get("status"), cost_status="not_billed"
            )
            return

        batch_id = item["batch_id"]
        owner = item["user_id"]
        try:
            user_ctx = await authenticate_stored_key(
                item.get("key_hash"), getattr(self._services, "operational_store", None)
            )
        except StoredKeyRejected as exc:
            await self._release(entry, delay=self._timings.owner_retry_seconds)
            await self._fail_batch(batch_id, exc.reason)
            return
        except StoredKeyQuotaExceeded as exc:
            await self._release(entry, delay=self._timings.owner_retry_seconds)
            await self._pause_owner(owner, exc.reset_at, "daily cost quota spent")
            return
        except Exception:
            logger.warning(
                "batch item %s: owner could not be authenticated; retrying later",
                entry.item_id,
                extra={"event": "batch_owner_auth_unavailable", "user_id": owner},
                exc_info=True,
            )
            await self._release(entry, delay=self._timings.owner_retry_seconds)
            return

        role = user_ctx.get("role") or "free"
        if not may_use_batch(role, cfg):
            await self._release(entry, delay=self._timings.owner_retry_seconds)
            await self._pause_owner(
                owner,
                datetime.now(UTC) + timedelta(seconds=self._timings.role_pause_seconds),
                f"role {role!r} is below {cfg.min_role!r}",
            )
            return

        target = item.get("target") or TARGET_CHAT
        dispatch_body = self._dispatch_body(item, entry, target)
        give_back = await self._take_user_slot(user_ctx, entry.model_id)
        if give_back is None:
            logger.debug("batch item %s: owner at concurrency limit; deferred", entry.item_id)
            await self._release(entry, delay=self._timings.slot_retry_seconds)
            return
        try:
            marked = await self._store.mark_dispatched(
                item_id=entry.item_id, worker_id=self._worker_id, attempt=entry.attempt
            )
            if not marked:
                # The claim is gone (its lease was taken over, or the batch
                # deleted): nothing to run, nothing to record.
                entry.settled = True
                return
            entry.dispatched = True
            result = await dispatch_item(
                app=self._app,
                services=self._services,
                user_ctx=user_ctx,
                body=dispatch_body,
                batch_id=batch_id,
                target=target,
                item_id=entry.item_id,
                attempt=entry.attempt,
                protocol_profile=item.get("protocol_profile"),
                timeout_s=self._timings.attempt_timeout_seconds,
            )
        finally:
            give_back()
        await self._settle(entry, result, attempts, cfg)

    def _dispatch_body(self, item: dict[str, Any], entry: _InFlight, target: str) -> Any:
        """The body to run: the stored one, its model pinned to the admitted canonical id.

        The claim and deployment-wide cap are keyed by the canonical model
        admitted when the batch was created. The caller's saved alias is never
        resolved again at execution time: an admin may have remapped it
        meanwhile, which would otherwise run model B while consuming model A's
        capacity. Copied so retrieval still returns the body as the caller
        sent it.
        """
        body = item.get("request")
        if not isinstance(body, dict):
            return body
        # Every target, embeddings included: a model gone from the registry
        # fails as unavailable rather than letting its old alias pick another.
        return {**body, "model": entry.model_id}

    async def _take_user_slot(
        self, user_ctx: dict[str, Any], model: str
    ) -> Callable[[], None] | None:
        """Take a slot of the owner's batch quota; return how to give it back.

        The quota is the owner's role's ``BATCH_ROLE_CONCURRENCY_<ROLE>``, held
        across every model. ``None`` when the owner has none free. *model* only
        labels the deferral in the logs.
        """
        user_id = str(user_ctx.get("user_id") or "")
        granted, _limit, _role = await self._role_slots.try_acquire(
            user_id,
            user_ctx.get("role", "free") or "free",
            bool(user_ctx.get("is_admin", False)),
        )
        if not granted:
            return None

        def give_back() -> None:
            try:
                self._role_slots.release(user_id)
            except Exception:
                logger.exception(
                    "batch: releasing a batch quota slot failed",
                    extra={
                        "event": "batch_slot_release_failed",
                        "user_id": user_ctx.get("user_id"),
                        "model": model,
                    },
                )

        return give_back

    async def _settle(
        self, entry: _InFlight, result: DispatchResult, attempts: int, cfg: BatchConfig
    ) -> None:
        if result.error is None:
            self._model_succeeded(entry.model_id)
            await self._complete(
                entry,
                response=result.response,
                status_code=result.status_code,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=result.cost_usd,
                request_id=result.request_id,
                usage=result.usage,
                cost_status=result.cost_status,
                max_result_bytes=cfg.max_result_bytes,
            )
            return
        if result.retryable:
            # Only a failure that may pass says anything about the model; a
            # caller's own error (4xx) is no reason to slow it down for others.
            self._model_failed(entry.model_id)
            if attempts < self._timings.max_attempts:
                delay = self._backoff(attempts)
                logger.debug(
                    "batch item %s: attempt %d failed (%s); retrying in %.1fs",
                    entry.item_id,
                    attempts,
                    result.error.get("code"),
                    delay,
                )
                await self._store.retry_item(
                    item_id=entry.item_id,
                    worker_id=self._worker_id,
                    attempt=entry.attempt,
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
                    error=result.error,
                    cost_usd=result.cost_usd,
                    cost_status=result.cost_status,
                    status_code=result.status_code,
                    request_id=result.request_id,
                    usage=result.usage,
                )
                entry.settled = True
                return
        await self._complete(
            entry,
            error=result.error,
            status_code=result.status_code,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd,
            request_id=result.request_id,
            usage=result.usage,
            cost_status=result.cost_status,
        )

    # -- store transitions ------------------------------------------------------

    async def _complete(
        self,
        entry: _InFlight,
        *,
        response: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        status_code: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        request_id: str | None = None,
        usage: dict[str, Any] | None = None,
        cost_status: str | None = None,
        max_result_bytes: int | None = None,
    ) -> None:
        """Record the item's final outcome.

        Retried briefly on a store error: the request has been served (and
        perhaps billed), and losing the write would leave the item to its lease
        and run it again. A retry that finds its first try committed changes
        nothing (the write is guarded on this claim).
        """
        for delay in (*self._timings.record_retry_seconds, None):
            try:
                recorded = await self._store.complete_item(
                    item_id=entry.item_id,
                    worker_id=self._worker_id,
                    attempt=entry.attempt,
                    response=response,
                    error=error,
                    status_code=status_code,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                    request_id=request_id,
                    usage=usage,
                    cost_status=cost_status,
                    max_result_bytes=max_result_bytes,
                )
            except Exception:
                if delay is None:
                    raise
                logger.warning(
                    "batch item %s: recording its result failed; retrying",
                    entry.item_id,
                    extra={"event": "batch_complete_retry"},
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                continue
            entry.settled = True
            logger.debug(
                "batch item %s: %s on attempt %d%s",
                entry.item_id,
                "completed" if error is None else f"failed ({error.get('code')})",
                entry.attempt,
                "" if recorded else " (no longer this worker's; not recorded)",
            )
            return

    async def _release(
        self, entry: _InFlight, *, delay: float | None = None, dispatched: bool = False
    ) -> None:
        """Hand the item back to the queue without using up its attempt."""
        await self._store.release_item(
            item_id=entry.item_id,
            worker_id=self._worker_id,
            attempt=entry.attempt,
            next_attempt_at=(
                datetime.now(UTC) + timedelta(seconds=delay) if delay is not None else None
            ),
            dispatched=dispatched,
        )
        entry.settled = True

    async def _fail_batch(self, batch_id: str, reason: str) -> None:
        failed = await self._store.fail_batch(
            batch_id,
            {
                "code": "owner_unauthorized",
                "message": f"The batch's API key no longer authenticates its owner: {reason}.",
            },
        )
        if failed:
            logger.info(
                "batch %s failed: its owner is no longer authorized (%s)",
                batch_id,
                reason,
                extra={"event": "batch_owner_unauthorized", "reason": reason},
            )

    async def _pause_owner(self, user_id: str, until: datetime, reason: str) -> None:
        paused = await self._store.pause_user_batches(user_id, until)
        logger.info(
            "batch: paused %d batch(es) of user %s until %s: %s",
            paused,
            user_id,
            until.isoformat(),
            reason,
            extra={"event": "batch_owner_paused", "user_id": user_id, "reason": reason},
        )
