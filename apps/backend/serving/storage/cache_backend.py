"""Pluggable cache backend interface and the default in-memory backend.

Extracted from :mod:`serving.storage.cache` so the interface and its concrete
backends live apart from the store wrapper. ``cache.py`` imports these to keep
its public surface unchanged.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from copy import deepcopy
from typing import Any

__all__ = [
    "CacheBackend",
    "InMemoryCache",
    "_snapshot",
]

# ---------------------------------------------------------------------------
# Cache backend interface
# ---------------------------------------------------------------------------


class CacheBackend(ABC):
    """Minimal async cache interface. Implementations must be concurrency-safe.

    Generational publication is supported through the *generation* parameter of
    :meth:`set`.  A reader passes the generation it observed before it began the
    underlying read; a publication whose generation differs from the store's
    current generation is dropped.  Implementations that do not track
    generations treat every publication as current, which is safe only for
    caches without invalidation events -- :class:`InMemoryCache` below is the
    canonical tracking implementation.
    """

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """Return cached value or None on miss."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Store *value* under *key* with a TTL in seconds."""

    async def set_if_generation(
        self,
        key: str,
        value: Any,
        ttl: int,
        *,
        generation: int,
    ) -> bool:
        """Atomically publish *value* iff the store's generation still matches.

        The generation check and the insert happen under the same backend
        synchronization boundary, so an invalidation that races a fill cannot
        interleave between the check and the write. Returns True if the value
        was published, False if the generation changed (a stale fill was
        dropped). The default implementation performs a check-then-set and is
        safe only for backends whose invalidation and publication are already
        atomic within :meth:`set`; tracking backends SHOULD override this with
        a single critical section.
        """
        if self.current_generation() != generation:
            return False
        await self.set(key, value, ttl)
        return True

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a single key (no-op if missing)."""

    @abstractmethod
    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob *pattern* (e.g. ``user:*``)."""

    # -- generation hooks (optional; a tracking implementation overrides) ----

    def current_generation(self) -> int:
        """Monotonic counter bumped by every invalidation event."""
        return 0

    def bump_generation(self) -> int:
        """Advance the generation counter and return the new value."""
        return 0

    # -- observability (optional) -------------------------------------------

    def stats(self) -> dict[str, int]:
        """Snapshot of size / hits / misses / evictions counters."""
        return {"size": 0, "hits": 0, "misses": 0, "evictions": 0}


class InMemoryCache(CacheBackend):
    """Bounded, TTL-expiring, LRU cache. Per-process, single-instance VPS.

    Every method is serialized behind one lock. This is not optimistic: the
    auth hot path issues far more reads than writes, and the whole point of
    the generation fence is that an invalidation racing a fill *must not*
    interleave between the fill's generation check and its publication.
    Holding the lock across both is the only way to make that promise, and the
    critical section is a few dict operations -- microseconds, not I/O.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._generation = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._max_entries = max_entries

    # -- internal helpers (lock must be held) -------------------------------

    def _expired(self, expires_at: float) -> bool:
        return time.monotonic() > expires_at

    def _evict_lru_if_needed(self) -> None:
        """Drop least-recently-used entries until size <= max_entries."""
        while len(self._store) > self._max_entries:
            _, _ = self._store.popitem(last=False)
            self._evictions += 1

    def _purge_expired_locked(self, bound: int = 256) -> None:
        """Drop up to *bound* expired entries, oldest-first.

        Only the first *bound* items (oldest by insertion order) are inspected,
        so a cache miss never pays for a full-map scan. An expired entry that
        falls beyond the inspected window is still evicted lazily when it is
        accessed on a later get(); bounding the sweep here only caps the
        per-miss cleanup cost.
        """
        now = time.monotonic()
        purged = 0
        for key in list(self._store.keys())[:bound]:
            _, expires_at = self._store[key]
            if now > expires_at:
                del self._store[key]
                purged += 1
        if purged:
            # Expiry is a form of eviction for observability purposes.
            self._evictions += purged

    # -- CacheBackend API ----------------------------------------------------

    async def get(self, key: str) -> Any | None:
        """Return cached value or None on miss / expiry."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                self._purge_expired_locked()
                return None
            value, expires_at = entry
            if self._expired(expires_at):
                del self._store[key]
                self._evictions += 1
                self._misses += 1
                return None
            self._hits += 1
            # LRU: most recently used goes to the end.
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Store *value* with monotonic-clock expiry."""
        async with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)
            self._store.move_to_end(key)
            self._evict_lru_if_needed()

    async def set_if_generation(
        self,
        key: str,
        value: Any,
        ttl: int,
        *,
        generation: int,
    ) -> bool:
        """Publish *value* atomically iff the generation still matches.

        The check and the insert share one critical section, so an invalidation
        (delete / delete_pattern) that races the fill cannot interleave between
        them. Returns True on publish, False when the generation changed.
        """
        async with self._lock:
            if self._generation != generation:
                return False
            self._store[key] = (value, time.monotonic() + ttl)
            self._store.move_to_end(key)
            self._evict_lru_if_needed()
            return True

    async def delete(self, key: str) -> None:
        """Remove a single key.

        Bumps the generation unconditionally: an invalidation event is an
        epoch marker, not a hit count. A fill that raced this call must see
        a different generation afterwards even when no key matched, because
        the *intent* of the write was to invalidate.
        """
        async with self._lock:
            self._store.pop(key, None)
            self._generation += 1

    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob *pattern*.

        Bumps the generation unconditionally, for the same reason as
        :meth:`delete` — a fill racing this call must not publish its
        snapshot even when nothing matched.
        """
        async with self._lock:
            to_delete = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
            for k in to_delete:
                del self._store[k]
            self._generation += 1

    # -- generation hooks ----------------------------------------------------

    def current_generation(self) -> int:
        """Return the current invalidation generation counter."""
        return self._generation

    def bump_generation(self) -> int:
        """Advance the generation counter and return the new value.

        Used to force a fill to drop its snapshot when an invalidation overlaps
        an in-flight read without deleting a key or pattern.
        """
        self._generation += 1
        return self._generation

    # -- observability -------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Snapshot of size / hits / misses / evictions counters."""
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }


def _snapshot(value: Any) -> Any:
    """Return an independent deep copy of a cache value.

    Rows returned from the cache must not alias the stored object that later
    reads will hand out, and the stored object must not alias whatever was
    written after a cache miss. A consumer that mutates one returned row must
    never corrupt a later read of the same key. Deep-copying is the only way to
    fully isolate the cache's stored value from every returned reference; the
    cached rows here are shallow dicts whose ``preferences`` value is itself a
    mutable dict, so a shallow copy would leak the nested object.
    """
    # Scalars and immutable values are copied by reference at no cost.
    if value is None or isinstance(value, (str, int, float, bool, bytes, tuple, frozenset)):
        return value
    return deepcopy(value)
