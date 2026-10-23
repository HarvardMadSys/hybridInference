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
import contextlib
import enum
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
_LOG_DRAIN_TIMEOUT_S = 10.0
_LOG_DROP_WARN_EVERY = 100


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

    DB writes are admitted through one bounded queue drained by a single
    worker task per running loop (issue #133): a slow database cannot pile up
    one pending task per completed request, and the worker serializes writes
    instead of racing the whole pool. When the queue is full the OLDEST
    payload is dropped and the loss is logged loudly — it is never silent.
    ``stop`` drains the queue on graceful shutdown before the log store's
    pool is torn down, so billing-critical rows queued in memory survive.

    Accounting boundary (issue #133 discussion): ``api_logs`` rows written
    here are billing *reports and analytics*, not the quota-authoritative
    ledger. Quota enforcement reads ``user_daily_cost``, incremented inline
    by ``CostTracker.schedule_increment`` (chat), the embeddings surface's
    ``_schedule_cost_increment``, and the Anthropic messages path — each as
    its own ``tracked_task``, on the request path, never through this
    queue. A payload dropped under saturation therefore loses an
    analytics/report row while the caller's quota spend stays recorded.
    The queue is a throughput optimization over that ledger, never the
    only surviving copy of an accounting fact.
    """

    def __init__(self, *, log_store: Any, model_router_registry: Any | None = None) -> None:
        """Create a logger.

        Args:
            log_store: ``LogStore`` instance (Postgres).
                ``None``-tolerant: ``schedule_log`` no-ops when this is
                ``None``, so callers do not need to pre-gate.
            model_router_registry: Accepted for backward-compat with
                bootstrap; currently unused. ``build_db_params`` resolves
                adapter configs via the ``get_adapter_config_for_provider``
                closure passed in by the handler scope rather than via this
                registry.
        """
        self._log_store = log_store
        # NOTE: ``model_router_registry`` is intentionally not stored — the
        # field was unused and ``build_db_params`` takes the lookup as an
        # argument. Constructor still accepts the kwarg for API stability.
        del model_router_registry
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._queue_loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task[None] | None = None
        self._dropped_count = 0
        self._lifecycle: _Lifecycle = _Lifecycle.RUNNING

    # -- DB log scheduling ---------------------------------------------------

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        """Schedule a fire-and-forget write of ``log_data`` to the log store.

        ``log_data`` is forwarded verbatim as kwargs to
        ``log_store.log_request(**log_data)``. The handler is responsible
        for assembling the dict; this method only owns the asyncio
        scheduling so the row write doesn't block the HTTP response.

        The payload joins a bounded queue drained by a single worker task
        (issue #133): pending payloads are capped by ``_LOG_QUEUE_MAXSIZE``
        regardless of how slow the database is, and writes are serialized
        instead of one task per request racing the connection pool. When the
        queue is full the OLDEST payload is dropped (the house pattern of
        ``AlertingLogHandler``) and the loss is logged loudly with a
        cumulative count — a billing-critical row is never dropped silently.
        The write itself still runs as its own ``tracked_task`` so the
        ``tracked_task_completed`` failure event consumed by the alert
        engine keeps flowing exactly as before.

        Admission is lifecycle-gated: after ``stop`` (or during it) the
        method refuses new payloads instead of queueing them behind a drain
        that can no longer represent them, and if the worker task has died
        unexpectedly the pending payloads are preserved and consumption is
        restarted exactly once rather than left unconsumed.
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

        if self._worker is not None and self._worker.done():
            # The consumer died (cancelled by a dead loop, an unexpected
            # exception outside the per-row guard, a loop teardown). The
            # queue's pending payloads are still the only copy of those
            # rows — preserve them and restart consumption over the SAME
            # queue, exactly once, so two consumers can never drain one
            # queue concurrently. If the worker died on a different loop
            # than this one, restarting on this loop would hand a second
            # loop a queue it cannot share; keep the pending rows and
            # refuse instead.
            if self._queue_loop is not loop:
                logger.error(
                    f"Request-log worker died on its original loop; refusing "
                    f"payload for {request_id} with {self._queue.qsize()} "
                    "pending rows preserved",
                    extra={"event": "request_log_worker_faulted"},
                )
                return
            logger.error(
                "Request-log worker terminated unexpectedly; restarting "
                f"consumption over {self._queue.qsize()} pending rows",
                extra={"event": "request_log_worker_faulted"},
            )
            self._worker = asyncio.create_task(self._drain_loop(), name="request_log_worker")
            self._background_tasks.add(self._worker)
            self._worker.add_done_callback(self._background_tasks.discard)
        elif self._queue is None or self._queue_loop is not loop or self._worker is None:
            # Lazy, per-loop worker. One worker serializes the writes; a plain
            # task (not ``tracked_task``) because its completion is observable
            # through ``stop``, and it must not end a tracked-task window.
            self._queue = asyncio.Queue(maxsize=_LOG_QUEUE_MAXSIZE)
            self._queue_loop = loop
            self._worker = asyncio.create_task(self._drain_loop(), name="request_log_worker")
            self._background_tasks.add(self._worker)
            self._worker.add_done_callback(self._background_tasks.discard)

        item = (request_id, log_data)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Drop the oldest payload, keep the newest, and count the loss.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()  # the evicted payload never reaches the worker
                self._dropped_count += 1
                self._queue.put_nowait(item)
            if self._dropped_count == 1 or self._dropped_count % _LOG_DROP_WARN_EVERY == 0:
                logger.warning(
                    f"Request-log queue saturated; dropped oldest payload for {request_id} "
                    f"(total dropped: {self._dropped_count}, queue bound: {_LOG_QUEUE_MAXSIZE})",
                    extra={
                        "event": "request_log_queue_saturated",
                        "dropped_total": self._dropped_count,
                        "queue_maxsize": _LOG_QUEUE_MAXSIZE,
                    },
                )

    async def _drain_loop(self) -> None:
        """Serialize queued payloads into ``log_store.log_request`` (issue #133).

        Each row write runs as its own ``tracked_task`` so the
        ``tracked_task_completed`` success/failure events consumed by
        ``TrackedTaskFailureRateRule`` keep flowing exactly as before the
        queue existed.
        """
        queue = self._queue
        if queue is None:  # pragma: no cover - schedule_log always creates it first
            return
        while True:
            request_id, log_data = await queue.get()
            try:
                await tracked_task(self._log_to_db(request_id, log_data), name="request_log")
            finally:
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
        """Drain queued log rows, then end the worker. Idempotent.

        Called from ``bootstrap.shutdown`` BEFORE the log store is torn down:
        billing-critical rows queued in memory are written out (bounded by
        ``timeout``) instead of being lost when the process exits.

        Moves the lifecycle RUNNING → STOPPING before draining (admission
        is refused from the first await of this method, so late writes can
        no longer join the drain being flushed) and to STOPPED once the
        worker has actually ended — the worker reference is cleared only
        after its task has been awaited to completion, never while a
        producer could still observe it.
        """
        if self._lifecycle is not _Lifecycle.RUNNING:
            # Idempotent re-entry (or shutdown racing shutdown): nothing
            # left to do that the first call did not do.
            self._lifecycle = _Lifecycle.STOPPED
            return
        self._lifecycle = _Lifecycle.STOPPING
        worker, queue = self._worker, self._queue
        if worker is None or queue is None:
            self._lifecycle = _Lifecycle.STOPPED
            return
        try:
            if self._queue_loop is asyncio.get_running_loop():
                await asyncio.wait_for(queue.join(), timeout)
            else:
                # The worker belongs to a different (dead) loop; nothing can
                # join it here. Just make sure it is not left running.
                worker.cancel()
        except asyncio.TimeoutError:
            logger.error(
                f"Request-log drain timed out after {timeout}s; "
                f"{queue.qsize()} queued payloads discarded"
            )
        except asyncio.CancelledError:
            self._lifecycle = _Lifecycle.STOPPED
            raise
        finally:
            # The worker blocks on ``queue.get()`` once idle; end it on every
            # path. On the timeout path a write the worker was awaiting is
            # cancelled with the worker and aborts (its row is part of the
            # discarded count logged above). The reference is cleared only
            # after the task has been awaited, so a concurrent ``stop`` or a
            # late ``schedule_log`` never sees a half-retired worker.
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self._worker = None
            self._lifecycle = _Lifecycle.STOPPED

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
        failed_attempts = _extract_failed_attempts(routing)
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

        provider, endpoint_id, strategy_metadata = _extract_observation_keys(routing)
        obs = RoutingObservation(
            model_id=model_id,
            endpoint_id=endpoint_id or provider or "unknown",
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
