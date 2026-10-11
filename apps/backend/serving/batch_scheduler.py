"""In-process batch scheduler.

A single asyncio task, started with the app, drains pending batch items through
the normal chat-completions path. Batch work is a *scavenger*: on each tick, for
each target model it decides whether the model is free of other callers and, if
so, grants the batch a bounded processing slice.

Per-model lease:

- **Others present** (any non-batch traffic on the model in the window) -> the
  batch yields fully; no new items launch.
- **No others** -> a slice of ``slice_seconds`` (default 15 min) opens; the
  batch runs at up to ``headroom_ratio`` (default 0.8) of the model's normal
  load.
- **Slice ends** -> a cooldown of one tick (default 10 min) releases the model
  before the next slice, so a real user can take it and the decision is
  re-evaluated.

A batch is deferred, not failed, when a model is yielding, cooling down, at its
headroom cap, or the owner is at their concurrency cap. It is only marked
terminal when every item has a terminal status, or its ``expires_at`` passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from serving.batch_dispatch import dispatch_chat_item
from serving.batch_gate import DEFAULT_HEADROOM_RATIO, LoadGate, ModelLoad, batch_may_run
from serving.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TICK_SECONDS = 600
DEFAULT_SLICE_SECONDS = 900
DEFAULT_MODEL_WIDTH = 4
DEFAULT_MAX_ITEM_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_BATCH_TTL_SECONDS = 24 * 3600


@dataclass
class ModelAvailability:
    """In-process model availability, updated by dispatch outcomes.

    Optimistic by default: a model is available until it has failed
    ``failure_threshold`` times in a row, at which point it cools down. A
    success clears the streak.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 300.0
    _failures: dict[str, int] = field(default_factory=dict)
    _down_until: dict[str, float] = field(default_factory=dict)

    def is_available(self, model: str) -> bool:
        """Whether the model is not cooling down."""
        return time.time() >= self._down_until.get(model, 0.0)

    def record_success(self, model: str) -> None:
        """Clear the model's failure streak and cooldown."""
        self._failures.pop(model, None)
        self._down_until.pop(model, None)

    def record_failure(self, model: str) -> None:
        """Record a failure; cool the model down after too many in a row."""
        count = self._failures.get(model, 0) + 1
        if count >= self.failure_threshold:
            self._down_until[model] = time.time() + self.cooldown_seconds
            self._failures[model] = 0
        else:
            self._failures[model] = count


@dataclass
class _Lease:
    """Per-model processing lease state."""

    active_until: float = 0.0
    cooldown_until: float = 0.0


