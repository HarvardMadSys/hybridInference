"""Runtime resolver for per-model concurrency-limit exemptions."""

from __future__ import annotations

import time
from threading import RLock
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class ModelConcurrencyResolver:
    """Resolve whether a model is exempt from the per-user concurrency limit."""

    def __init__(self, store: Any, ttl: float = 30.0) -> None:
        self._store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}
        self._versions: dict[str, int] = {}
        self._lock = RLock()

    async def is_exempt(self, model_id: str) -> bool:
        """Return True when the model is exempt from the per-user concurrency limit."""
        while True:
            now = time.monotonic()
            with self._lock:
                cached = self._cache.get(model_id)
                if cached is not None and (now - cached[0]) < self._ttl:
                    return cached[1]
                version = self._versions.setdefault(model_id, 0)

            row = await self._store.get_model_concurrency_exemption(model_id)
            exempt = row is not None
            refreshed_at = time.monotonic()
            with self._lock:
                if self._versions.get(model_id, 0) != version:
                    continue
                self._cache[model_id] = (refreshed_at, exempt)
                return exempt

    def invalidate_model(self, model_id: str) -> None:
        """Drop the cached exemption entry for a single model."""
        with self._lock:
            self._cache.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def invalidate_cache(self) -> None:
        """Clear all cached model concurrency exemptions."""
        with self._lock:
            self._cache.clear()
            for model_id in self._versions:
                self._versions[model_id] += 1
