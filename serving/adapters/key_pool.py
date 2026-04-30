"""Multi-key API rotation with per-user session affinity.

Each adapter that opts into multi-key holds a KeyPool. The pool exposes
``acquire(affinity_key)`` and ``release(lease, outcome)``. State is
in-process, behind a single ``threading.Lock``.

See docs/superpowers/specs/2026-04-30-multi-key-rotation-design.md
"""

from __future__ import annotations

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


@dataclass
class _Affinity:
    key_index: int
    expires_at: float  # monotonic timestamp


@dataclass
class _Lease:
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
        return len(self._keys)

    def affinity_count(self) -> int:
        return len(self._affinity)

    def acquire(self, affinity_key: str) -> tuple[str, _Lease]:
        """Return (api_key, lease) for the caller, creating affinity as needed.

        Raises:
            KeyPoolExhausted: if every key is currently in cooldown.
        """
        now = time.monotonic()
        with self._lock:
            self._maybe_sweep_locked(now)

            existing = self._affinity.get(affinity_key)
            if existing is not None:
                # Affinity is honored only when it is still valid AND the
                # bound key is not cooled down.
                if (
                    now < existing.expires_at
                    and self._keys[existing.key_index].cooldown_until <= now
                ):
                    idx = existing.key_index
                    self._keys[idx].request_count += 1
                    return self._keys[idx].key, _Lease(idx, affinity_key)
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
            return self._keys[idx].key, _Lease(idx, affinity_key)

    def _pick_least_loaded_locked(self, now: float) -> int | None:
        """Return the index of the lowest-request_count non-cooled key, or None."""
        best_idx: int | None = None
        best_count: int | None = None
        for i, state in enumerate(self._keys):
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
