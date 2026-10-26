"""Completion callbacks: POST a batch's final status to the caller's URL.

The batch worker stores results, not notifications. When a batch finishes, the
row already says where its callback goes (``batch_jobs.webhook_url``); this
sender claims terminal batches whose webhook is still pending, builds the same
batch object ``GET /v1/batches/{id}`` returns, and posts it. A batch has one
callback, so its delivery state lives on the batch row rather than in a table of
its own.

Delivery is at least once. A process that dies after the receiver answered but
before recording it leaves the row ``delivering``; another process claims it once
the lease runs out and posts again. The caller should treat the callback as a
signal to read ``GET /v1/batches/{id}/results``, not as the only copy of data.

Attempts are three, spaced five and ten minutes apart, so a receiver that is
deploying for a few minutes still gets the event. The URL is caller-supplied, so
every attempt resolves it afresh and refuses a non-public target
(:mod:`serving.utils.webhook_target`).

Payloads are unsigned: a receiver confirms an event by reading the batch with
its own key. Signing is a possible follow-up if one needs to trust the payload
without calling back.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_chain,
    wait_fixed,
)

from serving.batch.config import batch_config
from serving.batch.identity import default_worker_id
from serving.batch.view import batch_object, epoch
from serving.utils.logging import get_logger
from serving.utils.secret_urls import posting_to, url_redaction
from serving.utils.webhook_target import UnsafeWebhookTarget, resolve_public_webhook

if TYPE_CHECKING:
    from serving.batch.config import BatchConfig

logger = get_logger(__name__)

WEBHOOK_EVENT = "batch.finished"
#: Attempts per callback, and the pauses between them: three attempts over 15
#: minutes.
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = (300.0, 600.0)
#: Longest a POST may take, connect and response body included.
TIMEOUT_SECONDS = 10.0


class WebhookDeliveryFailed(RuntimeError):
    """The receiver answered with a non-2xx status."""


class WebhookClaimLost(RuntimeError):
    """The delivery is no longer this worker's: the batch was deleted or retaken."""


class WebhookTargetRefused(RuntimeError):
    """The URL is not a target this gateway may post to; retrying will not help."""


@dataclass(frozen=True)
class WebhookTimings:
    """The sender's intervals and bounds. Durations are in seconds."""

    poll_seconds: float = 5.0
    #: Longest a claim holds a delivery before another process may take it over.
    #: Exceeds the whole retry window, so a live retry loop is never stolen.
    lease_seconds: int = 1200
    #: Deliveries one process runs at once; the rest wait for the next poll.
    max_in_flight: int = 8
    #: How long in-flight deliveries get to stop when the worker shuts down.
    stop_grace_seconds: float = 5.0
    max_attempts: int = MAX_ATTEMPTS
    retry_wait_seconds: tuple[float, ...] = RETRY_WAIT_SECONDS
    timeout_seconds: float = TIMEOUT_SECONDS


