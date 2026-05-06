"""Multi-key API rotation with per-user session affinity.

Each adapter that opts into multi-key holds a KeyPool. The pool exposes
``acquire(affinity_key)`` and ``release(lease, outcome)``. State is
in-process, behind a single ``threading.Lock``.

See docs/agents/specs/2026-04-30-multi-key-rotation-design.md
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class KeyPoolExhausted(Exception):
    """Raised by ``KeyPool.acquire`` when every key is in cooldown."""


@dataclass
class _KeyState:
    key: str
    request_count: int = 0
    cooldown_until: float = 0.0  # monotonic timestamp
    removed: bool = False


@dataclass
class _Affinity:
    key_index: int
    expires_at: float  # monotonic timestamp


@dataclass
class Lease:
    """Round-trip token returned by ``KeyPool.acquire`` and consumed by ``release``."""

    key_index: int
    affinity_key: str


class KeyPool:
    """Rotates API keys with per-user TTL affinity and 429 cooldowns."""

    AFFINITY_TTL_SECONDS: float = 300.0  # 5 minutes
    DEFAULT_COOLDOWN_SECONDS: float = 120.0  # 2 minutes for 429 w/o Retry-After
    MAX_COOLDOWN_SECONDS: float = 3600.0  # 1 hour cap on Retry-After
    SWEEP_THRESHOLD: int = 1000

    def __init__(self, keys: list[str], provider_label: str) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        self._keys: list[_KeyState] = [_KeyState(key=k) for k in keys]
        self._affinity: dict[str, _Affinity] = {}
        self._lock = threading.Lock()
        self._provider_label = provider_label

    def size(self) -> int:
        """Return the number of active (non-removed) keys in the pool."""
        return sum(1 for s in self._keys if not s.removed)

    def snapshot_keys(self) -> list[str]:
        """Return a snapshot of every active key currently in the pool."""
        with self._lock:
            return [s.key for s in self._keys if not s.removed]

    def add_key(self, key: str) -> int:
        """Add a key to the pool, returning its slot index.

        Idempotent: if ``key`` is already present (active or removed), the
        existing slot is reactivated and returned. Otherwise a new slot is
        appended.
        """
        with self._lock:
            for idx, state in enumerate(self._keys):
                if state.key == key:
                    state.removed = False
                    return idx
            self._keys.append(_KeyState(key=key))
            return len(self._keys) - 1

    def remove_key(self, key: str) -> bool:
        """Mark a key as removed and drop affinity entries pointing at it.

        Returns True if the key was present and removed, False otherwise.
        Slots are tombstoned (not popped) so existing key indices remain
        stable for in-flight leases.
        """
        with self._lock:
            for idx, state in enumerate(self._keys):
                if state.key == key and not state.removed:
                    state.removed = True
                    stale = [k for k, a in self._affinity.items() if a.key_index == idx]
                    for k in stale:
                        del self._affinity[k]
                    return True
            return False

    def affinity_count(self) -> int:
        """Return the number of active per-user affinity entries."""
        return len(self._affinity)

    def acquire(self, affinity_key: str) -> tuple[str, Lease]:
        """Return (api_key, lease) for the caller, creating affinity as needed.

        Raises:
            KeyPoolExhausted: if every key is currently in cooldown.
        """
        now = time.monotonic()
        with self._lock:
            self._maybe_sweep_locked(now)

            existing = self._affinity.get(affinity_key)
            if existing is not None:
                bound = self._keys[existing.key_index]
                # Affinity is honored only when it is still valid AND the
                # bound key is not cooled down or removed.
                if now < existing.expires_at and not bound.removed and bound.cooldown_until <= now:
                    idx = existing.key_index
                    self._keys[idx].request_count += 1
                    return self._keys[idx].key, Lease(idx, affinity_key)
                # Drop stale or unusable affinity; we'll re-pick below.
                del self._affinity[affinity_key]

            idx = self._pick_least_loaded_locked(now)
            if idx is None:
                raise KeyPoolExhausted(
                    f"All {len(self._keys)} keys for provider "
                    f"{self._provider_label!r} are in cooldown"
                )

            self._affinity[affinity_key] = _Affinity(
                key_index=idx,
                expires_at=now + self.AFFINITY_TTL_SECONDS,
            )
            self._keys[idx].request_count += 1
            return self._keys[idx].key, Lease(idx, affinity_key)

    def _pick_least_loaded_locked(self, now: float) -> int | None:
        """Return the index of the lowest-request_count non-cooled key, or None."""
        best_idx: int | None = None
        best_count: int | None = None
        for i, state in enumerate(self._keys):
            if state.removed:
                continue
            if state.cooldown_until > now:
                continue
            if best_count is None or state.request_count < best_count:
                best_idx = i
                best_count = state.request_count
        return best_idx

    def _maybe_sweep_locked(self, now: float) -> None:
        """Drop expired affinity entries when the dict grows past threshold."""
        if len(self._affinity) <= self.SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def release(
        self,
        lease: Lease,
        *,
        status_code: int,
        retry_after: str | None,
    ) -> None:
        """Report the request outcome so cooldowns can be updated.

        Args:
            lease: the lease returned by ``acquire``.
            status_code: HTTP status code (or 0 for non-HTTP failures, which
                cause no cooldown change).
            retry_after: raw ``Retry-After`` header value if any.
        """
        if status_code != 429:
            # Only 429 triggers cooldown. 2xx, other 4xx, 5xx, and network
            # errors do not flag the key.
            return
        with self._lock:
            now = time.monotonic()
            cooldown = self._compute_cooldown_seconds(retry_after)
            self._keys[lease.key_index].cooldown_until = now + cooldown

    def _compute_cooldown_seconds(self, retry_after: str | None) -> float:
        """Parse Retry-After per RFC 7231; clamp to [0, MAX_COOLDOWN_SECONDS]."""
        if retry_after is None:
            return self.DEFAULT_COOLDOWN_SECONDS

        # Try integer seconds first
        seconds: float | None
        try:
            seconds = float(retry_after.strip())
        except (TypeError, ValueError, AttributeError):
            seconds = None

        # Fall back to HTTP-date
        if seconds is None:
            try:
                dt = parsedate_to_datetime(retry_after)
                seconds = dt.timestamp() - time.time()
            except (TypeError, ValueError, IndexError):
                return self.DEFAULT_COOLDOWN_SECONDS

        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            return self.DEFAULT_COOLDOWN_SECONDS
        return min(seconds, self.MAX_COOLDOWN_SECONDS)
