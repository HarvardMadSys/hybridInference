"""``CompletionsLogger`` — DB-log scheduling and routing-observation forwarding.

Replaces the module-level helpers that previously lived in
``apps/backend/serving/servers/routers/completions.py``:

- ``_schedule_db_log_task`` — fire-and-forget async write to the log store.
- ``_record_routing_observation`` — emit a ``RoutingObservation`` for online
  learning routers (RouteWise).
- ``_build_db_params`` — fill in the default ``max_tokens`` from the adapter
  config when the client didn't specify one.

The DB log payload (kwargs to ``LogStore.log_request``) is preserved
byte-for-byte — admin/recent-requests UI, billing reports, and the Slack
alert rules from PR #372 all read these rows and must keep working.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import enum
import time
from typing import Any

from routing.routers import RoutingObservation
from serving.observability.tracked_tasks import tracked_task
from serving.servers.routers.routing_info import RoutingInfo, merge_strategy_metadata
from serving.utils.logging import get_logger

logger = get_logger(__name__)

# Backpressure bounds for the background DB log queue (issue #133). The queue
# caps how many log payloads may sit in memory while Postgres is slow, and the
# drain timeout caps how long graceful shutdown waits for the last rows.
_LOG_QUEUE_MAXSIZE = 1024
# A small, fixed write pool--not one task per request, and not a second
# unbounded task factory. Four workers remove the single-writer throughput
# ceiling while leaving the bounded queue as the source of admission control.
_LOG_WORKERS = 4
_LOG_DRAIN_TIMEOUT_S = 10.0
_LOG_DROP_WARN_EVERY = 100
# Even when the cumulative count never reaches the next multiple of
# ``_LOG_DROP_WARN_EVERY``, a continuing loss episode must remain observable.
# This adds at most one time-based warning per interval; the first-drop and
# every-100th warnings still fire independently.
_LOG_DROP_WARN_INTERVAL_S = 60.0


class _Lifecycle(enum.Enum):
    """Admission states of the request-log queue.

    ``stop()`` is the only transition out of ``RUNNING``, and
    ``schedule_log`` admits payloads in exactly one of them. Without this
    the admission gate was "is there a worker task object" — undefined
    during shutdown (the queue was still accepting while ``stop`` waited
    on ``queue.join``, so late rows could arrive behind the drain that was
    supposed to flush them) and wrong after a worker crash (a dead
    consumer kept accepting).
    """

    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class CompletionsLogger:
    """Encapsulate fire-and-forget side effects from the chat-completions handler.

    Owns DB log writes and RouteWise observation forwarding. The instance keeps
    a private set of background tasks so the asyncio garbage collector cannot
    cancel them mid-flight.

    DB writes are admitted through one bounded queue drained by a small,
    fixed pool of ``_LOG_WORKERS`` (four) worker tasks per running loop
    (issue #133): a slow database cannot pile up one pending task per
    completed request, while the workers let writes overlap instead of
    making one database write a serialization point for the whole gateway.
    When the queue is full the OLDEST payload is dropped and the loss is
    logged loudly — it is never silent. ``stop`` drains the queue on
    graceful shutdown before the log store's pool is torn down, so queued
    report rows have a bounded chance to be written instead of being
    discarded on exit.

    Accounting boundary (issue #133 discussion): ``api_logs`` rows written
    here feed reports and analytics (billing, usage, and task/grant cost),
    not the quota-authoritative ledger. Quota enforcement reads
    ``user_daily_cost``. ``CostTracker.schedule_increment`` (chat), the
    embeddings surface's ``_schedule_cost_increment``, and the Anthropic
    messages path schedule those increments as separate background
    ``tracked_task`` writes on the request path, never through this queue.
    A payload dropped under saturation therefore loses an ``api_logs`` row
    while the separately scheduled quota increment is insulated from this
    queue's eviction; that increment is not guaranteed to commit, but it is
    not dropped here. The dropped row is also what usage and task/grant cost
    reports aggregate, so those reports undercount the dropped requests.
    The queue is a throughput optimization for report rows, not the quota
    ledger.
    """

    def __init__(self, *, log_store: Any) -> None:
        """Create a logger.

        Args:
            log_store: ``LogStore`` instance (Postgres).
                ``None``-tolerant: ``schedule_log`` no-ops when this is
                ``None``, so callers do not need to pre-gate.
        """
        self._log_store = log_store
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._queue: asyncio.Queue[tuple[str, dict[str, Any], contextvars.Context]] | None = None
        self._queue_loop: asyncio.AbstractEventLoop | None = None
        self._workers: tuple[asyncio.Task[None], ...] | None = None
        self._in_flight = 0
        self._stop_done: concurrent.futures.Future[None] | None = None
        self._dropped_count = 0
        self._last_drop_warn_at: float | None = None
        self._lifecycle: _Lifecycle = _Lifecycle.RUNNING

    # -- DB log scheduling ---------------------------------------------------

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        """Schedule a fire-and-forget write of ``log_data`` to the log store.

        ``log_data`` is forwarded verbatim as kwargs to
        ``log_store.log_request(**log_data)``. The handler is responsible
        for assembling the dict; this method only owns the asyncio
        scheduling so the row write doesn't block the HTTP response.

        The payload joins a bounded queue drained by a fixed pool of
        ``_LOG_WORKERS`` worker tasks (issue #133): pending payloads are
        capped by ``_LOG_QUEUE_MAXSIZE`` regardless of how slow the database
        is, and writes overlap within that pool instead of one task per
        request racing the connection pool. When the queue is full the
        OLDEST payload is dropped (the house pattern of
        ``AlertingLogHandler``) and the loss is logged loudly with a
        cumulative count — a dropped report row's loss is never silent.
        The write itself still runs as its own ``tracked_task`` so the
        ``tracked_task_completed`` failure event consumed by the alert
        engine keeps flowing exactly as before. Quota-authoritative
        increments are scheduled separately on the request path, not through
        this queue; rows dropped here do reduce reports later built from
        ``api_logs``.

        Admission is lifecycle-gated: after ``stop`` (or during it) the
        method refuses new payloads instead of queueing them behind a drain
        that can no longer represent them, and if a worker task has died
        unexpectedly the pending payloads are preserved and consumption is
        restarted over the same queue rather than left unconsumed.
        """
        if self._log_store is None:
            return
        if self._lifecycle is not _Lifecycle.RUNNING:
            logger.error(
                f"Request-log admission refused after shutdown: dropping payload "
                f"for {request_id} (lifecycle={self._lifecycle.value})",
                extra={
                    "event": "request_log_admission_refused",
                    "lifecycle": self._lifecycle.value,
                },
            )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop (e.g., synchronous context in tests).
            logger.debug(f"No event loop; dropping log payload for {request_id}")
            return

        workers = self._workers
        if workers is not None and any(worker.done() for worker in workers):
            # A worker can be cancelled by loop teardown or end outside the
            # per-row guard. The queue's pending payloads are still the only
            # copy of those rows, so preserve the queue and replace only the
            # dead consumers. If the pool died on another loop, restarting
            # here would hand that loop's queue to a second event loop; keep
            # the pending rows and refuse instead.
            if self._queue_loop is not loop:
                logger.error(
                    f"Request-log workers died on their original loop; refusing "
                    f"payload for {request_id} with {self._queue.qsize() if self._queue else 0} "
                    "pending rows preserved",
                    extra={"event": "request_log_worker_faulted"},
                )
                return
            logger.error(
                "Request-log worker terminated unexpectedly; restarting "
                f"consumption over {self._queue.qsize() if self._queue else 0} pending rows",
                extra={"event": "request_log_worker_faulted"},
            )
            self._workers = self._replace_dead_workers(workers)
        elif self._queue is None or self._queue_loop is not loop or not workers:
            # Lazy, per-loop pool. Plain tasks (not ``tracked_task``) because
            # their completion is observable through ``stop`` and must not end
            # a tracked-task window.
            self._queue = asyncio.Queue(maxsize=_LOG_QUEUE_MAXSIZE)
            self._queue_loop = loop
            self._workers = self._start_workers()

        # The producer's context, as a per-request task would have copied it:
        # the write and its log lines are attributed to this row's request.
        item = (request_id, log_data, contextvars.copy_context())
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Drop the oldest payload, keep the newest, and count the loss.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()  # the evicted payload never reaches the worker
                self._dropped_count += 1
                self._queue.put_nowait(item)
                now = time.monotonic()
                refresh_due = (
                    self._last_drop_warn_at is None
                    or now - self._last_drop_warn_at >= _LOG_DROP_WARN_INTERVAL_S
                )
                if (
                    self._dropped_count == 1
                    or self._dropped_count % _LOG_DROP_WARN_EVERY == 0
                    or refresh_due
                ):
                    self._last_drop_warn_at = now
                    logger.warning(
                        f"Request-log queue saturated; dropped oldest payload for {request_id} "
                        f"(total dropped: {self._dropped_count}, queue bound: "
                        f"{_LOG_QUEUE_MAXSIZE})",
                        extra={
                            "event": "request_log_queue_saturated",
                            "dropped_total": self._dropped_count,
                            "queue_maxsize": _LOG_QUEUE_MAXSIZE,
                        },
                    )

    def _start_workers(self) -> tuple[asyncio.Task[None], ...]:
        """Start the fixed worker pool on the current loop."""
        return tuple(self._spawn_worker(index) for index in range(_LOG_WORKERS))

    def _spawn_worker(self, index: int) -> asyncio.Task[None]:
        """Start one worker in an empty context.

        A worker outlives the request whose ``schedule_log`` happened to start
        it. Copying that request's context would stamp its request id, model
        and provider on every later row's log lines, and keep it alive for the
        life of the pool; each row's own context is restored per write instead.
        """
        worker = asyncio.create_task(
            self._drain_loop(),
            name=f"request_log_worker_{index}",
            context=contextvars.Context(),
        )
        self._background_tasks.add(worker)
        worker.add_done_callback(self._background_tasks.discard)
        return worker

    def _replace_dead_workers(
        self, workers: tuple[asyncio.Task[None], ...]
    ) -> tuple[asyncio.Task[None], ...]:
        """Replace dead consumers while preserving live tasks and the queue."""
        replaced = list(workers)
        for index, worker in enumerate(replaced):
            if not worker.done():
                continue
            replaced[index] = self._spawn_worker(index)
        return tuple(replaced)

    async def _drain_loop(self) -> None:
        """Drain queued payloads into ``log_store.log_request`` (issue #133).

        Each row write runs as its own ``tracked_task`` so the
        ``tracked_task_completed`` success/failure events consumed by
        ``TrackedTaskFailureRateRule`` keep flowing exactly as before the
        queue existed. A fixed set of these loops may run concurrently; the
        bounded queue, not worker count, remains the admission bound.
        """
        queue = self._queue
        if queue is None:  # pragma: no cover - schedule_log always creates it first
            return
        while True:
            request_id, log_data, context = await queue.get()
            self._in_flight += 1
            try:
                # Created inside the row's context, the write task runs in a
                # copy of it: its log lines and ``tracked_task_completed``
                # event carry this row's request, and nothing it sets reaches
                # the producer or the next row.
                write = context.run(
                    tracked_task, self._log_to_db(request_id, log_data), name="request_log"
                )
                await write
            finally:
                self._in_flight -= 1
                queue.task_done()

    async def _log_to_db(self, request_id: str, log_data: dict[str, Any]) -> None:
        try:
            await self._log_store.log_request(**log_data)
            logger.debug(f"Background DB logging completed for request {request_id}")
        except Exception as exc:
            logger.error(
                f"Background DB logging failed for request {request_id}: {exc}",
                exc_info=True,
            )
            raise  # let tracked_task record the failure

    async def stop(self, *, timeout: float = _LOG_DRAIN_TIMEOUT_S) -> None:
        """Drain queued log rows, then end the worker pool.

        Called from ``bootstrap.shutdown`` BEFORE the log store is torn down:
        queued report rows are written out (bounded by ``timeout``) instead of
        being lost when the process exits.

        Moves the lifecycle RUNNING → STOPPING before the first await
        (admission is refused from that point, so late writes cannot join a
        drain being flushed) and to STOPPED only after the pool has ended.
        A concurrent caller that finds STOPPING waits for the same shutdown
        instead of marking the logger STOPPED and returning early; a caller
        cancelled while waiting stops waiting, while the owner still finishes
        cleanup and releases the other waiters.

        On timeout, queued rows are discarded and writes already handed to the
        workers are cancelled. The two are reported separately because a
        cancelled write's commit status is uncertain, not known to be zero.
        """
        if self._lifecycle is _Lifecycle.STOPPED:
            return
        if self._lifecycle is _Lifecycle.STOPPING:
            done = self._stop_done
            if done is not None:
                # A concurrent caller, including one on another loop, waits
                # for the owner's cleanup. ``wrap_future`` is thread-safe and
                # ``shield`` keeps cancelling this waiter from cancelling the
                # shared completion future.
                await asyncio.shield(asyncio.wrap_future(done))
            return

        self._lifecycle = _Lifecycle.STOPPING
        done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._stop_done = done
        workers, queue = self._workers, self._queue
        if workers is None:
            workers = ()
        try:
            if workers and queue is not None:
                if self._queue_loop is asyncio.get_running_loop():
                    if queue.qsize() and any(worker.done() for worker in workers):
                        # A consumer can die without a later producer arriving
                        # to notice it. ``queue.join`` would then wait forever
                        # for rows nobody is left to consume, and the timeout
                        # path below would discard them. Replace only the dead
                        # consumers over the same queue before draining.
                        logger.error(
                            "Request-log worker terminated before drain; "
                            f"restarting consumption over {queue.qsize()} pending rows",
                            extra={
                                "event": "request_log_worker_faulted",
                                "pending_rows": queue.qsize(),
                            },
                        )
                        workers = self._replace_dead_workers(workers)
                        self._workers = workers
                    await asyncio.wait_for(queue.join(), timeout)
                else:
                    # The workers belong to a different (dead) loop; nothing can
                    # join them here. Just make sure they are not left running.
                    for worker in workers:
                        worker.cancel()
        except TimeoutError:
            queued = queue.qsize() if queue is not None else 0
            inflight = self._in_flight
            logger.error(
                f"Request-log drain timed out after {timeout}s; "
                f"discarded {queued} queued row(s); {inflight} in-flight "
                "write(s) were cancelled and their outcome is uncertain",
                extra={
                    "event": "request_log_drain_timeout",
                    "queued_rows_discarded": queued,
                    "inflight_writes_uncertain": inflight,
                },
            )
        except asyncio.CancelledError:
            raise
        finally:
            # Idle workers block on ``queue.get()``; end them on every path.
            # References are cleared only after the tasks have been awaited,
            # so a concurrent caller or a late ``schedule_log`` never sees a
            # half-retired pool. The completion future is published last,
            # once every waiter can observe the terminal STOPPED state.
            for worker in workers:
                worker.cancel()
            try:
                for worker in workers:
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker
            finally:
                # Publish the terminal state even if awaiting a foreign-loop
                # task raised: a waiter must not be stranded on the future.
                self._workers = None
                self._lifecycle = _Lifecycle.STOPPED
                self._stop_done = None
                done.set_result(None)

    # -- Routing observation forwarding -------------------------------------

    def record_routing_observation(
        self,
        active_router: Any,
        model_id: str,
        routing: RoutingInfo | dict[str, Any] | None,
        *,
        request_id: str,
        ttft_ms: float | None,
        total_latency_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        success: bool,
        cached_tokens: int | None = None,
    ) -> None:
        """Emit a ``RoutingObservation`` for online-learning routers.

        Accepts either a ``RoutingInfo`` (preferred) or the legacy untyped
        dict (e.g., ``getattr(exc, "_routing", None)``) so the handler's
        exception path — which today receives a raw dict from the adapter —
        keeps working without further plumbing changes. ``request_id`` is
        forwarded explicitly rather than recovered from ambient request context.

        ``cached_tokens`` is the authoritative observed cache usage from the
        provider (``None`` = unknown). It MUST NOT be synthesized: if the
        provider did not report usage, leave it as ``None``.
        """
        provider, endpoint_id, strategy_metadata = _extract_observation_keys(routing)
        terminal_key = endpoint_id or provider or "unknown"
        failed_attempts = _extract_failed_attempts(routing)
        if not success and failed_attempts:
            last_provider, last_endpoint = _extract_failed_attempt_keys(failed_attempts[-1])
            if (last_endpoint or last_provider or "unknown") == terminal_key:
                # The router lists the final attempt among the failed ones as
                # well as naming it the endpoint: it is the terminal failure
                # below, and reported twice it would count as two.
                failed_attempts = failed_attempts[:-1]
        seen_failed_attempts: set[tuple[str, str | None, str | None]] = set()
        for attempt in failed_attempts:
            failed_provider, failed_endpoint = _extract_failed_attempt_keys(attempt)
            failed_key = failed_endpoint or failed_provider or "unknown"
            dedupe_key = (
                failed_key,
                _string_or_none(attempt.get("error_type")),
                _string_or_none(attempt.get("error")),
            )
            if dedupe_key in seen_failed_attempts:
                continue
            seen_failed_attempts.add(dedupe_key)
            active_router.record_observation(
                RoutingObservation(
                    model_id=model_id,
                    endpoint_id=failed_key,
                    ttft_ms=None,
                    total_latency_ms=0.0,
                    token_count=prompt_tokens,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    success=False,
                    request_id=request_id,
                    terminal=False,
                    cached_tokens=None,
                    strategy_metadata={"routewise": {"quota_committed": 0.0}},
                )
            )

        obs = RoutingObservation(
            model_id=model_id,
            endpoint_id=terminal_key,
            ttft_ms=ttft_ms,
            total_latency_ms=total_latency_ms,
            token_count=prompt_tokens + completion_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=success,
            request_id=request_id,
            terminal=True,
            cached_tokens=cached_tokens,
            strategy_metadata=strategy_metadata,
        )
        active_router.record_observation(obs)

    # -- DB params builder --------------------------------------------------

    def build_db_params(
        self,
        params: dict[str, Any],
        provider: str,
        base_url: str | None,
        get_adapter_config_for_provider: Any,
    ) -> dict[str, Any]:
        """Reconstruct request params for the DB log row.

        Fills the default ``max_tokens`` from the matching adapter config
        when the client didn't supply one. ``get_adapter_config_for_provider``
        is the existing closure from the handler scope (it knows the chosen
        ``model``) — passed in so this stays a pure helper.

        The function returns ``None`` for unregistered providers (e.g., the
        synthetic ``"router"`` placeholder); ``getattr(None, "x", default)``
        raises ``AttributeError`` rather than returning the default, so we
        guard explicitly before reading the attribute.
        """
        _params = dict(params)
        if _params.get("max_tokens") is None:
            config = get_adapter_config_for_provider(provider, base_url)
            if config is not None:
                _params["max_tokens"] = getattr(config, "max_output_length", None)
        return _params


def _extract_observation_keys(
    routing: RoutingInfo | dict[str, Any] | None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Return ``(provider, endpoint_id, strategy_metadata)`` from either type.

    Mirrors the legacy lookup precedence the handler used:
    ``endpoint_id`` > ``base_url`` > ``provider`` for the observation key.
    """
    if routing is None:
        return None, None, {}
    if isinstance(routing, RoutingInfo):
        endpoint = routing.endpoint_id or routing.base_url
        return routing.provider, endpoint, dict(routing.strategy_metadata or {})
    # Legacy dict shape (used by exception._routing).
    endpoint = routing.get("endpoint_id") or routing.get("base_url")
    strategy_metadata: dict[str, Any] = {}
    raw_strategy_metadata = routing.get("strategy_metadata")
    if isinstance(raw_strategy_metadata, dict):
        strategy_metadata = merge_strategy_metadata(strategy_metadata, raw_strategy_metadata)
    routewise = routing.get("routewise")
    if isinstance(routewise, dict):
        strategy_metadata = merge_strategy_metadata(strategy_metadata, {"routewise": routewise})
    return (
        routing.get("provider"),
        endpoint,
        strategy_metadata,
    )


def _extract_failed_attempts(
    routing: RoutingInfo | dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    if routing is None:
        return ()
    if isinstance(routing, RoutingInfo):
        value = routing.extra.get("failed_attempts")
    else:
        value = routing.get("failed_attempts")
    if not isinstance(value, list):
        return ()
    return tuple(attempt for attempt in value if isinstance(attempt, dict))


def _extract_failed_attempt_keys(
    attempt: dict[str, Any],
) -> tuple[str | None, str | None]:
    provider = _string_or_none(attempt.get("provider"))
    endpoint = _string_or_none(attempt.get("endpoint_id")) or _string_or_none(
        attempt.get("base_url")
    )
    return provider, endpoint


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
