"""Virtual Token Counter (VTC) Fair Scheduling.

Implements Sheng's VTC algorithm for per-user fair resource allocation at
request level. Each user has a virtual counter tracking how much service
they have received. The scheduler always prioritises the user with the
lowest **current** counter (i.e. the one served least so far).

Design goals
------------
- Modular: ``FairnessScheduler`` is an abstract base — swap algorithms by
  subclassing without touching call sites.
- Per-model isolation: each model maintains independent counters and queues.
- Counter Lift: prevents idle users from monopolising the system on re-join.
- Rich logging for observability.

Scheduling data structure
--------------------------
Requests are stored in **per-user FIFO queues** (a dict of deques).  When
the watcher is ready to dispatch, it performs an O(n) linear scan over the
*n* distinct users that currently have pending requests, picking the one
with the lowest **current** counter value.

This is intentionally simpler than a priority heap.  The key correctness
property is that scheduling always uses the *up-to-date* counter — a heap
would snapshot the counter at arrival time, causing stale priorities once
earlier requests for the same user complete and raise their counter.

Typical call sequence
---------------------
1. ``acquire(model_id, user_id, estimated_tokens, timeout)``
   — called before the request is executed; blocks until the scheduler
   grants capacity (or times out).
2. ``on_request_finish(model_id, user_id, actual_input_tokens, actual_output_tokens)``
   — called once the response is complete; updates the user's counter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .rate_limiter import PersistentRateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class _WaitingEntry:
    """A single request waiting in a per-user FIFO queue."""

    user_id: str
    estimated_tokens: int
    event: asyncio.Event
    arrived_at: float = field(default_factory=time.time)
    timed_out: bool = False
    dispatched: bool = False


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class FairnessScheduler(ABC):
    """Abstract fairness scheduler interface.

    Concrete implementations only need to provide three methods.  All
    routing code depends on this interface, making algorithm replacement a
    matter of swapping the concrete class in bootstrap.
    """

    @abstractmethod
    async def acquire(
        self,
        model_id: str,
        user_id: str,
        estimated_tokens: int,
        timeout: float = 30.0,
    ) -> tuple[bool, dict[str, Any]]:
        """Request capacity on behalf of *user_id* for *model_id*.

        Returns
        -------
        (success, metadata)
            *success* is ``True`` when the caller may proceed.
            *metadata* carries observability fields (counters, wait times…).
        """
        ...

    @abstractmethod
    async def on_request_finish(
        self,
        model_id: str,
        user_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Update fairness state after a request completes.

        Must be called regardless of whether the request succeeded or failed
        so that counters and active-user bookkeeping stay consistent.
        """
        ...

    @abstractmethod
    def get_status(self, model_id: str) -> dict[str, Any]:
        """Return a snapshot of the scheduler state for *model_id*."""
        ...


# ---------------------------------------------------------------------------
# Per-model state
# ---------------------------------------------------------------------------


