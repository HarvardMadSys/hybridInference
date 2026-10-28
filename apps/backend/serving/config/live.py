"""Live configuration groups: prepare a new state, then publish it at once.

A *group* owns a fixed set of configuration keys and one schema version. Its
component turns a source snapshot into a typed target (``project``), builds a
complete candidate state for it (``prepare``), and later releases a candidate
it will not use (``discard``) or a state that has been replaced (``retire``).
Publication belongs to the framework: :meth:`LiveGroup.publish` swaps one
immutable reference under a short lock, with no ``await`` and no I/O, and every
reader takes that reference under the same lock (:meth:`LiveGroup.acquire`).
Readers then use the immutable view; nothing relies on the event loop not
switching tasks.

The saved target and the state in use stay separate. A target that fails to
project or prepare leaves the published state in place, reports ``invalid``
or ``failed``, and is retried with backoff (1 s doubling to 60 s, jittered);
a new relevant target is tried at once. A candidate is published only when it
still matches the newest target observed, so a candidate overtaken by a newer
write is discarded however often that happens — a stale channel, a disabled
one or a revoked one is never published to "make progress". An unrelated
write (one whose projection is unchanged) cancels nothing and rebuilds nothing.

Each group applies serially in its own task; groups advance independently. One
reader per source feeds them (:meth:`Coordinator.submit`, called by
``app_config`` after every read: its ten-second poll, and at once after a write
in this process). There is no LISTEN/NOTIFY connection; the database snapshot
is the only source of truth.

A group applies only once an operator has activated its persisted contract
(:mod:`serving.config.revisions`). Until then it is ``dormant``: its keys keep
their restart-required behavior, and its readers use the baseline built at
boot. A group deactivated after it applied is ``dormant-frozen``: it keeps the
last state it published until the process restarts, rather than reverting to
whatever the process booted with.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from serving.config.instances import InstanceReporter
    from serving.config.revisions import Contracts, GroupContract, SourceSnapshot, SourceVersion

logger = get_logger(__name__)

D = TypeVar("D")
V = TypeVar("V")

#: Backoff after a failed prepare, in seconds: doubling from the first, capped.
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
#: Deadlines for one component call.
PREPARE_TIMEOUT_SECONDS = 10.0
DISCARD_TIMEOUT_SECONDS = 5.0
RETIRE_TIMEOUT_SECONDS = 10.0
#: Replaced states still waiting to be retired. At the bound, a group applies
#: nothing new and reports ``cleanup_blocked``: a state still in use is never
#: forced closed, and replaced states do not pile up without limit.
MAX_RETAINED = 4

#: States a group reports. ``paused`` is applied with its component idle by
#: configuration (an alert channel cleared on purpose), not a failure.
STATES = (
    "dormant",
    "dormant-frozen",
    "applying",
    "applied",
    "paused",
    "invalid",
    "failed",
    "unsupported",
    "cleanup_blocked",
)


def _log_failure(what: str, group: str, exc: BaseException) -> None:
    """Log a failure by its class alone.

    A component's exception text can quote what it was handed — a webhook, a
    key — so neither the message nor the traceback is ever logged here.
    """
    logger.error(
        "Live configuration group %s: %s failed (%s); the state in use is kept",
        group,
        what,
        type(exc).__name__,
    )


class InvalidConfig(Exception):
    """A target that cannot be applied, with a stable code and the keys involved.

    The code and keys are what is reported; never put a value in either.
    """

    def __init__(self, code: str, keys: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.keys = keys


class GroupComponent(Protocol[D, V]):
    """What a component provides; the framework does the publishing."""

    def baseline(self) -> tuple[D, V]:
        """Return the target and state this process booted with."""
        ...

    def project(self, snapshot: SourceSnapshot) -> D:
        """Return the typed target *snapshot* configures.

        Raises:
            InvalidConfig: The target cannot be applied.
        """
        ...

    def same(self, desired: D, active: D) -> bool:
        """Whether two targets configure the same state."""
        ...

    async def prepare(self, desired: D, active: V) -> V:
        """Build a complete state for *desired* without disturbing *active*.

        May validate and construct; must not send messages, migrate, change an
        external system or take over a resource *active* holds.
        """
        ...

    async def discard(self, prepared: V) -> None:
        """Release a candidate that will not be published."""
        ...

    async def retire(self, previous: V) -> None:
        """Release a state that has been replaced, once its users are done."""
        ...

    def mirror(self, desired: D) -> Mapping[str, str | None]:
        """Return each key's raw value, for the compatibility readers.

        ``None`` for a key the target leaves unset, so its reader's default
        applies. Two targets the component calls the same may still differ
        here (a key set to its default), and the published target follows.
        """
        ...

    def describe(self, view: V) -> dict[str, Any]:
        """Return non-secret facts about a state for the console."""
        ...

    def paused(self, view: V) -> bool:
        """Whether a state is idle by configuration."""
        ...


@dataclass(frozen=True)
class Published(Generic[D, V]):
    """One published state of a group. Immutable; readers keep it as long as they need."""

    view: V
    desired: D
    #: Local publication count; 0 is the baseline.
    generation: int
    #: The source version the target was read at; ``None`` for the baseline.
    version: SourceVersion | None = None
    #: Published by the protocol, as opposed to the boot baseline.
    managed: bool = False


@dataclass
class GroupStatus:
    """What a group reports. Every field is safe to show and to store."""

    state: str = "dormant"
    epoch: str | None = None
    checked_revision: int | None = None
    reconciled_revision: int | None = None
    generation: int = 0
    #: The contract's schema version, when one is stored.
    schema_version: int | None = None
    error_code: str | None = None
    error_keys: tuple[str, ...] = ()
    cleanup_degraded: bool = False
    #: Keys whose saved target differs from the state in use, while dormant.
    pending_keys: tuple[str, ...] = ()
    regressed: bool = False
    read_stale: bool = False
    #: ``time.time()`` stamps.
    checked_at: float | None = None
    applied_at: float | None = None
    #: When the target now being worked on was first seen unapplied.
    pending_since: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_report(self) -> dict[str, Any]:
        """Return the stored and displayed form."""
        return {
            "state": self.state,
            "epoch": self.epoch,
            "checked_revision": self.checked_revision,
            "reconciled_revision": self.reconciled_revision,
            "generation": self.generation,
            "schema_version": self.schema_version,
            "error_code": self.error_code,
            "error_keys": list(self.error_keys),
            "cleanup_degraded": self.cleanup_degraded,
            "pending_keys": list(self.pending_keys),
            "regressed": self.regressed,
            "read_stale": self.read_stale,
            "checked_at": self.checked_at,
            "applied_at": self.applied_at,
            "pending_since": self.pending_since,
            "detail": dict(self.detail),
        }


@dataclass
class _Retiring:
    """A state awaiting release: a replaced one to retire, or a candidate to discard."""

    view: Any
    attempts: int = 0
    retry_at: float = 0.0
    #: ``retire`` for a replaced state, ``discard`` for an unpublished candidate.
    kind: str = "retire"
    #: The release call in progress. It keeps its claim on the state until it
    #: returns, however long that takes: no second release of the same state
    #: starts, and nobody waits for it past a deadline.
    task: asyncio.Future[Any] | None = None


def _retrieve(task: asyncio.Future[Any]) -> None:
    """Mark a task's outcome seen, so asyncio never logs its exception (and text)."""
    if not task.cancelled():
        task.exception()


