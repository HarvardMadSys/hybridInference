"""Per-model batch concurrency caps, cached for the batch worker.

An administrator caps how many batch requests each model runs at once across
the whole deployment, on the admin Settings page
(``serving.servers.routers.admin.batch``). The caps are rows of
``batch_model_limits`` (``serving.storage.batch_store``); a model without one
takes ``BATCH_MODEL_CONCURRENCY`` (:mod:`serving.batch.config`), and a cap of 0
pauses batch work on the model.

The worker reads a cap for every model it claims for, every round, so this
keeps one snapshot of every row and reads the table at most once per ``ttl``:
a change reaches every gateway process within that time, and the process that
made it at once (:meth:`BatchModelLimits.invalidate`).
"""

from __future__ import annotations

import time
from threading import RLock
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class BatchModelLimits:
    """Cached snapshot of the per-model batch concurrency overrides.

    Reads are fenced by a generation counter, as in ``ModelConcurrencyResolver``:
    a read whose query was in flight when :meth:`invalidate` ran publishes
    nothing and reads again, so a slow query can never put back a cap an
    administrator just changed.

    A read that fails keeps the last snapshot that loaded, logs a warning and
    is not retried for another ``ttl``. Before any read has succeeded there is
    nothing to keep: an unread table is unknown, not empty, and assuming no
    overrides could run a paused model, so the store's error propagates.
    """

    def __init__(self, store: Any, *, ttl: float = 10.0) -> None:
        self._store = store
        self._ttl = ttl
        self._snapshot: dict[str, int] = {}
        self._loaded = False
        # Monotonic time of the last read; None when stale (never read, invalidated).
        self._read_at: float | None = None
        self._generation = 0
        self._lock = RLock()

    async def overrides(self) -> dict[str, int]:
        """Return every model's override, reading the store at most once per ``ttl``.

        Returns:
            A copy of the snapshot: model id to the cap its row sets.

        Raises:
            Exception: The store's error, when no read has succeeded yet.
        """
        while True:
            with self._lock:
                if self._read_at is not None and time.monotonic() - self._read_at < self._ttl:
                    return dict(self._snapshot)
                generation = self._generation

            try:
                rows = await self._store.list_model_limits()
                snapshot = {str(model_id): int(limit) for model_id, limit in rows.items()}
            except Exception:
                with self._lock:
                    if not self._loaded:
                        raise
                    if self._generation == generation:
                        # Retry after another ttl, not on every read of an
                        # unreachable store.
                        self._read_at = time.monotonic()
                    snapshot = dict(self._snapshot)
                logger.warning(
                    "Could not read the batch model limits; keeping the last snapshot",
                    exc_info=True,
                )
                return snapshot

            with self._lock:
                if self._generation != generation:
                    continue
                self._snapshot = snapshot
                self._loaded = True
                self._read_at = time.monotonic()
                return dict(snapshot)

    async def limit_for(self, model_id: str, default: int) -> int:
        """Return the cap in force for *model_id*: its override, else *default*.

        Raises:
            Exception: The store's error, when no read has succeeded yet.
        """
        return (await self.overrides()).get(model_id, default)

    def invalidate(self) -> None:
        """Make the next read query the store (after an administrator's write)."""
        with self._lock:
            self._generation += 1
            self._read_at = None
