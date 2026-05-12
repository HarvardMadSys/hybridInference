"""SMART_ECONOMIC hedging for latency-aware routing.

This module provides:
- ``survival_at`` / ``cdf_separate_at``: Empirical survival and CDF functions
  using SEPARATE mode (success-only samples) for hedge threshold computation.
- ``compute_hedge_threshold``: Grid search for the minimum elapsed time h*
  where hedging is cost-justified under the economic model.
- ``ProviderEventSink``: Protocol for reporting per-provider outcomes.
- ``HedgedAdapter``: Composite adapter that races primary vs delayed backup.

The SEPARATE mode CDF used here differs from the INFINITY mode CDF in
``latency.py`` (used for LP constraints).  Keeping them separate avoids
interface confusion on ProviderProfile.

Reference algorithm: experiment/strategies/smart_hedging.py::smart_hedge_economic().
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .latency import ProviderProfile

from routing.routers import _has_non_empty_content
from serving.adapters.base import BaseAdapter
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Survival / CDF functions (SEPARATE mode)
# ---------------------------------------------------------------------------


def survival_at(
    profile: ProviderProfile,
    t_sec: float,
    current_time: float,
) -> float:
    """Compute survival function S(t) = P(T > t | success).

    Uses SEPARATE mode: only successful latency samples are considered.
    Failures are handled separately by the hedge trigger logic.

    Args:
        profile: Provider latency profile.
        t_sec: Time threshold in seconds.
        current_time: Reference time for window pruning.

    Returns:
        S(t) in [0, 1].  Returns 1.0 if no samples (assume high latency).
    """
    samples = profile._get_latency_samples_sec(current_time)
    if not samples:
        return 1.0
    return sum(1 for s in samples if s > t_sec) / len(samples)


def cdf_separate_at(
    profile: ProviderProfile,
    t_sec: float,
    current_time: float,
) -> float:
    """Compute CDF F(t) = P(T <= t | success) in SEPARATE mode.

    Args:
        profile: Provider latency profile.
        t_sec: Time threshold in seconds.
        current_time: Reference time for window pruning.

    Returns:
        F(t) in [0, 1].  Returns 0.0 if no samples.
    """
    return 1.0 - survival_at(profile, t_sec, current_time)


# ---------------------------------------------------------------------------
# Hedge threshold computation (SMART_ECONOMIC grid search)
# ---------------------------------------------------------------------------


def compute_hedge_threshold(
    primary_profile: ProviderProfile,
    backup_profile: ProviderProfile,
    slo_sec: float,
    cost_ratio: float,
    dispatch_overhead_sec: float,
    current_time: float,
    resolution_sec: float = 0.1,
) -> float:
    """Find minimum elapsed time h* where hedging is cost-justified.

    Decision rule at each candidate h:
        P_viol(h) * F_backup(remaining) > cost_ratio

    Where:
    - P_viol(h) = S_primary(SLO) / S_primary(h) = P(primary violates | survived to h)
    - F_backup(remaining) = P(backup finishes within SLO - h - overhead)
    - cost_ratio = C_b / V (backup cost relative to violation penalty)

    Args:
        primary_profile: Latency profile for the primary provider.
        backup_profile: Latency profile for the backup provider.
        slo_sec: SLO deadline in seconds.
        cost_ratio: C_b / V threshold.
        dispatch_overhead_sec: Backup launch overhead in seconds.
        current_time: Reference time for profile queries.
        resolution_sec: Grid search step size in seconds.

    Returns:
        Optimal hedge time h* in seconds.  Returns float("inf") if the
        condition is never met (hedging not justified).
    """
    # Pre-compute S_primary(SLO) once -- it does not change across the grid.
    s_primary_slo = survival_at(primary_profile, slo_sec, current_time)

    # If primary never violates SLO (S(SLO) ~ 0 means all requests finish
    # before the deadline), hedging is never justified regardless of h.
    if s_primary_slo < 1e-6:
        return float("inf")

    # Grid search from 0 to slo_sec in resolution_sec steps.
    steps = int(slo_sec / resolution_sec)
    for i in range(steps + 1):
        h = i * resolution_sec

        remaining = slo_sec - h - dispatch_overhead_sec
        if remaining <= 0:
            break  # No time for backup; hedge would be pointless.

        s_primary_h = survival_at(primary_profile, h, current_time)
        p_viol = 1.0 if s_primary_h < 1e-6 else s_primary_slo / s_primary_h

        f_backup = cdf_separate_at(backup_profile, remaining, current_time)

        if p_viol * f_backup > cost_ratio:
            return h

    return float("inf")


def compute_probability_targeted_hedge_threshold(
    primary_profile: ProviderProfile,
    backup_profile: ProviderProfile,
    slo_sec: float,
    success_target: float,
    dispatch_overhead_sec: float,
    current_time: float,
    resolution_sec: float = 0.1,
) -> float:
    """Find latest elapsed time where primary-plus-backup SLO success meets target."""
    s_primary_slo = survival_at(primary_profile, slo_sec, current_time)
    latest: float | None = None
    steps = int(slo_sec / resolution_sec)
    for i in range(steps + 1):
        elapsed = i * resolution_sec
        remaining = slo_sec - elapsed - dispatch_overhead_sec
        if remaining <= 0:
            break
        s_primary_elapsed = survival_at(primary_profile, elapsed, current_time)
        conditional_primary_miss = (
            1.0 if s_primary_elapsed < 1e-6 else min(1.0, s_primary_slo / s_primary_elapsed)
        )
        backup_miss = survival_at(backup_profile, remaining, current_time)
        p_success = 1.0 - conditional_primary_miss * backup_miss
        if p_success >= success_target:
            latest = elapsed
    return latest if latest is not None else float("inf")


# ---------------------------------------------------------------------------
# ProviderEventSink protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProviderEventSink(Protocol):
    """Protocol for reporting per-provider request outcomes.

    HedgedAdapter uses this to inform the router's health tracking about
    individual provider successes and failures, so circuit breakers see
    the real per-provider outcomes rather than just the composite result.
    """

    def on_provider_success(self, provider: str) -> None:
        """Record a successful request for *provider*."""
        ...

    def on_provider_failure(self, provider: str, reason: str) -> None:
        """Record a failed request for *provider*."""
        ...


# ---------------------------------------------------------------------------
# HedgedAdapter
# ---------------------------------------------------------------------------


class HedgedAdapter(BaseAdapter):
    """Composite adapter that races a primary against a delayed backup.

    BaseRouter sees HedgedAdapter as a single opaque BaseAdapter.  Internally
    it launches the primary immediately and, after ``hedge_threshold_sec``,
    starts the backup.  The first provider to produce a result wins; the loser
    is cancelled.

    Per-provider outcomes are reported to ``event_sink`` so that circuit
    breakers and health tracking see individual provider results.

    Attributes:
        primary: The primary adapter (launched immediately).
        backup: The backup adapter (launched after h* seconds).
        hedge_threshold_sec: Delay before launching the backup.
        event_sink: Callback for per-provider health reporting.
    """

    def __init__(
        self,
        primary: BaseAdapter,
        backup: BaseAdapter,
        hedge_threshold_sec: float,
        event_sink: ProviderEventSink,
    ) -> None:
        super().__init__(primary.config)  # BaseRouter reads primary's config
        self.primary = primary
        self.backup = backup
        self.hedge_threshold_sec = hedge_threshold_sec
        self.event_sink = event_sink

    # ---------------------------------------------------------------
    # Non-streaming race
    # ---------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        """Race primary against delayed backup for non-streaming completion.

        When backup wins, ``self.config`` is swapped to the backup adapter's
        config so that BaseRouter reads the real winner's provider/endpoint_id
        for ``_routing`` metadata and ``req_ctx``.
        """
        primary_provider = self.primary.config.provider
        backup_provider = self.backup.config.provider

        # Tracks whether the backup task has progressed past its initial
        # sleep(h*) delay.  When primary fails, we only cancel+relaunch the
        # backup if it is still sleeping; if the real request is already
        # in-flight, cancelling it would waste an otherwise-useful attempt.
        backup_past_sleep = False

        async def _run_primary() -> dict[str, Any]:
            return await self.primary.chat_completion(messages, **params)

        async def _run_backup_delayed() -> dict[str, Any]:
            nonlocal backup_past_sleep
            await asyncio.sleep(self.hedge_threshold_sec)
            backup_past_sleep = True
            return await self.backup.chat_completion(messages, **params)

        async def _run_backup_immediate() -> dict[str, Any]:
            nonlocal backup_past_sleep
            backup_past_sleep = True
            return await self.backup.chat_completion(messages, **params)

        primary_task = asyncio.ensure_future(_run_primary())
        backup_task = asyncio.ensure_future(_run_backup_delayed())
        pending = {primary_task, backup_task}

        primary_error: BaseException | None = None

        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        if task is primary_task:
                            self.event_sink.on_provider_failure(
                                primary_provider,
                                reason=exc.__class__.__name__,
                            )
                            primary_error = exc
                            # Primary failed.  If the backup is still in its
                            # initial sleep(h*), cancel it and re-launch without
                            # the delay.  If the backup is already executing
                            # the real request, let it continue.
                            if backup_task in pending and not backup_past_sleep:
                                backup_task.cancel()
                                await _safe_await_task(backup_task)
                                pending.discard(backup_task)
                                backup_task = asyncio.ensure_future(_run_backup_immediate())
                                pending.add(backup_task)
                        else:
                            self.event_sink.on_provider_failure(
                                backup_provider,
                                reason=exc.__class__.__name__,
                            )
                    else:
                        # Winner found -- cancel the loser.
                        winner_result = task.result()
                        if task is primary_task:
                            self.event_sink.on_provider_success(primary_provider)
                            # self.config stays as primary.config (already correct).
                            backup_task.cancel()
                            await _safe_await_task(backup_task)
                        else:
                            self.event_sink.on_provider_success(backup_provider)
                            # Swap config so BaseRouter attributes to real winner.
                            self.config = self.backup.config
                            primary_task.cancel()
                            await _safe_await_task(primary_task)
                        return winner_result

            # Both tasks completed with errors.
            assert primary_error is not None
            raise primary_error  # type: ignore[misc]
        except BaseException:
            # Clean up on unexpected exceptions (e.g. CancelledError from caller).
            for t in pending:
                t.cancel()
            for t in pending:
                await _safe_await_task(t)
            raise

    # ---------------------------------------------------------------
    # Streaming race
    # ---------------------------------------------------------------

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        """Race primary against delayed backup for streaming completion.

        Algorithm:
        1. Start primary stream immediately.
        2. After h* seconds (or immediately if primary errors), start backup.
        3. Pull chunks from active streams via asyncio tasks wrapping __anext__.
        4. First stream to yield non-empty content wins.
        5. Buffer pre-content chunks (role deltas); yield winner's buffer + rest.
        6. Close loser via aclose().
        7. Primary tiebreaker: if both produce content in same await, primary wins.
        """
        primary_provider = self.primary.config.provider
        backup_provider = self.backup.config.provider

        primary_gen: AsyncGenerator[str, None] | None = None
        backup_gen: AsyncGenerator[str, None] | None = None

        try:
            primary_gen = self.primary.stream_chat_completion(messages, **params)
            backup_gen = self.backup.stream_chat_completion(messages, **params)

            winner_gen: AsyncGenerator[str, None] | None = None
            winner_buffer: list[str] = []

            # Phase 1: race for first content chunk.
            winner_gen, _loser_gen, winner_buffer = await self._race_streams(
                primary_gen,
                backup_gen,
                primary_provider,
                backup_provider,
            )

            # After the race, self.config has been swapped to the winner's
            # config (backup.config if backup won).  Update req_ctx so that
            # completions.py caches the correct provider/endpoint_id on the
            # first chunk it reads from us.
            winner_endpoint_id = getattr(self.config, "endpoint_id", None)
            winner_base_url = getattr(self.config, "base_url", None)
            req_ctx.update(
                {
                    "provider": self.config.provider,
                    "endpoint_id": winner_endpoint_id,
                    "base_url": winner_base_url,
                }
            )

            # Phase 2: yield buffered chunks from winner.
            for chunk in winner_buffer:
                yield chunk

            # Phase 3: yield remaining chunks from winner.
            async for chunk in winner_gen:
                yield chunk

        finally:
            # Close both generators.
            if primary_gen is not None:
                await _safe_aclose(primary_gen)
            if backup_gen is not None:
                await _safe_aclose(backup_gen)

    async def _race_streams(
        self,
        primary_gen: AsyncGenerator[str, None],
        backup_gen: AsyncGenerator[str, None],
        primary_provider: str,
        backup_provider: str,
    ) -> tuple[AsyncGenerator[str, None], AsyncGenerator[str, None] | None, list[str]]:
        """Race two streams, returning (winner_gen, loser_gen, winner_buffer).

        The winner is the first stream to produce a chunk with non-empty
        content.  If primary produces content in the same batch as backup,
        primary wins (tiebreaker).
        """
        primary_buffer: list[str] = []
        backup_buffer: list[str] = []
        primary_done = False
        backup_started = False
        primary_error: BaseException | None = None
        hedge_timer_task: asyncio.Task[None] | None = None

        # Create a timer task for starting the backup.
        async def _hedge_timer() -> None:
            await asyncio.sleep(self.hedge_threshold_sec)

        hedge_timer_task = asyncio.ensure_future(_hedge_timer())

        primary_next_task: asyncio.Task[str] | None = None
        backup_next_task: asyncio.Task[str] | None = None

        try:
            # Start pulling from primary immediately.
            primary_next_task = asyncio.ensure_future(primary_gen.__anext__())

            while True:
                wait_set: set[asyncio.Task[Any]] = set()
                if primary_next_task is not None:
                    wait_set.add(primary_next_task)
                if backup_next_task is not None:
                    wait_set.add(backup_next_task)
                if hedge_timer_task is not None:
                    wait_set.add(hedge_timer_task)

                if not wait_set:
                    break

                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                # Process hedge timer.
                if hedge_timer_task in done:
                    hedge_timer_task = None
                    if not backup_started and not primary_done:
                        backup_started = True
                        backup_next_task = asyncio.ensure_future(backup_gen.__anext__())

                # Check for primary content.
                primary_has_content = False
                if primary_next_task in done:
                    try:
                        chunk = primary_next_task.result()
                        primary_buffer.append(chunk)
                        if _has_non_empty_content(chunk):
                            primary_has_content = True
                    except StopAsyncIteration:
                        primary_done = True
                        primary_next_task = None
                    except Exception as e:
                        primary_error = e
                        primary_done = True
                        primary_next_task = None
                        self.event_sink.on_provider_failure(
                            primary_provider, reason=e.__class__.__name__
                        )
                        # Start backup immediately if not already running.
                        if not backup_started:
                            backup_started = True
                            if hedge_timer_task is not None:
                                hedge_timer_task.cancel()
                                hedge_timer_task = None
                            backup_next_task = asyncio.ensure_future(backup_gen.__anext__())
                        continue

                # Check for backup content.
                backup_has_content = False
                if backup_next_task is not None and backup_next_task in done:
                    try:
                        chunk = backup_next_task.result()
                        backup_buffer.append(chunk)
                        if _has_non_empty_content(chunk):
                            backup_has_content = True
                    except StopAsyncIteration:
                        backup_next_task = None
                    except Exception as e:
                        self.event_sink.on_provider_failure(
                            backup_provider, reason=e.__class__.__name__
                        )
                        backup_next_task = None

                # Decide winner.
                if primary_has_content and backup_has_content:
                    # Tiebreaker: primary wins.
                    self.event_sink.on_provider_success(primary_provider)
                    # self.config stays as primary.config (already correct).
                    _cancel_task(backup_next_task)
                    _cancel_task(hedge_timer_task)
                    return primary_gen, backup_gen, primary_buffer
                elif primary_has_content:
                    self.event_sink.on_provider_success(primary_provider)
                    _cancel_task(backup_next_task)
                    _cancel_task(hedge_timer_task)
                    return primary_gen, backup_gen, primary_buffer
                elif backup_has_content:
                    self.event_sink.on_provider_success(backup_provider)
                    # Swap config so BaseRouter attributes to real winner.
                    self.config = self.backup.config
                    _cancel_task(primary_next_task)
                    _cancel_task(hedge_timer_task)
                    return backup_gen, primary_gen, backup_buffer

                # No content yet; continue pulling from active streams.
                if primary_next_task in done and not primary_done:
                    primary_next_task = asyncio.ensure_future(primary_gen.__anext__())
                if backup_started and backup_next_task is not None and backup_next_task in done:
                    backup_next_task = asyncio.ensure_future(backup_gen.__anext__())

            # Both streams exhausted without content.
            # If primary had an error, raise it.
            if primary_error is not None:
                raise primary_error  # type: ignore[misc]

            # Return primary's buffer (even if empty -- no content from either).
            self.event_sink.on_provider_success(primary_provider)
            return primary_gen, backup_gen, primary_buffer

        except BaseException:
            _cancel_task(primary_next_task)
            _cancel_task(backup_next_task)
            _cancel_task(hedge_timer_task)
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _safe_await_task(task: asyncio.Task[Any]) -> None:
    """Await a task, suppressing any exception (CancelledError or otherwise)."""
    with contextlib.suppress(BaseException):
        await task


async def _safe_aclose(gen: AsyncGenerator[Any, None]) -> None:
    """Close an async generator, suppressing errors."""
    with contextlib.suppress(Exception):
        await gen.aclose()


def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel a task if it exists and is not done."""
    if task is not None and not task.done():
        task.cancel()
