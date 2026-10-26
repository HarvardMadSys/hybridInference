"""Bounded request-to-prefix state awaiting a terminal observation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

PREFIX_CACHE_PENDING_TTL_SECONDS = 300.0
PREFIX_CACHE_PENDING_MAX_ENTRIES = 10_000
PREFIX_CACHE_PENDING_SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class PendingPrefixCacheEntry:
    """One route-time prefix snapshot waiting for its winning observation."""

    blocks: Any
    scopes: Mapping[str, Any]
    created_at: float
    last_activity_at: float


class PendingPrefixCacheStore:
    """Own TTL, capacity, and abnormal-eviction telemetry for prefix stashes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = PREFIX_CACHE_PENDING_TTL_SECONDS,
        max_entries: int = PREFIX_CACHE_PENDING_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: dict[str, PendingPrefixCacheEntry] = {}
        self._lock = RLock()

    def put(self, request_id: str, blocks: Any, scopes: Mapping[str, Any]) -> None:
        """Store one request, evicting oldest entries when the cap is exceeded."""
        if not request_id or not scopes:
            return
        now = self._clock()
        evicted: list[tuple[str, PendingPrefixCacheEntry, int, str]] = []
        with self._lock:
            if request_id not in self._entries and len(self._entries) >= self._max_entries:
                # Entries are ordered by last activity: put appends new keys,
                # replacement preserves position, and touch moves a key to the
                # end. Only the oldest entry needs inspection here.
                stale_request_id = next(iter(self._entries))
                entry = self._entries[stale_request_id]
                if self._is_expired(entry, now):
                    removed = self._entries.pop(stale_request_id)
                    evicted.append((stale_request_id, removed, len(self._entries), "ttl"))
            prior = self._entries.get(request_id)
            self._entries[request_id] = PendingPrefixCacheEntry(
                blocks=blocks,
                scopes=dict(scopes),
                # A retry may update which endpoint scopes can eventually be
                # warmed, but it must not renew the pending entry forever.
                created_at=prior.created_at if prior is not None else now,
                last_activity_at=prior.last_activity_at if prior is not None else now,
            )
            while len(self._entries) > self._max_entries:
                oldest_request_id = next(iter(self._entries))
                entry = self._entries.pop(oldest_request_id)
                evicted.append((oldest_request_id, entry, len(self._entries), "size_cap"))
        for evicted_request_id, entry, pending_count, reason in evicted:
            self._emit_eviction(
                evicted_request_id,
                entry,
                now=now,
                reason=reason,
                pending_count=pending_count,
            )

    def touch(self, request_id: str) -> bool:
        """Renew the inactivity lease for a request that is still streaming."""
        if not request_id:
            return False
        now = self._clock()
        expired: PendingPrefixCacheEntry | None = None
        pending_count = 0
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return False
            if self._is_expired(entry, now):
                expired = self._entries.pop(request_id)
                pending_count = len(self._entries)
            else:
                self._entries.pop(request_id)
                self._entries[request_id] = PendingPrefixCacheEntry(
                    blocks=entry.blocks,
                    scopes=entry.scopes,
                    created_at=entry.created_at,
                    last_activity_at=now,
                )
        if expired is not None:
            self._emit_eviction(
                request_id,
                expired,
                now=now,
                reason="ttl",
                pending_count=pending_count,
            )
            return False
        return True

    def pop(self, request_id: str) -> PendingPrefixCacheEntry | None:
        """Consume a live entry, rejecting and reporting one that expired first."""
        if not request_id:
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.pop(request_id, None)
        if entry is None:
            return None
        if self._is_expired(entry, now):
            self._emit_eviction(
                request_id,
                entry,
                now=now,
                reason="ttl",
                pending_count=len(self),
            )
            return None
        return entry

    def discard(self, request_id: str) -> bool:
        """Drop terminal request state without abnormal-eviction telemetry."""
        if not request_id:
            return False
        with self._lock:
            return self._entries.pop(request_id, None) is not None

    def clear(self) -> None:
        """Drop all pending state as normal lifecycle cleanup."""
        with self._lock:
            self._entries.clear()

    def sweep_expired(self) -> int:
        """Evict all expired entries and return the number reclaimed."""
        now = self._clock()
        stale: list[tuple[str, PendingPrefixCacheEntry, int]] = []
        with self._lock:
            while self._entries:
                request_id = next(iter(self._entries))
                entry = self._entries[request_id]
                if not self._is_expired(entry, now):
                    break
                removed = self._entries.pop(request_id)
                stale.append((request_id, removed, len(self._entries)))
        for request_id, entry, pending_count in stale:
            self._emit_eviction(
                request_id,
                entry,
                now=now,
                reason="ttl",
                pending_count=pending_count,
            )
        return len(stale)

    def __contains__(self, request_id: object) -> bool:
        with self._lock:
            return request_id in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _is_expired(self, entry: PendingPrefixCacheEntry, now: float) -> bool:
        return now - entry.last_activity_at > self._ttl_seconds

    def _emit_eviction(
        self,
        request_id: str,
        entry: PendingPrefixCacheEntry,
        *,
        now: float,
        reason: str,
        pending_count: int,
    ) -> None:
        try:
            logger.info(
                "routewise_prefix_cache_entry_evicted",
                extra={
                    "event": "routewise_prefix_cache_entry_evicted",
                    "request_id": request_id,
                    "age_sec": int(max(0.0, now - entry.created_at)),
                    "idle_sec": int(max(0.0, now - entry.last_activity_at)),
                    "reason": reason,
                    "pending_count": pending_count,
                    "capacity": self._max_entries,
                },
            )
        except Exception:
            # Observability must never alter a routing or completion result.
            return


__all__ = [
    "PREFIX_CACHE_PENDING_MAX_ENTRIES",
    "PREFIX_CACHE_PENDING_SWEEP_INTERVAL_SECONDS",
    "PREFIX_CACHE_PENDING_TTL_SECONDS",
    "PendingPrefixCacheEntry",
    "PendingPrefixCacheStore",
]