class BatchScheduler:
    """Drains pending batch items on a timer, gated per model."""

    def __init__(
        self,
        *,
        app: Any,
        services: Any,
        store: Any,
        gate: LoadGate | None = None,
        tick_seconds: int = DEFAULT_TICK_SECONDS,
        slice_seconds: int = DEFAULT_SLICE_SECONDS,
        model_width: int = DEFAULT_MODEL_WIDTH,
        headroom_ratio: float = DEFAULT_HEADROOM_RATIO,
        max_item_attempts: int = DEFAULT_MAX_ITEM_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        availability: ModelAvailability | None = None,
    ) -> None:
        self._app = app
        self._services = services
        self._store = store
        self._gate = gate or LoadGate()
        self._tick_seconds = tick_seconds
        self._slice_seconds = slice_seconds
        self._model_width = model_width
        self._headroom_ratio = headroom_ratio
        self._max_item_attempts = max_item_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._availability = availability or ModelAvailability()
        self._model_semaphores: dict[str, asyncio.Semaphore] = {}
        self._leases: dict[str, _Lease] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Start the background task if it is not already running."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="batch_scheduler")

    async def stop(self) -> None:
        """Cancel the background task and wait for it to finish."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("batch scheduler tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)

    async def _tick(self) -> None:
        batches = await self._store.list_active_batches()
        if not batches:
            return
        models = {m for batch in batches for m in (batch.get("model_ids") or [])}
        allowed = await self._allowed_models(models)
        logger.debug(
            "batch scheduler tick",
            extra={"active_batches": len(batches), "allowed_models": sorted(allowed)},
        )
        for batch in batches:
            try:
                await self._process_batch(batch, allowed_models=allowed)
            except Exception:
                logger.exception("batch processing failed", extra={"batch_id": batch["id"]})

    async def _allowed_models(self, models: set[str]) -> set[str]:
        allowed: set[str] = set()
        for model in models:
            load = await self._gate.model_load(self._store.pool, model)
            if self._model_allowed(model, load):
                allowed.add(model)
        return allowed

    def _model_allowed(self, model: str, load: ModelLoad) -> bool:
        now = time.time()
        lease = self._leases.setdefault(model, _Lease())
        if now < lease.cooldown_until:
            return False
        if load.others_present:
            lease.active_until = 0.0  # yield fully; end any slice
            return False
        if lease.active_until == 0.0:
            lease.active_until = now + self._slice_seconds
        elif now >= lease.active_until:
            lease.active_until = 0.0
            lease.cooldown_until = now + self._tick_seconds
            return False
        return batch_may_run(load, headroom_ratio=self._headroom_ratio)

    async def _process_batch(self, batch: dict[str, Any], *, allowed_models: set[str]) -> None:
        batch_id = batch["id"]
        if batch["status"] == "cancelling":
            await self._store.cancel_pending_items(batch_id)
            await self._store.set_status(batch_id, "cancelled")
            return

        if self._expired(batch):
            await self._store.set_status(batch_id, "expired")
            return

        if batch["status"] == "validating":
            await self._store.set_status(batch_id, "in_progress")

        items = await self._store.fetch_runnable_items(
            batch_id, limit=self._model_width * max(len(batch.get("model_ids") or []), 1)
        )
        runnable = [
            it
            for it in items
            if it["model_id"] in allowed_models and self._availability.is_available(it["model_id"])
        ]
        if runnable:
            await asyncio.gather(*(self._run_item(batch, it) for it in runnable))

        counts = await self._store.refresh_counts(batch_id)
        if counts["open"] == 0:
            await self._store.set_status(batch_id, "completed")

    async def _run_item(self, batch: dict[str, Any], item: dict[str, Any]) -> None:
        model = item["model_id"]
        user_id = batch["user_id"]
        role = batch.get("role") or "free"
        is_admin = role == "admin"
        limiter = getattr(self._services, "user_concurrency_limiter", None)

        if limiter is not None:
            granted, _capacity, _label = await limiter.try_acquire(user_id, role, is_admin)
            if not granted:
                return  # owner at their concurrency cap; retry next tick
        try:
            semaphore = self._model_semaphores.setdefault(
                model, asyncio.Semaphore(self._model_width)
            )
            async with semaphore:
                await self._store.mark_item_running(item["id"])
                result = await dispatch_chat_item(
                    app=self._app,
                    services=self._services,
                    user_ctx={
                        "user_id": user_id,
                        "role": role,
                        "authenticated": True,
                        "batch_job_id": batch["id"],
                    },
                    body=self._item_body(batch, item),
                )
        finally:
            if limiter is not None:
                limiter.release(user_id)

        if result.error is not None:
            self._availability.record_failure(model)
            await self._handle_item_failure(item, result.error)
        else:
            self._availability.record_success(model)
            await self._store.mark_item_done(
                item["id"],
                response=result.response,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

    async def _handle_item_failure(self, item: dict[str, Any], error: dict[str, Any]) -> None:
        attempts = int(item.get("attempts") or 0) + 1
        if attempts >= self._max_item_attempts:
            await self._store.mark_item_done(item["id"], response=None, error=error)
            return
        delay = min(self._backoff_base * (2 ** (attempts - 1)), self._backoff_cap)
        delay += random.uniform(0, self._backoff_base)
        await self._store.requeue_item(
            item["id"],
            next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        )

    def _item_body(self, batch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body = dict(item["request"])
        body.setdefault("stream", False)
        metadata = body.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["batch_job_id"] = batch["id"]
        body["metadata"] = metadata
        return body

    def _expired(self, batch: dict[str, Any]) -> bool:
        expires = batch.get("expires_at")
        if not isinstance(expires, datetime):
            return False
        return datetime.now(timezone.utc) >= expires