class WebhookSender:
    """Claims this process's share of pending completion callbacks and posts them."""

    def __init__(
        self,
        *,
        store: Any,
        worker_id: str | None = None,
        timings: WebhookTimings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Set the sender up; nothing runs until :meth:`start`.

        Args:
            store: The batch store (``serving.storage.batch_store.BatchStore``).
            worker_id: The name this process claims deliveries under; unique by
                default.
            timings: Intervals and bounds; the defaults by default.
            transport: HTTP transport, for tests; the default transport by default.
        """
        self._store = store
        self._worker_id = worker_id or default_worker_id()
        self._timings = timings or WebhookTimings()
        self._transport = transport
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self) -> None:
        """Start the loop. Idempotent while it runs."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="batch_webhooks", context=contextvars.Context()
        )

    async def stop(self) -> None:
        """Stop claiming, cancel in-flight retries and hand those back."""
        self._stopping = True
        self._wake.set()
        loop_task, self._task = self._task, None
        if loop_task is not None:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "batch webhook sender %s stopped",
            self._worker_id,
            extra={"event": "batch_webhooks_stopped"},
        )

    async def _run(self) -> None:
        logger.info(
            "batch webhook sender %s started",
            self._worker_id,
            extra={"event": "batch_webhooks_started"},
        )
        while not self._stopping:
            try:
                await self._round()
            except Exception:
                logger.exception(
                    "batch webhook round failed", extra={"event": "batch_webhooks_round_failed"}
                )
            if self._stopping:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._timings.poll_seconds)
            self._wake.clear()

    async def _round(self) -> None:
        room = self._timings.max_in_flight - len(self._tasks)
        if room <= 0:
            return
        rows = await self._store.claim_webhook_deliveries(
            worker_id=self._worker_id,
            limit=room,
            lease_seconds=self._timings.lease_seconds,
            max_attempts=self._timings.max_attempts,
        )
        for row in rows:
            batch_id = row["id"]
            task = asyncio.create_task(
                self._deliver(row),
                name=f"batch_webhook:{batch_id}",
                context=contextvars.Context(),
            )
            self._tasks[batch_id] = task
            task.add_done_callback(lambda _task, batch_id=batch_id: self._forget(batch_id))

    def _forget(self, batch_id: str) -> None:
        self._tasks.pop(batch_id, None)

    async def _deliver(self, row: dict[str, Any]) -> None:
        batch_id = row["id"]
        settled = False
        try:
            await self._deliver_attempts(row)
            settled = True
        finally:
            if not settled:
                with contextlib.suppress(Exception):
                    await self._store.release_webhook_delivery(
                        batch_id=batch_id, worker_id=self._worker_id
                    )

    async def _deliver_attempts(self, row: dict[str, Any]) -> None:
        batch_id = row["id"]
        attempts = int(row.get("webhook_attempts") or 0)
        remaining = self._timings.max_attempts - attempts
        if remaining <= 0:
            await self._finish(batch_id, delivered=False, retry=False)
            return
        payload = webhook_payload(row, batch_config())
        retrying = AsyncRetrying(
            stop=stop_after_attempt(remaining),
            wait=wait_chain(*[wait_fixed(seconds) for seconds in self._timings.retry_wait_seconds]),
            retry=retry_if_exception_type((httpx.HTTPError, WebhookDeliveryFailed)),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    await self._post(row, payload, attempt.retry_state.attempt_number + attempts)
        except WebhookClaimLost:
            logger.info(
                "batch %s webhook is no longer this worker's; not delivering",
                batch_id,
                extra={"event": "batch_webhook_claim_lost"},
            )
            return
        except WebhookTargetRefused:
            await self._finish(batch_id, delivered=False, retry=False)
            return
        except Exception as exc:
            logger.warning(
                "batch %s webhook delivery stopped: %s",
                batch_id,
                self._scrub(row.get("webhook_url") or "", exc),
                extra={"event": "batch_webhook_failed"},
            )
            await self._finish(batch_id, delivered=False)
            return
        await self._finish(batch_id, delivered=True)

    async def _post(self, row: dict[str, Any], payload: dict[str, Any], attempt: int) -> None:
        url = row["webhook_url"]
        try:
            await resolve_public_webhook(url)
        except UnsafeWebhookTarget as exc:
            await self._record(row, None, str(exc))
            logger.warning(
                "batch %s webhook refused: %s",
                row["id"],
                exc,
                extra={"event": "batch_webhook_refused"},
            )
            raise WebhookTargetRefused(str(exc)) from exc
        headers = {
            "User-Agent": "hybridinference-batch-webhook/1.0",
            "X-HybridInference-Event": WEBHOOK_EVENT,
            "X-HybridInference-Delivery": payload["id"],
        }
        try:
            with posting_to(url):
                async with httpx.AsyncClient(
                    timeout=self._timings.timeout_seconds,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            await self._record(row, None, f"{type(exc).__name__}: {self._scrub(url, exc)}")
            logger.warning(
                "batch %s webhook attempt %d failed: %s",
                row["id"],
                attempt,
                self._scrub(url, exc),
                extra={"event": "batch_webhook_attempt", "attempt": attempt},
            )
            raise
        error = None if response.is_success else f"HTTP {response.status_code}"
        recorded = await self._record(
            row, response.status_code, self._scrub(url, error) if error else None
        )
        if recorded is None:
            raise WebhookClaimLost
        logger.info(
            "batch %s webhook attempt %d: %s",
            row["id"],
            attempt,
            response.status_code,
            extra={
                "event": "batch_webhook_attempt",
                "attempt": attempt,
                "status_code": response.status_code,
            },
        )
        if error is not None:
            raise WebhookDeliveryFailed(error)

    async def _record(
        self, row: dict[str, Any], status_code: int | None, error: str | None
    ) -> int | None:
        return await self._store.record_webhook_attempt(
            batch_id=row["id"],
            worker_id=self._worker_id,
            status_code=status_code,
            error=error,
        )

    async def _finish(self, batch_id: str, *, delivered: bool, retry: bool = True) -> None:
        with contextlib.suppress(Exception):
            await self._store.finish_webhook_delivery(
                batch_id=batch_id,
                worker_id=self._worker_id,
                delivered=delivered,
                max_attempts=self._timings.max_attempts,
                retry=retry,
            )

    @staticmethod
    def _scrub(url: str, value: Any) -> str:
        """Render *value* as text with *url* redacted."""
        text = str(value)
        return url_redaction(url).scrub(text)[:500] if url else text[:500]


def webhook_payload(row: dict[str, Any], config: BatchConfig) -> dict[str, Any]:
    """Build the unsigned event body: the batch object plus where its results are.

    ``created_at`` is the batch's terminal timestamp, so retries of one callback
    carry the same value.
    """
    batch = batch_object(row, config)
    return {
        "id": f"evt_{row['id']}",
        "object": "batch.event",
        "type": WEBHOOK_EVENT,
        "created_at": _terminal_epoch(row),
        "data": {
            "batch": batch.model_dump(),
            "results_url": f"/v1/batches/{row['id']}/results",
        },
    }


def _terminal_epoch(row: dict[str, Any]) -> int:
    for field in ("completed_at", "failed_at", "expired_at", "cancelled_at", "created_at"):
        value = row.get(field)
        if isinstance(value, datetime):
            return epoch(value) or 0
    return 0