class _VTCModelState:
    """All VTC state for a single model.

    Waiting requests are stored in ``_waiting_queues``: a dict mapping each
    user_id to a FIFO deque of their pending ``_WaitingEntry`` objects.
    When the watcher needs to dispatch, it scans all users in
    ``_waiting_queues`` and picks the one with the lowest **current**
    counter — O(n) in the number of distinct waiting users.

    Attributes
    ----------
    model_id:
        Identifier of the model this state belongs to.
    counters:
        Virtual token counter per user — total (weighted) tokens served so far.
    active_users:
        Users that currently have at least one request in ``_waiting_queues``.
        Used to compute the counter-lift minimum for newly joining users.
    last_active_counter:
        Counter value when the system went completely idle.  Used to lift a
        brand-new (or long-absent) user on join, preventing them from
        monopolising service with a stale counter of 0.
    """

    def __init__(self, model_id: str, rate_limiter: PersistentRateLimiter | None) -> None:
        self.model_id = model_id
        self._rate_limiter = rate_limiter

        self.counters: dict[str, float] = {}
        self.active_users: set[str] = set()
        self.last_active_counter: float = 0.0

        # Per-user FIFO queues.  Keys are user_ids; values are deques of entries.
        self._waiting_queues: dict[str, deque[_WaitingEntry]] = {}

        self._lock: asyncio.Lock = asyncio.Lock()
        self._watcher_task: asyncio.Task[None] | None = None

        logger.info("[VTC:%s] Model state created", model_id)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def acquire(
        self,
        user_id: str,
        estimated_tokens: int,
        timeout: float,
    ) -> tuple[bool, dict[str, Any]]:
        """Acquire capacity for *user_id*.  May block up to *timeout* seconds."""
        start_ts = time.time()

        async with self._lock:
            # ---- 1. Handle join & counter lift ----
            if user_id not in self.active_users:
                self._apply_counter_lift(user_id)

            user_counter = self.counters[user_id]
            total_waiting = sum(len(q) for q in self._waiting_queues.values())
            logger.debug(
                "[VTC:%s] acquire user=%s counter=%.1f tokens=%d "
                "waiting_users=%d total_waiting=%d",
                self.model_id,
                user_id,
                user_counter,
                estimated_tokens,
                len(self._waiting_queues),
                total_waiting,
            )

            # ---- 2. Fast path: no one waiting → try immediate dispatch ----
            if not self._waiting_queues:
                success, remaining = await self._try_consume(estimated_tokens)
                if success:
                    logger.info(
                        "[VTC:%s] Fast dispatch user=%s counter=%.1f " "tokens=%d remaining=%.0f",
                        self.model_id,
                        user_id,
                        user_counter,
                        estimated_tokens,
                        remaining,
                    )
                    return True, {
                        "fairness": "immediate",
                        "vtc_counter": round(user_counter, 1),
                        "tokens_consumed": estimated_tokens,
                    }

            # ---- 3. Must queue into per-user FIFO ----
            event: asyncio.Event = asyncio.Event()
            entry = _WaitingEntry(
                user_id=user_id,
                estimated_tokens=estimated_tokens,
                event=event,
                arrived_at=start_ts,
            )
            if user_id not in self._waiting_queues:
                self._waiting_queues[user_id] = deque()
            self._waiting_queues[user_id].append(entry)
            self.active_users.add(user_id)

            logger.info(
                "[VTC:%s] Queued user=%s counter=%.1f tokens=%d " "queue_depth=%d waiting_users=%d",
                self.model_id,
                user_id,
                user_counter,
                estimated_tokens,
                len(self._waiting_queues[user_id]),
                len(self._waiting_queues),
            )
            self._ensure_watcher()

        # Wait outside the lock so other coroutines can proceed
        remaining_timeout = max(0.0, timeout - (time.time() - start_ts))
        try:
            await asyncio.wait_for(event.wait(), timeout=remaining_timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                entry.timed_out = True
                self._cleanup_queues()
                logger.debug(
                    "[VTC:%s] System state after timeout: active_users=%s",
                    self.model_id,
                    sorted(self.active_users),
                )
            logger.warning(
                "[VTC:%s] Timeout user=%s after %.1fs " "(counter=%.1f tokens=%d)",
                self.model_id,
                user_id,
                timeout,
                self.counters.get(user_id, 0.0),
                estimated_tokens,
            )
            return False, {
                "error": "Rate limit exceeded",
                "tokens_requested": estimated_tokens,
                "retry_after": 30,
            }

        if entry.timed_out:
            # Race: dispatched just as we timed out; treat as timeout
            return False, {
                "error": "Rate limit exceeded",
                "tokens_requested": estimated_tokens,
                "retry_after": 30,
            }

        wait_time = time.time() - start_ts
        logger.info(
            "[VTC:%s] Dispatched user=%s counter=%.1f wait=%.2fs tokens=%d",
            self.model_id,
            user_id,
            self.counters.get(user_id, 0.0),
            wait_time,
            estimated_tokens,
        )
        return True, {
            "fairness": "queued",
            "vtc_counter": round(self.counters.get(user_id, 0.0), 1),
            "wait_time_s": round(wait_time, 3),
            "tokens_consumed": estimated_tokens,
        }

    async def on_request_finish(
        self,
        user_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Update VTC counter after a request completes.

        W_prompt = W_decode = 1 for the initial version (simple token sum).
        After updating, log the new counter relative to all other known users
        for easy observability of fairness.
        """
        cost = float(actual_input_tokens + actual_output_tokens)

        async with self._lock:
            old_counter = self.counters.get(user_id, 0.0)
            self.counters[user_id] = old_counter + cost

            logger.info(
                "[VTC:%s] Counter update user=%s %.1f → %.1f " "(cost=%.0f, input=%d, output=%d)",
                self.model_id,
                user_id,
                old_counter,
                self.counters[user_id],
                cost,
                actual_input_tokens,
                actual_output_tokens,
            )

            # Log all counters for fairness observability
            if self.counters:
                counter_summary = ", ".join(
                    f"{u}={c:.0f}" for u, c in sorted(self.counters.items())
                )
                logger.debug("[VTC:%s] All counters: %s", self.model_id, counter_summary)

            # Handle leave: remove user from active_users when they have no more
            # live pending requests.  This covers both the fast-path case (user
            # was added to active_users by counter-lift but never queued) and the
            # normal case (user's last queued request was just dispatched).
            user_still_queuing = user_id in self._waiting_queues and any(
                not e.timed_out and not e.dispatched for e in self._waiting_queues[user_id]
            )
            if not user_still_queuing:
                self.active_users.discard(user_id)
                if not self.active_users:
                    self.last_active_counter = self.counters[user_id]
                    logger.debug(
                        "[VTC:%s] System idle, last_active_counter=%.1f",
                        self.model_id,
                        self.last_active_counter,
                    )

            # Attempt to dispatch next waiting request
            self._cleanup_queues()
            if self._waiting_queues:
                self._ensure_watcher()

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot suitable for admin/health endpoints."""
        waiting: list[dict[str, Any]] = []
        for uid, q in self._waiting_queues.items():
            for entry in q:
                if not entry.timed_out and not entry.dispatched:
                    waiting.append(
                        {
                            "user_id": uid,
                            "current_vtc_counter": round(self.counters.get(uid, 0.0), 1),
                            "estimated_tokens": entry.estimated_tokens,
                            "wait_s": round(time.time() - entry.arrived_at, 1),
                        }
                    )
        return {
            "model_id": self.model_id,
            "active_users": sorted(self.active_users),
            "counters": {u: round(c, 1) for u, c in sorted(self.counters.items())},
            "last_active_counter": round(self.last_active_counter, 1),
            "waiting_queue_size": len(waiting),
            "waiting_queue": waiting,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_counter_lift(self, user_id: str) -> None:
        """Lift *user_id*'s counter on join.  Must be called under ``_lock``."""
        current = self.counters.get(user_id, 0.0)

        if self.active_users:
            min_active = min(self.counters[u] for u in self.active_users)
            lifted = max(current, min_active)
            logger.debug(
                "[VTC:%s] Counter lift user=%s: %.1f → %.1f (min_active=%.1f)",
                self.model_id,
                user_id,
                current,
                lifted,
                min_active,
            )
        else:
            lifted = max(current, self.last_active_counter)
            logger.debug(
                "[VTC:%s] Counter lift user=%s: %.1f → %.1f " "(idle system, last=%.1f)",
                self.model_id,
                user_id,
                current,
                lifted,
                self.last_active_counter,
            )

        self.counters[user_id] = lifted
        self.active_users.add(user_id)

    def _pick_next_user(self) -> str | None:
        """O(n) scan: return the user with the lowest current counter.

        Only considers users that have at least one live (non-timed-out,
        non-dispatched) entry in their queue.  Returns ``None`` when the
        waiting queues are empty.
        """
        best_user: str | None = None
        best_counter = float("inf")

        for uid, q in self._waiting_queues.items():
            # Skip users whose entire queue consists of dead entries
            if not any(not e.timed_out and not e.dispatched for e in q):
                continue
            c = self.counters.get(uid, 0.0)
            if c < best_counter:
                best_counter = c
                best_user = uid

        return best_user

    def _cleanup_queues(self) -> None:
        """Remove timed-out/dispatched entries; drop empty user queues.

        Must be called under ``_lock``.
        """
        empty_users: list[str] = []
        for uid, q in self._waiting_queues.items():
            # Drop dead entries from the front of each per-user queue
            while q and (q[0].timed_out or q[0].dispatched):
                dead = q.popleft()
                logger.debug(
                    "[VTC:%s] Cleaned %s entry user=%s",
                    self.model_id,
                    "timed-out" if dead.timed_out else "dispatched",
                    uid,
                )
            if not q:
                empty_users.append(uid)

        for uid in empty_users:
            del self._waiting_queues[uid]
            self.active_users.discard(uid)
            if not self.active_users:
                self.last_active_counter = self.counters.get(uid, 0.0)
                logger.debug(
                    "[VTC:%s] System idle after cleanup, last_active_counter=%.1f",
                    self.model_id,
                    self.last_active_counter,
                )

    async def _try_consume(self, tokens: int) -> tuple[bool, float]:
        """Non-blocking capacity check against the rate limiter.

        Delegates to ``PersistentRateLimiter.try_consume_tokens`` which
        acquires the rate-limiter's own lock internally.  Safe to call while
        the VTC lock is held because asyncio is single-threaded and no
        circular dependency exists.
        """
        if self._rate_limiter is None:
            return True, 0.0
        return await self._rate_limiter.try_consume_tokens(self.model_id, tokens)

    def _ensure_watcher(self) -> None:
        """Start the background refill watcher if not already running.

        Must be called under ``_lock``.
        """
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(
                self._refill_watcher(),
                name=f"vtc-watcher-{self.model_id}",
            )
            logger.debug("[VTC:%s] Refill watcher task started", self.model_id)

    async def _refill_watcher(self) -> None:
        """Background task: dispatch waiting requests as capacity becomes available.

        Each iteration:
        1. Clean up dead entries.
        2. Pick the user with the lowest **current** counter (O(n) scan).
        3. Try to consume capacity for their first queued request.
        4. If capacity available: pop + signal; loop immediately to dispatch more.
        5. If not: sleep until the bucket is estimated to refill, then retry.
        """
        logger.debug("[VTC:%s] Refill watcher running", self.model_id)

        while True:
            async with self._lock:
                self._cleanup_queues()

                if not self._waiting_queues:
                    logger.debug("[VTC:%s] Queue empty — watcher exiting", self.model_id)
                    return

                # O(n) pick: who has the lowest current counter?
                next_user = self._pick_next_user()
                if next_user is None:
                    logger.debug("[VTC:%s] No live entries found — watcher exiting", self.model_id)
                    return

                entry = self._waiting_queues[next_user][0]
                next_counter = self.counters.get(next_user, 0.0)

                logger.debug(
                    "[VTC:%s] Watcher considering user=%s counter=%.1f tokens=%d "
                    "(waiting_users=%d)",
                    self.model_id,
                    next_user,
                    next_counter,
                    entry.estimated_tokens,
                    len(self._waiting_queues),
                )

                success, remaining = await self._try_consume(entry.estimated_tokens)

                if success:
                    self._waiting_queues[next_user].popleft()
                    entry.dispatched = True

                    # Clean up empty user queue
                    if not self._waiting_queues[next_user]:
                        del self._waiting_queues[next_user]
                        self.active_users.discard(next_user)
                        if not self.active_users:
                            self.last_active_counter = self.counters.get(next_user, 0.0)

                    logger.info(
                        "[VTC:%s] Watcher dispatched user=%s counter=%.1f "
                        "tokens=%d remaining_users=%d",
                        self.model_id,
                        next_user,
                        next_counter,
                        entry.estimated_tokens,
                        len(self._waiting_queues),
                    )
                    entry.event.set()
                    # Loop immediately — there may be more capacity
                    continue

                # Not enough capacity yet — estimate how long to sleep
                sleep_for = self._estimate_refill_sleep(entry.estimated_tokens)
                logger.debug(
                    "[VTC:%s] No capacity for %d tokens (remaining=%.0f), " "sleeping %.2fs",
                    self.model_id,
                    entry.estimated_tokens,
                    remaining,
                    sleep_for,
                )

            # Sleep outside the lock so other coroutines can make progress
            await asyncio.sleep(sleep_for)

    def _estimate_refill_sleep(self, tokens_needed: int) -> float:
        """Estimate seconds until the token bucket can satisfy *tokens_needed*."""
        if self._rate_limiter is None:
            return 0.05
        bucket = self._rate_limiter.buckets.get(self.model_id)
        if bucket is None or bucket.refill_rate <= 0:
            return 0.5
        deficit = max(0.0, tokens_needed - bucket.tokens)
        time_to_refill = deficit / bucket.refill_rate
        # Clamp: at least 50 ms, at most 2 s
        return max(0.05, min(time_to_refill, 2.0))


# ---------------------------------------------------------------------------
# Public scheduler
# ---------------------------------------------------------------------------


class VTCFairnessScheduler(FairnessScheduler):
    """Request-level Virtual Token Counter (VTC) fairness scheduler.

    Maintains one ``_VTCModelState`` per model.  States are created lazily
    on first use so there is no need to pre-register models.

    Parameters
    ----------
    rate_limiter:
        Optional ``PersistentRateLimiter`` used for capacity checks.  When
        ``None`` the scheduler provides ordering only (all requests are
        granted immediately after leaving the per-user queues).
    """

    def __init__(self, rate_limiter: PersistentRateLimiter | None = None) -> None:
        self._rate_limiter = rate_limiter
        self._states: dict[str, _VTCModelState] = {}
        logger.info(
            "[VTC] VTCFairnessScheduler initialised (rate_limiter=%s)",
            "attached" if rate_limiter is not None else "none",
        )

    # ------------------------------------------------------------------
    # FairnessScheduler interface
    # ------------------------------------------------------------------

    async def acquire(
        self,
        model_id: str,
        user_id: str,
        estimated_tokens: int,
        timeout: float = 30.0,
    ) -> tuple[bool, dict[str, Any]]:
        """Acquire capacity for *user_id* on *model_id* using VTC ordering."""
        logger.debug(
            "[VTC] acquire model=%s user=%s tokens=%d timeout=%.1f",
            model_id,
            user_id,
            estimated_tokens,
            timeout,
        )
        state = self._get_state(model_id)
        return await state.acquire(user_id, estimated_tokens, timeout)

    async def on_request_finish(
        self,
        model_id: str,
        user_id: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Update the VTC counter for *user_id* on *model_id*."""
        logger.debug(
            "[VTC] on_request_finish model=%s user=%s input=%d output=%d",
            model_id,
            user_id,
            actual_input_tokens,
            actual_output_tokens,
        )
        state = self._get_state(model_id)
        await state.on_request_finish(user_id, actual_input_tokens, actual_output_tokens)

    def get_status(self, model_id: str) -> dict[str, Any]:
        """Return scheduler state for *model_id*."""
        state = self._states.get(model_id)
        if state is None:
            return {"model_id": model_id, "state": "no_requests_seen"}
        return state.get_status()

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Return scheduler state for every known model."""
        return {mid: s.get_status() for mid, s in self._states.items()}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_state(self, model_id: str) -> _VTCModelState:
        if model_id not in self._states:
            self._states[model_id] = _VTCModelState(model_id, self._rate_limiter)
        return self._states[model_id]
