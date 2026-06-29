"""Runtime hedge dispatch support for latency-aware routing."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

from routewise.core import CheckpointBackupDispatch, CheckpointBackupSelector

from routing.routers import _has_non_empty_content, _routing_chunk
from serving.adapters.base import BaseAdapter
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)

_STREAM_RACE_BUFFER_MAX_BYTES = 1_000_000
_STREAM_RACE_DEADLINE_SECONDS = 60.0


class HedgeBackupUnavailable(RuntimeError):
    """Raised when a planned backup cannot reserve state at dispatch time."""


class HedgeStreamRaceTimeout(TimeoutError):
    """Raised when no stream produces race-winning output before the deadline."""


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
    it launches the primary immediately and evaluates checkpoint backup
    selectors until one returns a concrete backup dispatch. The first provider
    to produce a result wins; the loser is cancelled.

    Per-provider outcomes are reported to ``event_sink`` so that circuit
    breakers and health tracking see individual provider results.

    Attributes:
        primary: The primary adapter (launched immediately).
        backup: Optional fixed backup adapter for legacy/direct callers.
        hedge_threshold_sec: Delay for a fixed backup adapter.
        stream_race_deadline_sec: Maximum time to wait for race-winning output.
        backup_release: Optional reservation release for fixed backup callers.
        event_sink: Callback for per-provider health reporting.
    """

    def __init__(
        self,
        primary: BaseAdapter,
        backup: BaseAdapter | None = None,
        hedge_threshold_sec: float | None = None,
        event_sink: ProviderEventSink | None = None,
        backup_start_hook: Callable[[], bool] | None = None,
        backup_release: Callable[[], None] | None = None,
        checkpoint_backup_selector: CheckpointBackupSelector[BaseAdapter] | None = None,
        hedge_checkpoints_sec: Sequence[float] = (),
        stream_race_deadline_sec: float | None = _STREAM_RACE_DEADLINE_SECONDS,
    ) -> None:
        if event_sink is None:
            raise TypeError("event_sink is required")
        if checkpoint_backup_selector is None:
            if backup is None:
                raise TypeError(
                    "backup is required when checkpoint_backup_selector is not provided"
                )
            if hedge_threshold_sec is None:
                raise TypeError(
                    "hedge_threshold_sec is required when checkpoint_backup_selector "
                    "is not provided"
                )

        super().__init__(primary.config)  # BaseRouter reads primary's config
        self.primary = primary
        self.backup = backup
        self.hedge_threshold_sec = (
            float(hedge_threshold_sec)
            if hedge_threshold_sec is not None
            else _first_checkpoint(hedge_checkpoints_sec)
        )
        self.event_sink = event_sink
        self.backup_start_hook = backup_start_hook
        self.backup_release = backup_release
        self.checkpoint_backup_selector = checkpoint_backup_selector
        self.hedge_checkpoints_sec = _normalize_checkpoints(
            hedge_checkpoints_sec
            if checkpoint_backup_selector is not None
            else (self.hedge_threshold_sec,)
        )
        self.hedge_triggered = False
        self.backup_won = False
        self.hedge_delay_sec: float | None = None
        self.hedge_success_probability: float | None = None
        self.failed_attempts: list[dict[str, str]] = []
        self._stream_backup_dispatch: CheckpointBackupDispatch[BaseAdapter] | None = None
        self._stream_backup_gen: AsyncGenerator[str, None] | None = None
        self.stream_race_deadline_sec = (
            None if stream_race_deadline_sec is None else max(0.0, float(stream_race_deadline_sec))
        )

    def _start_backup_at(
        self,
        elapsed_sec: float,
    ) -> CheckpointBackupDispatch[BaseAdapter] | None:
        checkpoint_ts = time.time()
        if self.checkpoint_backup_selector is not None:
            try:
                dispatch = self.checkpoint_backup_selector(
                    float(elapsed_sec),
                    checkpoint_ts,
                )
            except Exception:
                logger.warning("checkpoint backup selector raised", exc_info=True)
                return None
        else:
            if self.backup is None:
                return None
            if self.backup_start_hook is not None and not self.backup_start_hook():
                return None
            dispatch = CheckpointBackupDispatch(
                backup=self.backup,
                elapsed_sec=float(elapsed_sec),
                release=self.backup_release,
            )

        if dispatch is None:
            return None
        self.backup = dispatch.backup
        self.hedge_triggered = True
        self.hedge_delay_sec = dispatch.elapsed_sec
        self.hedge_success_probability = dispatch.success_probability
        return dispatch

    def _finish_backup(
        self,
        dispatch: CheckpointBackupDispatch[BaseAdapter] | None,
    ) -> None:
        if dispatch is not None and dispatch.release is not None:
            dispatch.release()

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

        # Tracks whether the backup task has progressed past its initial
        # sleep(h*) delay.  When primary fails, we only cancel+relaunch the
        # backup if it is still sleeping; if the real request is already
        # in-flight, cancelling it would waste an otherwise-useful attempt.
        backup_past_sleep = False

        async def _run_primary() -> dict[str, Any]:
            return await self.primary.chat_completion(messages, **params)

        async def _run_backup_delayed() -> dict[str, Any]:
            nonlocal backup_past_sleep
            loop = asyncio.get_running_loop()
            schedule_start = loop.time()
            for checkpoint_sec in self.hedge_checkpoints_sec:
                wait_remaining = schedule_start + checkpoint_sec - loop.time()
                if wait_remaining > 0.0:
                    await asyncio.sleep(wait_remaining)
                dispatch = self._start_backup_at(checkpoint_sec)
                if dispatch is None:
                    continue
                backup_past_sleep = True
                try:
                    return await dispatch.backup.chat_completion(messages, **params)
                finally:
                    self._finish_backup(dispatch)
            raise HedgeBackupUnavailable("no checkpoint hedge backup selected")

        async def _run_backup_immediate() -> dict[str, Any]:
            nonlocal backup_past_sleep
            elapsed_sec = max(
                0.0,
                asyncio.get_running_loop().time() - schedule_start,
            )
            dispatch = self._start_backup_at(elapsed_sec)
            if dispatch is None:
                raise HedgeBackupUnavailable("no checkpoint hedge backup selected")
            backup_past_sleep = True
            try:
                return await dispatch.backup.chat_completion(messages, **params)
            finally:
                self._finish_backup(dispatch)

        schedule_start = asyncio.get_running_loop().time()
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
                            self.failed_attempts.append(_failed_attempt(self.primary, exc))
                            primary_error = exc
                            # Primary failed.  If the backup is still in its
                            # initial sleep(h*), cancel it and re-launch without
                            # the delay.  If the backup is already executing
                            # the real request, let it continue.
                            if not backup_past_sleep:
                                if backup_task in pending:
                                    backup_task.cancel()
                                    await _safe_await_task(backup_task)
                                    pending.discard(backup_task)
                                backup_task = asyncio.ensure_future(_run_backup_immediate())
                                pending.add(backup_task)
                        else:
                            if not isinstance(exc, HedgeBackupUnavailable):
                                backup_provider = _provider_name_from_adapter(self.backup)
                                self.event_sink.on_provider_failure(
                                    backup_provider,
                                    reason=exc.__class__.__name__,
                                )
                                self.failed_attempts.append(_failed_attempt(self.backup, exc))
                    else:
                        # Winner found -- cancel the loser.
                        winner_result = task.result()
                        if task is primary_task:
                            self.event_sink.on_provider_success(primary_provider)
                            # self.config stays as primary.config (already correct).
                            backup_task.cancel()
                            await _safe_await_task(backup_task)
                        else:
                            backup_provider = _provider_name_from_adapter(self.backup)
                            self.event_sink.on_provider_success(backup_provider)
                            # Swap config so BaseRouter attributes to real winner.
                            assert self.backup is not None
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
        2. At checkpoints (or immediately if primary errors), ask for a backup.
        3. Pull chunks from active streams via asyncio tasks wrapping __anext__.
        4. First stream to yield non-empty content wins.
        5. Buffer pre-content chunks (role deltas); yield winner's buffer + rest.
        6. Close loser via aclose().
        7. Primary tiebreaker: if both produce content in same await, primary wins.
        """
        primary_provider = self.primary.config.provider

        primary_gen: AsyncGenerator[str, None] | None = None
        winner_gen: AsyncGenerator[str, None] | None = None
        loser_gen: AsyncGenerator[str, None] | None = None

        try:
            primary_gen = self.primary.stream_chat_completion(messages, **params)

            winner_buffer: list[str] = []

            # Phase 1: race for first content chunk.
            winner_gen, loser_gen, winner_buffer = await self._race_streams(
                primary_gen,
                primary_provider,
                messages,
                params,
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
            # Close every generator we may have opened.
            seen: set[int] = set()
            for gen in (primary_gen, winner_gen, loser_gen, self._stream_backup_gen):
                if gen is not None and id(gen) not in seen:
                    seen.add(id(gen))
                    await _safe_aclose(gen)
            self._finish_backup(self._stream_backup_dispatch)
            self._stream_backup_dispatch = None
            self._stream_backup_gen = None

    async def _race_streams(
        self,
        primary_gen: AsyncGenerator[str, None],
        primary_provider: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> tuple[AsyncGenerator[str, None], AsyncGenerator[str, None] | None, list[str]]:
        """Race two streams, returning (winner_gen, loser_gen, winner_buffer).

        The winner is the first stream to produce a chunk with non-empty
        content.  If primary produces content in the same batch as backup,
        primary wins (tiebreaker).
        """
        primary_buffer: list[str] = []
        backup_buffer: list[str] = []
        primary_buffer_bytes = 0
        backup_buffer_bytes = 0
        primary_done = False
        backup_started = False
        backup_gen: AsyncGenerator[str, None] | None = None
        backup_provider: str | None = None
        primary_error: BaseException | None = None
        checkpoint_index = 0
        schedule_start = asyncio.get_running_loop().time()
        race_deadline_sec = self.stream_race_deadline_sec

        async def _checkpoint_timer(elapsed_sec: float) -> float:
            wait_remaining = schedule_start + elapsed_sec - asyncio.get_running_loop().time()
            if wait_remaining > 0.0:
                await asyncio.sleep(wait_remaining)
            return elapsed_sec

        def _next_checkpoint_task() -> asyncio.Task[float] | None:
            nonlocal checkpoint_index
            if checkpoint_index >= len(self.hedge_checkpoints_sec):
                return None
            elapsed_sec = self.hedge_checkpoints_sec[checkpoint_index]
            checkpoint_index += 1
            return asyncio.ensure_future(_checkpoint_timer(elapsed_sec))

        def _start_stream_backup(elapsed_sec: float) -> bool:
            nonlocal backup_gen, backup_provider, backup_started, backup_next_task
            dispatch = self._start_backup_at(elapsed_sec)
            if dispatch is None:
                return False
            backup_started = True
            self._stream_backup_dispatch = dispatch
            self._stream_backup_gen = dispatch.backup.stream_chat_completion(
                messages,
                **params,
            )
            backup_gen = self._stream_backup_gen
            backup_provider = dispatch.backup.config.provider
            backup_next_task = asyncio.ensure_future(backup_gen.__anext__())
            return True

        primary_next_task: asyncio.Task[str] | None = None
        backup_next_task: asyncio.Task[str] | None = None
        hedge_timer_task = _next_checkpoint_task()
        race_deadline_task = (
            asyncio.ensure_future(asyncio.sleep(race_deadline_sec))
            if race_deadline_sec is not None
            else None
        )

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

                if race_deadline_task is not None:
                    wait_set.add(race_deadline_task)

                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                race_deadline_expired = race_deadline_task in done

                # Process hedge timer.
                if hedge_timer_task in done:
                    elapsed_sec = hedge_timer_task.result()
                    hedge_timer_task = None
                    if (
                        not backup_started
                        and not primary_done
                        and not _start_stream_backup(elapsed_sec)
                    ):
                        hedge_timer_task = _next_checkpoint_task()

                # Check for primary content.
                primary_has_content = False
                if primary_next_task in done:
                    try:
                        chunk = primary_next_task.result()
                        primary_buffer.append(chunk)
                        primary_buffer_bytes += _chunk_buffer_size(chunk)
                        if _has_non_empty_content(chunk) or _stream_race_buffer_cap_reached(
                            primary_buffer_bytes,
                            leg="primary",
                        ):
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
                        self.failed_attempts.append(_failed_attempt(self.primary, e))
                        # Start backup immediately if not already running.
                        if not backup_started:
                            if hedge_timer_task is not None:
                                hedge_timer_task.cancel()
                                hedge_timer_task = None
                            elapsed_sec = max(
                                0.0,
                                asyncio.get_running_loop().time() - schedule_start,
                            )
                            _start_stream_backup(elapsed_sec)
                        continue

                # Check for backup content.
                backup_has_content = False
                if backup_next_task is not None and backup_next_task in done:
                    try:
                        chunk = backup_next_task.result()
                        backup_buffer.append(chunk)
                        backup_buffer_bytes += _chunk_buffer_size(chunk)
                        if _has_non_empty_content(chunk) or _stream_race_buffer_cap_reached(
                            backup_buffer_bytes,
                            leg="backup",
                        ):
                            backup_has_content = True
                    except StopAsyncIteration:
                        backup_next_task = None
                    except Exception as e:
                        if not isinstance(e, HedgeBackupUnavailable):
                            provider = backup_provider or _provider_name_from_adapter(self.backup)
                            self.event_sink.on_provider_failure(
                                provider, reason=e.__class__.__name__
                            )
                            self.failed_attempts.append(_failed_attempt(self.backup, e))
                        backup_next_task = None

                # Decide winner.
                if primary_has_content and backup_has_content:
                    # Tiebreaker: primary wins.
                    self.event_sink.on_provider_success(primary_provider)
                    # self.config stays as primary.config (already correct).
                    _cancel_task(backup_next_task)
                    _cancel_task(hedge_timer_task)
                    _cancel_task(race_deadline_task)
                    return primary_gen, backup_gen, primary_buffer
                elif primary_has_content:
                    self.event_sink.on_provider_success(primary_provider)
                    _cancel_task(backup_next_task)
                    _cancel_task(hedge_timer_task)
                    _cancel_task(race_deadline_task)
                    return primary_gen, backup_gen, primary_buffer
                elif backup_has_content:
                    provider = backup_provider or _provider_name_from_adapter(self.backup)
                    self.event_sink.on_provider_success(provider)
                    # Swap config so BaseRouter attributes to real winner.
                    assert self.backup is not None
                    self.config = self.backup.config
                    self.backup_won = True
                    _cancel_task(primary_next_task)
                    _cancel_task(hedge_timer_task)
                    _cancel_task(race_deadline_task)
                    assert backup_gen is not None
                    return backup_gen, primary_gen, backup_buffer

                if race_deadline_expired:
                    deadline_sec = race_deadline_sec if race_deadline_sec is not None else 0.0
                    exc = HedgeStreamRaceTimeout(
                        f"RouteWise stream race exceeded {deadline_sec:.3f}s before first output"
                    )
                    logger.warning(
                        "routewise_stream_race_deadline_exceeded",
                        extra={
                            "event": "routewise_stream_race_deadline_exceeded",
                            "deadline_sec": deadline_sec,
                            "primary_provider": primary_provider,
                            "backup_provider": backup_provider,
                            "primary_buffer_bytes": primary_buffer_bytes,
                            "backup_buffer_bytes": backup_buffer_bytes,
                        },
                    )
                    if not primary_done:
                        self.event_sink.on_provider_failure(
                            primary_provider, reason=exc.__class__.__name__
                        )
                        self.failed_attempts.append(_failed_attempt(self.primary, exc))
                    if backup_started and backup_next_task is not None:
                        provider = backup_provider or _provider_name_from_adapter(self.backup)
                        self.event_sink.on_provider_failure(provider, reason=exc.__class__.__name__)
                        self.failed_attempts.append(_failed_attempt(self.backup, exc))
                    raise exc

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
            _cancel_task(race_deadline_task)
            return primary_gen, backup_gen, primary_buffer

        except BaseException:
            _cancel_task(primary_next_task)
            _cancel_task(backup_next_task)
            _cancel_task(hedge_timer_task)
            _cancel_task(race_deadline_task)
            for task in (
                primary_next_task,
                backup_next_task,
                hedge_timer_task,
                race_deadline_task,
            ):
                if task is not None:
                    await _safe_await_task(task)
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


def _chunk_buffer_size(chunk: Any) -> int:
    if isinstance(chunk, bytes):
        return len(chunk)
    if isinstance(chunk, str):
        return len(chunk)
    return 0


def _stream_race_buffer_cap_reached(buffer_bytes: int, *, leg: str) -> bool:
    if buffer_bytes < _STREAM_RACE_BUFFER_MAX_BYTES:
        return False
    logger.warning(
        "routewise_stream_race_buffer_cap_reached",
        extra={
            "event": "routewise_stream_race_buffer_cap_reached",
            "leg": leg,
            "buffer_bytes": buffer_bytes,
            "limit_bytes": _STREAM_RACE_BUFFER_MAX_BYTES,
        },
    )
    return True


def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel a task if it exists and is not done."""
    if task is not None and not task.done():
        task.cancel()


def _normalize_checkpoints(checkpoints: Sequence[float]) -> tuple[float, ...]:
    return tuple(sorted(float(value) for value in checkpoints if float(value) >= 0.0))


def _first_checkpoint(checkpoints: Sequence[float]) -> float:
    normalized = _normalize_checkpoints(checkpoints)
    return normalized[0] if normalized else 0.0


def _provider_name_from_adapter(adapter: BaseAdapter | None) -> str:
    if adapter is None:
        return "unknown-backup"
    return str(adapter.config.provider)


def _endpoint_id_from_adapter(adapter: BaseAdapter | None) -> str:
    if adapter is None:
        return "unknown-backup"
    endpoint_id = getattr(adapter.config, "endpoint_id", None)
    return str(endpoint_id or adapter.config.provider)


def _failed_attempt(adapter: BaseAdapter | None, exc: BaseException) -> dict[str, str]:
    return {
        "provider": _provider_name_from_adapter(adapter),
        "endpoint_id": _endpoint_id_from_adapter(adapter),
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
