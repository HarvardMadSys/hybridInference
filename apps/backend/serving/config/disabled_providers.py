"""Runtime resolver for admin-disabled upstream providers.

A provider label present in the snapshot is treated as fully unavailable:
the router zeroes the weight of every adapter whose ``config.provider`` matches,
so both weighted selection and fallback loops skip it. The snapshot is read
synchronously during routing (mirroring :class:`WeightOverrideResolver`), so it
holds a plain in-process set refreshed from the operational store.
"""

from __future__ import annotations

from threading import RLock
from typing import Any


class DisabledProviderResolver:
    """Hold the set of disabled provider labels for synchronous routing reads."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._disabled: frozenset[str] = frozenset()
        self._lock = RLock()

    async def load_all(self) -> bool:
        """Reload the disabled set and return whether its contents changed."""
        rows = await self._store.list_disabled_providers()
        disabled = frozenset(str(row["provider"]) for row in rows)
        with self._lock:
            changed = disabled != self._disabled
            self._disabled = disabled
            return changed

    def is_disabled(self, provider: str) -> bool:
        """Return whether a provider label is currently disabled (sync snapshot)."""
        with self._lock:
            return provider in self._disabled

    def list_disabled(self) -> frozenset[str]:
        """Return the current snapshot of disabled provider labels."""
        with self._lock:
            return self._disabled

    def set_disabled(self, provider: str) -> None:
        """Update the snapshot after a successful admin disable."""
        with self._lock:
            self._disabled = self._disabled | {provider}

    def clear_disabled(self, provider: str) -> None:
        """Update the snapshot after a successful admin re-enable."""
        with self._lock:
            self._disabled = self._disabled - {provider}