class _Superseded(Exception):
    """The candidate no longer matches the newest target; start over."""


class LiveGroup(Generic[D, V]):
    """One configuration group: its published state and its serial applier."""

    def __init__(
        self,
        name: str,
        *,
        schema_versions: tuple[int, ...],
        keys: tuple[str, ...],
        component: GroupComponent[D, V],
        prepare_timeout: float = PREPARE_TIMEOUT_SECONDS,
        retire_timeout: float = RETIRE_TIMEOUT_SECONDS,
        discard_timeout: float = DISCARD_TIMEOUT_SECONDS,
        max_retained: int = MAX_RETAINED,
        backoff_initial: float = BACKOFF_INITIAL_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self.name = name
        self.schema_versions = schema_versions
        self.keys = keys
        self.component = component
        self.prepare_timeout = prepare_timeout
        self.retire_timeout = retire_timeout
        self.discard_timeout = discard_timeout
        self.max_retained = max_retained
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        # A threading lock, held only for a reference swap or read: safe for
        # readers on any thread, and never held across an await.
        self._lock = threading.Lock()
        self._published: Published[D, V] | None = None
        self._managed_ever = False
        self.status = GroupStatus()
        self._latest: SourceSnapshot | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._retiring: list[_Retiring] = []
        #: Prepares abandoned (cancelled or timed out) that have not finished
        #: yet. Whatever one of them still returns is discarded; until then it
        #: counts against :attr:`max_retained`.
        self._abandoned: set[asyncio.Future[Any]] = set()
        #: Held for every release of a state in :attr:`_retiring`, so one
        #: state is never retired or discarded twice at once, whichever of the
        #: applier, a late prepare's completion or :meth:`stop` gets there.
        self._cleanup_lock = asyncio.Lock()
        #: The one task releasing a late candidate while the applier is idle;
        #: :meth:`stop` waits for it within its deadline.
        self._cleanup_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._failed_desired: Any = None
        self._stopping = False
        self._on_status: Callable[[LiveGroup[Any, Any]], None] | None = None
        self._on_publish: Callable[[LiveGroup[Any, Any], Published[Any, Any]], None] | None = None

    # --- readers -----------------------------------------------------------

    def acquire(self) -> Published[D, V]:
        """Return the published state: one immutable reference for the caller's whole use.

        Before a baseline is installed (a process without a database, a test),
        the component's baseline is built on each call and not kept.
        """
        with self._lock:
            published = self._published
        if published is not None:
            return published
        desired, view = self.component.baseline()
        return Published(view, desired, 0)

    def is_current(self, published: Published[D, V]) -> bool:
        """Whether *published* is still the state in force (a reader's fence)."""
        with self._lock:
            current = self._published
        if current is None:
            return published.generation == 0
        return current.generation == published.generation

    @property
    def managed(self) -> bool:
        """Whether the protocol, not the boot baseline, published the state in use."""
        with self._lock:
            published = self._published
        return published is not None and published.managed

    @property
    def active_in_contract(self) -> bool:
        """Whether the last snapshot this applier processed has this group's contract active."""
        return self.status.state not in ("dormant", "dormant-frozen", "unsupported")

    def owns(self, contracts: Contracts) -> bool:
        """Whether *contracts* hand this group's keys to its applier.

        True once the contract is active with a schema version and keys this
        build supports — whether or not a publication has succeeded yet. A
        target that fails to apply is the group's to retry when it is saved
        again, not a restart's; its status reports the failure.
        """
        contract = contracts.groups.get(self.name)
        return contract is not None and contract.active and self._refusal(contract) is None

    def _refusal(self, contract: GroupContract) -> str | None:
        if contract.schema_version not in self.schema_versions:
            return "schema_version_unsupported"
        if set(contract.keys) != set(self.keys):
            return "contract_keys_mismatch"
        return None

    # --- publication ------------------------------------------------------

    def install_baseline(self) -> None:
        """Publish the boot baseline (generation 0). Synchronous."""
        desired, view = self.component.baseline()
        with self._lock:
            self._published = Published(view, desired, 0)

    def publish(self, view: V, desired: D, version: SourceVersion | None) -> Published[D, V] | None:
        """Swap in a prepared state; return the one it replaces.

        No await, no I/O: the swap is one assignment under the reader lock,
        and the observers that follow are synchronous.
        """
        with self._lock:
            previous = self._published
            generation = (previous.generation if previous is not None else 0) + 1
            published = Published(view, desired, generation, version, managed=True)
            self._published = published
        self._managed_ever = True
        self.status.generation = generation
        if self._on_publish is not None:
            try:
                self._on_publish(self, published)
            except Exception as exc:
                _log_failure("publication observer", self.name, exc)
        return previous

    def _adopt(self, desired: D, version: SourceVersion | None) -> None:
        """Make *desired* the target of the state in use without replacing the state.

        For a target the component calls the same as the one in use. The view
        and its generation stay, so nothing is rebuilt and no reader's fence
        moves; the target is swapped when its mirror differs (a key set to its
        default, or reset), so the compatibility readers follow it.
        """
        with self._lock:
            current = self._published
        if current is None:
            return
        if current.managed and self._same_mirror(desired, current.desired):
            return
        adopted = replace(current, desired=desired, managed=True, version=version)
        with self._lock:
            if self._published is not current:
                return  # Only the applier publishes; never undo a newer state.
            self._published = adopted
        self._managed_ever = True
        if self._on_publish is not None:
            try:
                self._on_publish(self, adopted)
            except Exception as exc:
                _log_failure("publication observer", self.name, exc)

    # --- applier ------------------------------------------------------------

    def submit(self, snapshot: SourceSnapshot) -> None:
        """Hand the applier the newest snapshot. Synchronous; never blocks."""
        self._latest = snapshot
        self._wake.set()

    def read_failed(self) -> None:
        """The source could not be read: the state in use stays, its currency is unknown."""
        if not self.status.read_stale:
            self.status.read_stale = True
            self._emit()

    def start(self) -> None:
        """Start the applier task."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name=f"live-config:{self.name}")

    async def stop(self, deadline: float) -> None:
        """Stop applying, discard any candidate, and retire replaced states by *deadline*.

        *deadline* is a ``time.monotonic()`` value. The state in use is left as
        it is: requests still running hold it until the process exits. A
        prepare abandoned earlier is waited for too, so a candidate it still
        returns is discarded rather than left behind; whatever the deadline
        cuts short stays counted in :attr:`retained`.
        """
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        # Every wait below ends by the deadline: a task still running then is
        # cancelled or left to finish on its own, never awaited further.
        if task is not None:
            task.add_done_callback(_retrieve)
            task.cancel()
            await self._wait_until({task}, deadline)
        if self._abandoned:
            # Already cancelled; their completion queues what they return.
            await self._wait_until(set(self._abandoned), deadline)
        cleanup = self._cleanup_task
        if cleanup is not None and not cleanup.done():
            await self._wait_until({cleanup}, deadline)
            if not cleanup.done():
                cleanup.cancel()
        await self._release_due(deadline=deadline)

    @staticmethod
    async def _wait_until(tasks: set[asyncio.Future[Any]], deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if tasks and remaining > 0:
            await asyncio.wait(tasks, timeout=remaining)

    async def run_once(self) -> None:
        """Apply the newest snapshot now (tests, and the loop below)."""
        await self._retire_due()
        snapshot = self._latest
        if snapshot is None:
            return
        await self._reconcile(snapshot)

    async def _run(self) -> None:
        while not self._stopping:
            timeout = self._next_wakeup()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            self._wake.clear()
            if self._stopping:
                return
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_failure("applier", self.name, exc)
                self._set_failed("internal_error", ())

    def _next_wakeup(self) -> float | None:
        now = time.monotonic()
        times = [self._retry_at] if self._retry_at > now else []
        times.extend(retiring.retry_at for retiring in self._retiring)
        if not times:
            return None
        return max(0.05, min(times) - now)

    def _emit(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status(self)
            except Exception as exc:
                _log_failure("status report", self.name, exc)

    def _set_failed(self, code: str, keys: tuple[str, ...], *, state: str = "failed") -> None:
        self.status.state = state
        self.status.error_code = code
        self.status.error_keys = keys
        self._emit()

    def _backoff(self, desired: Any) -> None:
        self._failures += 1
        self._failed_desired = desired
        delay = self._retry_delay(self._failures)
        self._retry_at = time.monotonic() + delay * (0.5 + random.random() / 2)

    def _retry_delay(self, attempts: int) -> float:
        """Doubling from the first delay, capped; the exponent is capped too, so it never overflows."""
        return min(self.backoff_max, self.backoff_initial * 2.0 ** min(max(attempts - 1, 0), 32))

    def _clear_backoff(self) -> None:
        self._failures = 0
        self._retry_at = 0.0
        self._failed_desired = None

    def _contract(self, snapshot: SourceSnapshot) -> tuple[bool, str | None]:
        """Return whether the snapshot's contract activates this group, and why not."""
        contract = snapshot.contracts.groups.get(self.name)
        self.status.schema_version = contract.schema_version if contract is not None else None
        if contract is None or not contract.active:
            return False, None
        refusal = self._refusal(contract)
        return refusal is None, refusal

    def _track_version(self, snapshot: SourceSnapshot) -> None:
        version = snapshot.version
        status = self.status
        epoch = version.epoch if version is not None else None
        revision = version.revision if version is not None else None
        if epoch != status.epoch:
            # A new epoch: nothing reconciled under the old one counts.
            status.reconciled_revision = None
            status.regressed = False
        elif (
            revision is not None
            and status.checked_revision is not None
            and revision < status.checked_revision
        ):
            # Same history, older revision: a restore without a new epoch.
            # Reported, and still applied: the database is the source of truth.
            status.regressed = True
        status.epoch = epoch
        status.checked_revision = revision
        status.read_stale = False
        status.checked_at = time.time()

    def _same_mirror(self, desired: D, active: D) -> bool:
        try:
            return dict(self.component.mirror(desired)) == dict(self.component.mirror(active))
        except Exception:
            return False

    def _pending_keys(self, desired: Any, active: Published[D, V]) -> tuple[str, ...]:
        try:
            target = self.component.mirror(desired)
            current = self.component.mirror(active.desired)
        except Exception:
            return ()
        return tuple(key for key in self.keys if target.get(key) != current.get(key))

    async def _reconcile(self, snapshot: SourceSnapshot) -> None:
        while True:
            try:
                await self._reconcile_one(snapshot)
                return
            except _Superseded:
                snapshot = self._latest or snapshot

    async def _reconcile_one(self, snapshot: SourceSnapshot) -> None:
        status = self.status
        self._track_version(snapshot)
        active_contract, refusal = self._contract(snapshot)
        if not active_contract:
            status.error_keys = ()
            if refusal is not None:
                status.state = "unsupported"
                status.error_code = refusal
            elif self._managed_ever:
                status.state = "dormant-frozen"
                status.error_code = None
            else:
                status.state = "dormant"
                status.error_code = None
            try:
                desired = self.component.project(snapshot)
            except InvalidConfig:
                status.pending_keys = ()
            else:
                status.pending_keys = self._pending_keys(desired, self.acquire())
            status.detail = self._describe(self.acquire())
            self._emit()
            return
        status.pending_keys = ()
        try:
            desired = self.component.project(snapshot)
        except InvalidConfig as exc:
            # The target is kept for the operator to see; the state in use stays.
            status.pending_since = status.pending_since or time.time()
            self._set_failed(exc.code, exc.keys, state="invalid")
            return
        active = self.acquire()
        if self.component.same(desired, active.desired):
            self._adopt(desired, snapshot.version)
            self._clear_backoff()
            self._mark_applied(snapshot.version)
            return
        if self._failed_desired is not None and self.component.same(desired, self._failed_desired):
            if time.monotonic() < self._retry_at:
                return  # Still backing off on this same target.
        else:
            self._clear_backoff()
        if self.retained >= self.max_retained:
            status.pending_since = status.pending_since or time.time()
            self._set_failed("cleanup_blocked", (), state="cleanup_blocked")
            return
        status.state = "applying"
        status.error_code = None
        status.error_keys = ()
        status.pending_since = status.pending_since or time.time()
        self._emit()
        try:
            candidate = await self._prepare(desired, active.view)
        except InvalidConfig as exc:
            self._backoff(desired)
            self._set_failed(exc.code, exc.keys, state="invalid")
            return
        except TimeoutError:
            self._backoff(desired)
            self._set_failed("prepare_timeout", ())
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._backoff(desired)
            logger.warning(
                "Live configuration group %s: prepare failed (%s); keeping the state in use",
                self.name,
                type(exc).__name__,
            )
            self._set_failed("prepare_failed", ())
            return
        # Never publish a candidate known to be stale: compare it with the
        # newest target observed, however many times that has moved on.
        latest = self._latest
        if latest is not None and latest is not snapshot:
            stale = (latest.version and latest.version.epoch) != (
                snapshot.version and snapshot.version.epoch
            )
            newer = desired
            if not stale:
                ok, _ = self._contract(latest)
                if not ok:
                    stale = True
                else:
                    try:
                        newer = self.component.project(latest)
                        stale = not self.component.same(newer, desired)
                    except InvalidConfig:
                        stale = True
            if stale:
                await self._discard(candidate)
                raise _Superseded
            # The same state, but published with the newer target: its mirror
            # may differ (a key set to its default meanwhile).
            snapshot, desired = latest, newer
            self._track_version(snapshot)
        if self._stopping:
            await self._discard(candidate)
            return
        previous = self.publish(candidate, desired, snapshot.version)
        self._clear_backoff()
        self._mark_applied(snapshot.version)
        if previous is not None:
            self._retiring.append(_Retiring(previous.view))
            await self._retire_due()

    def _mark_applied(self, version: SourceVersion | None) -> None:
        status = self.status
        view = self.acquire().view
        status.state = "paused" if self._paused(view) else "applied"
        status.error_code = None
        status.error_keys = ()
        status.reconciled_revision = version.revision if version is not None else None
        if status.pending_since is not None or status.applied_at is None:
            status.applied_at = time.time()
        status.pending_since = None
        status.cleanup_degraded = any(retiring.attempts for retiring in self._retiring)
        status.detail = self._describe(self.acquire())
        self._emit()

    def _paused(self, view: V) -> bool:
        try:
            return bool(self.component.paused(view))
        except Exception:
            return False

    def _describe(self, published: Published[D, V]) -> dict[str, Any]:
        try:
            return dict(self.component.describe(published.view))
        except Exception:
            return {}

    def refresh_detail(self) -> None:
        """Re-read the component's description of the state in use, and report it."""
        self.status.detail = self._describe(self.acquire())
        self._emit()

    async def _prepare(self, desired: D, active: V) -> V:
        """Run ``prepare`` with its deadline; a candidate it leaves behind is discarded."""
        task = asyncio.ensure_future(self.component.prepare(desired, active))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.prepare_timeout)
        except asyncio.CancelledError:
            await self._abandon(task)
            raise
        if not done:
            await self._abandon(task)
            raise TimeoutError
        return task.result()

    async def _abandon(self, task: asyncio.Future[V]) -> None:
        """Cancel a prepare; whatever it still produces, now or later, is discarded."""
        task.cancel()
        try:
            candidate = await asyncio.wait_for(asyncio.shield(task), timeout=self.discard_timeout)
        except asyncio.CancelledError:
            if not task.done():
                self._track_abandoned(task)
            elif not task.cancelled() and task.exception() is None:
                await self._discard(task.result())
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise
            return
        except TimeoutError:
            # It ignores cancellation for now; keep it, and discard what it returns.
            self._track_abandoned(task)
            return
        except Exception:
            return  # It failed: there is no candidate.
        await self._discard(candidate)

    def _track_abandoned(self, task: asyncio.Future[V]) -> None:
        self._abandoned.add(task)

        def finished(done: asyncio.Future[V]) -> None:
            self._abandoned.discard(done)
            if done.cancelled() or done.exception() is not None:
                return
            # Queued like a replaced state, so a discard that fails is retried
            # and the candidate counts against the bound until it is released.
            self._retiring.append(_Retiring(done.result(), kind="discard"))
            self._wake.set()
            if not self._stopping:
                self._schedule_cleanup()

        task.add_done_callback(finished)

    def _schedule_cleanup(self) -> None:
        """Release what is due in one owned task, unless that task is already running."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return  # It takes up whatever is queued before it finishes.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop: the applier or stop releases it.
        self._cleanup_task = loop.create_task(
            self._clean_up(), name=f"live-config-cleanup:{self.name}"
        )

    async def _clean_up(self) -> None:
        # A release that fails is due again only later, so this ends; the
        # applier (or stop) retries it then.
        while any(retiring.retry_at <= time.monotonic() for retiring in self._retiring):
            await self._release_due()

    async def _discard(self, candidate: V) -> None:
        """Discard a candidate; one that cannot be discarded now is retried later.

        The candidate is queued before anything is awaited, so an interruption
        (a shutdown) never loses it: whoever cleans up next releases it.
        """
        retiring = _Retiring(candidate, kind="discard")
        self._retiring.append(retiring)
        async with self._cleanup_lock:
            if retiring in self._retiring and await self._try_retire(
                retiring, timeout=self.discard_timeout
            ):
                self._retiring.remove(retiring)
            self._note_degraded()

    async def _try_retire(self, retiring: _Retiring, *, timeout: float) -> bool:
        """Release one queued state, waiting at most *timeout*; the caller holds the lock.

        A release still running when the wait ends keeps running and keeps the
        state: it is waited for again at the next attempt, never started twice.
        """
        task = retiring.task
        if task is None:
            release = (
                self.component.discard if retiring.kind == "discard" else self.component.retire
            )
            task = asyncio.ensure_future(release(retiring.view))
            task.add_done_callback(_retrieve)
            task.add_done_callback(lambda _: self._release_finished(retiring))
            retiring.task = task
        if not task.done() and timeout > 0:
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            failure = "still running"
        else:
            retiring.task = None
            if task.cancelled():
                failure = "CancelledError"
            elif task.exception() is not None:
                failure = type(task.exception()).__name__
            else:
                return True
        retiring.attempts += 1
        retiring.retry_at = time.monotonic() + self._retry_delay(retiring.attempts)
        logger.warning(
            "Live configuration group %s: releasing a %s state failed (%s); retrying",
            self.name,
            "replaced" if retiring.kind == "retire" else "discarded",
            failure,
        )
        return False

    def _release_finished(self, retiring: _Retiring) -> None:
        """A release returned: its state is due now, whoever was waiting for it or not."""
        if retiring not in self._retiring:
            return
        retiring.retry_at = 0.0
        self._wake.set()
        if not self._stopping:
            self._schedule_cleanup()

    async def _retire_due(self) -> None:
        """Retire replaced states whose retry time has come. Never undoes a publication."""
        await self._release_due()

    async def _release_due(self, *, deadline: float | None = None) -> None:
        """Release queued states, one caller at a time.

        Without *deadline*, only states whose retry time has come, each with
        its own timeout. With one (:meth:`stop`), every queued state, within
        the time left; nothing waits past it.
        """
        if not self._retiring:
            return
        if deadline is None:
            await self._cleanup_lock.acquire()
        else:
            try:
                await asyncio.wait_for(
                    self._cleanup_lock.acquire(), timeout=max(0.0, deadline - time.monotonic())
                )
            except TimeoutError:
                return
        try:
            now = time.monotonic()
            for retiring in list(self._retiring):
                if retiring not in self._retiring:
                    continue
                if deadline is None:
                    if retiring.retry_at > now:
                        continue
                    timeout = self.retire_timeout
                else:
                    timeout = min(self.retire_timeout, deadline - time.monotonic())
                    if timeout <= 0:
                        break
                if await self._try_retire(retiring, timeout=timeout):
                    self._retiring.remove(retiring)
            self._note_degraded()
        finally:
            self._cleanup_lock.release()

    def _note_degraded(self) -> None:
        degraded = any(retiring.attempts for retiring in self._retiring)
        if degraded != self.status.cleanup_degraded:
            self.status.cleanup_degraded = degraded
            if not degraded and self.status.state == "cleanup_blocked":
                self.status.state = "applying"
                self._wake.set()
            self._emit()

    @property
    def retained(self) -> int:
        """States not yet released: replaced, discarded, or still being abandoned."""
        return len(self._retiring) + len(self._abandoned)

    def reset(self) -> None:
        """Forget everything (tests)."""
        for task in (self._task, self._cleanup_task):
            if task is not None:
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self._task = None
        self._cleanup_task = None
        self._cleanup_lock = asyncio.Lock()
        with self._lock:
            self._published = None
        self._managed_ever = False
        self.status = GroupStatus()
        self._latest = None
        self._wake = asyncio.Event()
        self._retiring.clear()
        self._abandoned.clear()
        self._clear_backoff()
        self._stopping = False
        reset = getattr(self.component, "reset", None)
        if reset is not None:
            reset()


class Coordinator:
    """Every live group in this process, fed by one reader per source."""

    def __init__(self) -> None:
        self.groups: dict[str, LiveGroup[Any, Any]] = {}
        self.reporter: InstanceReporter | None = None
        self._publish_observers: list[
            Callable[[LiveGroup[Any, Any], Published[Any, Any]], None]
        ] = []
        #: Contract groups this build does not know, with their schema version.
        self.unknown_groups: dict[str, int] = {}
        self.started = False
        self.last_version: SourceVersion | None = None
        self.read_ok: bool | None = None
        self.acknowledged: bool | None = None

    def register(self, group: LiveGroup[Any, Any]) -> LiveGroup[Any, Any]:
        """Add *group*; it reports through this coordinator."""
        self.groups[group.name] = group
        group._on_status = self._report
        group._on_publish = self._published
        return group

    def on_publish(
        self, observer: Callable[[LiveGroup[Any, Any], Published[Any, Any]], None]
    ) -> None:
        """Call *observer* synchronously right after every publication."""
        self._publish_observers.append(observer)

    def _published(self, group: LiveGroup[Any, Any], published: Published[Any, Any]) -> None:
        for observer in list(self._publish_observers):
            try:
                observer(group, published)
            except Exception as exc:
                _log_failure("publication observer", group.name, exc)

    def capabilities(self) -> dict[str, Any]:
        """The groups and schema versions this build supports."""
        from serving.config.instances import PROTOCOL_VERSION

        return {
            "protocol": PROTOCOL_VERSION,
            "groups": {name: list(group.schema_versions) for name, group in self.groups.items()},
        }

    def _report(self, group: LiveGroup[Any, Any]) -> None:
        if self.reporter is not None:
            self.reporter.set_group(group.name, group.status.as_report())

    def submit(self, snapshot: SourceSnapshot) -> None:
        """Hand every group the newest snapshot of its source. Synchronous."""
        self.last_version = snapshot.version
        self.read_ok = True
        unknown = {
            name: contract.schema_version
            for name, contract in snapshot.contracts.groups.items()
            if name not in self.groups and contract.active
        }
        if unknown != self.unknown_groups:
            gone = set(self.unknown_groups) - set(unknown)
            self.unknown_groups = unknown
            if self.reporter is not None:
                for name in gone:
                    self.reporter.remove_group(name)
                self._report_unknown(self.reporter)
        for group in self.groups.values():
            group.submit(snapshot)

    def _report_unknown(self, reporter: InstanceReporter) -> None:
        """Report every active contract group this build does not know as unsupported."""
        epoch = self.last_version.epoch if self.last_version is not None else None
        for name, version in self.unknown_groups.items():
            reporter.set_group(
                name,
                {
                    "state": "unsupported",
                    "epoch": epoch,
                    "schema_version": version,
                    "error_code": "unknown_group",
                },
            )

    def read_failed(self) -> None:
        """The source read failed: keep every state, report its currency as unknown."""
        self.read_ok = False
        for group in self.groups.values():
            group.read_failed()

    def install_baselines(self) -> None:
        """Publish every group's boot baseline."""
        for group in self.groups.values():
            group.install_baseline()

    def start(self, reporter: InstanceReporter | None) -> None:
        """Start every applier, and the reporter when there is one."""
        self.reporter = reporter
        if reporter is not None:
            reporter.on_report = self._ack
            for group in self.groups.values():
                reporter.set_group(group.name, group.status.as_report())
            # Seen in a snapshot read before the reporter existed (the boot load).
            self._report_unknown(reporter)
            reporter.start()
        for group in self.groups.values():
            group.start()
        self.started = True

    def _ack(self, stored: bool) -> None:
        self.acknowledged = stored

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop applying, then retire, then report the process stopped; within *timeout*."""
        deadline = time.monotonic() + timeout
        if self.reporter is not None:
            self.reporter.set_lifecycle("stopping")
        for group in self.groups.values():
            await group.stop(deadline)
        reporter, self.reporter = self.reporter, None
        if reporter is not None:
            await reporter.stop()
        self.started = False

    def local_status(self) -> dict[str, Any]:
        """This process's own view: no database read, nothing secret."""
        return {
            "groups": {name: group.status.as_report() for name, group in self.groups.items()},
            "unknown_groups": dict(self.unknown_groups),
            "read_ok": self.read_ok,
            "acknowledged": self.acknowledged,
            "version": self.last_version.as_dict() if self.last_version else None,
            "reporter": self.reporter.details() if self.reporter is not None else None,
        }

    def reset(self) -> None:
        """Forget every group's state (tests); registrations stay."""
        for group in self.groups.values():
            group.reset()
        self.reporter = None
        self.unknown_groups = {}
        self.started = False
        self.last_version = None
        self.read_ok = None
        self.acknowledged = None


#: The process's coordinator. Groups register at import of their component.
COORDINATOR = Coordinator()
