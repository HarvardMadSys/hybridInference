"""Runtime resolver for per-model provider route weight overrides."""

from __future__ import annotations

import time
from threading import RLock
from typing import Any


class WeightOverrideResolver:
    """Resolve sparse endpoint weight overrides with a short in-process TTL."""

    def __init__(self, store: Any, ttl: float = 10.0) -> None:
        self._store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, dict[str, float]]] = {}
        self._snapshots: dict[str, dict[str, float]] = {}
        self._versions: dict[str, int] = {}
        self._lock = RLock()

    async def get_for_model(self, model_id: str) -> dict[str, float]:
        """Return endpoint_id -> override weight for one model."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(model_id)
            if cached is not None and (now - cached[0]) < self._ttl:
                return dict(cached[1])
            # Register the in-flight model so a concurrent full refresh bumps
            # its generation even when the authoritative snapshot has no row
            # for this model.
            version = self._versions.setdefault(model_id, 0)

        rows = await self._store.list_weight_overrides_for_model(model_id)
        overrides = {str(row["endpoint_id"]): float(row["weight"]) for row in rows}
        refreshed_at = time.monotonic()
        with self._lock:
            if self._versions.get(model_id, 0) == version:
                self._cache[model_id] = (refreshed_at, dict(overrides))
                self._snapshots[model_id] = dict(overrides)
        return dict(overrides)

    async def load_all(self) -> bool:
        """Warm the sync snapshot and return whether its contents changed."""
        with self._lock:
            start_versions = dict(self._versions)
        rows = await self._store.list_all_weight_overrides()
        snapshots: dict[str, dict[str, float]] = {}
        for row in rows:
            model_id = str(row["model_id"])
            endpoint_id = str(row["endpoint_id"])
            snapshots.setdefault(model_id, {})[endpoint_id] = float(row["weight"])
        with self._lock:
            concurrently_changed = {
                model_id
                for model_id, version in self._versions.items()
                if version != start_versions.get(model_id, 0)
            }
            resolved_snapshots = {
                model_id: dict(weights)
                for model_id, weights in snapshots.items()
                if model_id not in concurrently_changed
            }
            for model_id in concurrently_changed:
                current = self._snapshots.get(model_id)
                if current is not None:
                    resolved_snapshots[model_id] = dict(current)

            changed = resolved_snapshots != self._snapshots
            self._snapshots = resolved_snapshots
            # Preserve the generation fence even when the authoritative data
            # is unchanged: an older in-flight per-model fetch must not write
            # through after this full refresh completes.
            self._cache.clear()
            for model_id in set(self._versions) | set(resolved_snapshots):
                self._versions[model_id] = self._versions.get(model_id, 0) + 1
            return changed

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        """Return the current non-blocking snapshot for routing selection."""
        with self._lock:
            return dict(self._snapshots.get(model_id, {}))

    def set_override(self, model_id: str, endpoint_id: str, weight: float) -> None:
        """Update the routing snapshot after a successful admin upsert."""
        with self._lock:
            model_snapshot = dict(self._snapshots.get(model_id, {}))
            model_snapshot[endpoint_id] = float(weight)
            self._snapshots[model_id] = model_snapshot
            self._cache.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def clear_override(self, model_id: str, endpoint_id: str) -> None:
        """Update the routing snapshot after a successful admin delete."""
        with self._lock:
            model_snapshot = dict(self._snapshots.get(model_id, {}))
            model_snapshot.pop(endpoint_id, None)
            if model_snapshot:
                self._snapshots[model_id] = model_snapshot
            else:
                self._snapshots.pop(model_id, None)
            self._cache.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def invalidate_model(self, model_id: str) -> None:
        """Drop the cached override entry for a single model."""
        with self._lock:
            self._cache.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def clear_model(self, model_id: str) -> None:
        """Remove every cached and synchronous override for one model."""
        with self._lock:
            self._cache.pop(model_id, None)
            self._snapshots.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def invalidate_cache(self) -> None:
        """Clear all cached route weight overrides."""
        with self._lock:
            self._cache.clear()
            for model_id in self._versions:
                self._versions[model_id] += 1
