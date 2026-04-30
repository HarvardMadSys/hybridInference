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
