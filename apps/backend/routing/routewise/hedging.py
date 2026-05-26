"""Runtime hedge dispatch support for latency-aware routing."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

from routing.routers import _has_non_empty_content, _routing_chunk
from serving.adapters.base import BaseAdapter
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class HedgeBackupUnavailable(RuntimeError):
    """Raised when a planned backup cannot reserve state at dispatch time."""


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
        backup_start_hook: Callable[[], bool] | None = None,
        backup_finish_hook: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(primary.config)  # BaseRouter reads primary's config
        self.primary = primary
        self.backup = backup
        self.hedge_threshold_sec = hedge_threshold_sec
        self.event_sink = event_sink
        self.backup_start_hook = backup_start_hook
        self.backup_finish_hook = backup_finish_hook
        self.hedge_triggered = False
        self.backup_won = False
        self._stream_backup_started = False

    def _start_backup(self) -> bool:
        if self.backup_start_hook is not None and not self.backup_start_hook():
            raise HedgeBackupUnavailable("planned hedge backup is no longer feasible")
        self.hedge_triggered = True
        return True

    def _finish_backup(self, started: bool) -> None:
        if started and self.backup_finish_hook is not None:
            self.backup_finish_hook()

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
            started = self._start_backup()
            try:
                return await self.backup.chat_completion(messages, **params)
            finally:
                self._finish_backup(started)

        async def _run_backup_immediate() -> dict[str, Any]:
            nonlocal backup_past_sleep
            backup_past_sleep = True
            started = self._start_backup()
            try:
                return await self.backup.chat_completion(messages, **params)
            finally:
                self._finish_backup(started)

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
                            if not isinstance(exc, HedgeBackupUnavailable):
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
                            self.backup_won = True
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
            if self.backup_won:
                yield _routing_chunk(self)

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
            self._finish_backup(self._stream_backup_started)
            self._stream_backup_started = False

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
                        try:
                            self._stream_backup_started = self._start_backup()
                        except HedgeBackupUnavailable:
                            self._stream_backup_started = False
                        else:
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
                            if hedge_timer_task is not None:
                                hedge_timer_task.cancel()
                                hedge_timer_task = None
                            try:
                                self._stream_backup_started = self._start_backup()
                            except HedgeBackupUnavailable:
                                self._stream_backup_started = False
                            else:
                                backup_started = True
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
                        if not isinstance(e, HedgeBackupUnavailable):
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
                    self.backup_won = True
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
