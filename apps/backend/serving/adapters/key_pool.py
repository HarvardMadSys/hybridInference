"""Multi-key API rotation with sequential (use-one-until-it-errors) selection.

Each adapter that opts into multi-key holds a KeyPool. The pool exposes
``acquire(affinity_key)`` and ``release(lease, outcome)``. State is
in-process, behind a single ``threading.Lock``.

Selection is **sequential**: the pool always hands out the earliest usable
key and only advances to a later key once an earlier one is muted. A key is
muted for ``MUTE_SECONDS`` (5 minutes) on upstream failures that are
key-specific or transient — rate limit/quota (429), auth/permission
(401/402/403), request-timeout / too-early (408/425), any 5xx, and non-HTTP
failures such as timeouts and connection errors. Request-scoped client errors
(other 4xx like 400/404/422) do *not* mute: they fail identically on every
key, so muting would take the whole pool down. The net effect is "use one key
until it runs out of quota or errors, then move to the next".

See docs/agents/specs/archive/2026-04-30-multi-key-rotation-design.md for the
original (least-loaded) design this supersedes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# 4xx statuses that are key-specific (not request-scoped) and so should mute the
# key and trigger rotation: auth/permission, payment, request-timeout/too-early,
# and rate limit/quota. All other 4xx are treated as request-scoped.
_MUTABLE_4XX = frozenset({401, 402, 403, 408, 425, 429})


def should_mute_status(status_code: int) -> bool:
    """Whether an upstream outcome should mute the key and trigger rotation.

    Mutes on failures that are key-specific or transient: rate limit/quota
    (429), auth/permission (401/402/403), request-timeout / too-early
    (408/425), any 5xx, and non-HTTP failures (``status_code == 0`` for
    timeouts / connection errors). A 2xx response and request-scoped client
    errors (other 4xx such as 400/404/422) do not mute — a bad request fails
    identically on every key, so muting it would disable the whole pool.
    """
    if 200 <= status_code < 300:
        return False
    if status_code == 0:
        return True
    if status_code in _MUTABLE_4XX:
        return True
    return 500 <= status_code <= 599


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
    """Hands out API keys sequentially, muting a key for 5 minutes on a key error."""

    AFFINITY_TTL_SECONDS: float = 300.0  # 5 minutes
    MUTE_SECONDS: float = 300.0  # 5 minutes — key-specific/transient errors mute the key
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
        with self._lock:
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

            idx = self._pick_first_available_locked(now)
            if idx is None:
                raise KeyPoolExhausted(
                    f"All {len(self._keys)} keys for provider {self._provider_label!r} are muted"
                )

            self._affinity[affinity_key] = _Affinity(
                key_index=idx,
                expires_at=now + self.AFFINITY_TTL_SECONDS,
            )
            self._keys[idx].request_count += 1
            return self._keys[idx].key, Lease(idx, affinity_key)

    def _pick_first_available_locked(self, now: float) -> int | None:
        """Return the index of the lowest-index key that is usable, or None.

        Sequential selection: traffic concentrates on the earliest key that is
        neither removed nor muted, and only advances to a later key once the
        earlier ones are muted. ``request_count`` is no longer a selection
        signal — it is retained purely for telemetry.
        """
        for i, state in enumerate(self._keys):
            if state.removed:
                continue
            if state.cooldown_until > now:
                continue
            return i
        return None

    def _maybe_sweep_locked(self, now: float) -> None:
        """Drop expired affinity entries when the dict grows past threshold."""
        if len(self._affinity) <= self.SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def release(self, lease: Lease, *, status_code: int) -> None:
        """Report the request outcome so an errored key can be muted.

        Mutes the leased key for ``MUTE_SECONDS`` (5 minutes) when
        ``should_mute_status(status_code)`` is true — i.e. for key-specific or
        transient failures (429, 401/402/403, 408/425, 5xx, and non-HTTP
        failures signalled by ``status_code == 0``). A 2xx response and
        request-scoped client errors (other 4xx) leave the key usable.

        Args:
            lease: the lease returned by ``acquire``.
            status_code: HTTP status code, or 0 for non-HTTP failures
                (timeouts / network errors).
        """
        if not should_mute_status(status_code):
            return
        with self._lock:
            self._keys[lease.key_index].cooldown_until = time.monotonic() + self.MUTE_SECONDS
