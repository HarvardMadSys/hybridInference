"""Per-user concurrency limiter.

Caps the number of simultaneous in-flight inference requests per user,
keyed by ``user_id``. Backed by an in-process counter under the asyncio
single-thread invariant — no Redis, no DB.

A new ``_UserSlot`` is lazy-created on first acquire per user; its
capacity is captured from the user's role at that moment and is sticky
(role changes mid-process do not resize an existing slot — restart
corrects).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from serving.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _UserSlot:
    """Tiny counter for one user's in-flight requests.

    asyncio is single-threaded; ``try_acquire`` and ``release`` contain no
    ``await`` and therefore execute atomically with respect to other tasks
    on the same event loop. No internal lock is needed.
    """

    capacity: int
    role: str  # role label captured at slot creation; used for metrics
    in_use: int = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.capacity:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        if self.in_use > 0:
            self.in_use -= 1


class UserConcurrencyLimiter:
    """Per-user in-flight request limiter."""

    def __init__(self, limits: dict[str, int]):
        # e.g. {"free": 1, "pro": 3, "internal": 10, "admin": 10}
        self._limits = limits
        self._slots: dict[str, _UserSlot] = {}
        self._create_lock = asyncio.Lock()  # guards lazy slot creation

    def limit_for(self, role: str, is_admin: bool) -> int:
        """Return the capacity for a (role, is_admin) pair.

        ``is_admin=True`` always returns the admin cap, regardless of role.
        Unknown roles fall back to the most restrictive (``free``) cap.
        """
        if is_admin:
            return self._limits["admin"]
        return self._limits.get(role, self._limits["free"])

    def role_label(self, role: str, is_admin: bool) -> str:
        """The label used for metrics. Admin overrides the user's role."""
        if is_admin:
            return "admin"
        if role in self._limits:
            return role
        return "free"

    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> bool:
        """Non-blocking acquire. Returns True on success, False if at cap.

        Lazy-creates the per-user slot on first call. Capacity is captured
        from the user's role at creation time and is sticky thereafter.
        """
        slot = self._slots.get(user_id)
        if slot is None:
            async with self._create_lock:
                slot = self._slots.get(user_id)
                if slot is None:
                    capacity = self.limit_for(role, is_admin)
                    slot = _UserSlot(
                        capacity=capacity,
                        role=self.role_label(role, is_admin),
                    )
                    self._slots[user_id] = slot
        return slot.try_acquire()

    def release(self, user_id: str) -> None:
        """Release a slot. Idempotent for unknown user_id."""
        slot = self._slots.get(user_id)
        if slot is not None:
            slot.release()

    def role_for(self, user_id: str) -> str | None:
        """Return the role label captured at slot creation, or None."""
        slot = self._slots.get(user_id)
        return slot.role if slot is not None else None
