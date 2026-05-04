"""Per-user concurrency limiter.

Caps the number of simultaneous in-flight inference requests per user,
keyed by ``user_id``. Backed by an in-process counter under the asyncio
single-thread invariant — no Redis, no DB.

Limits are resolved per-call via a ``LimitsProvider`` async callable so
operators can adjust caps at runtime through the admin settings API.
Each existing ``_UserSlot`` lazily resizes on its owner's next acquire.
The slot's *role label* remains sticky to its creation-time value so
metrics stay coherent across role changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from serving.observability.metrics import (
    USER_CONCURRENCY_ACQUIRES_TOTAL,
    USER_CONCURRENCY_IN_FLIGHT,
    USER_CONCURRENCY_REJECTED_TOTAL,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

LimitsProvider = Callable[[], Awaitable[dict[str, int]]]

# Conservative fallback when the provider raises (e.g., DB hiccup). Kept
# in sync with the defaults declared in
# ``serving/config/runtime_settings.py`` for the ``user_concurrency_*``
# keys.
_FALLBACK_LIMITS: dict[str, int] = {
    "free": 3,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}


def static_limits_provider(limits: dict[str, int]) -> LimitsProvider:
    """Wrap a plain dict in a ``LimitsProvider`` (test helper)."""
    snapshot = dict(limits)

    async def _provider() -> dict[str, int]:
        return snapshot

    return _provider


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
    """Per-user in-flight request limiter with runtime-adjustable caps."""

    def __init__(self, limits_provider: LimitsProvider):
        self._provider = limits_provider
        self._slots: dict[str, _UserSlot] = {}
        self._create_lock = asyncio.Lock()  # guards lazy slot creation

    async def _read_limits(self) -> dict[str, int]:
        """Resolve current limits, falling back to defaults on error."""
        try:
            return await self._provider()
        except Exception:
            logger.exception(
                "user_concurrency: limits provider failed; falling back to defaults"
            )
            return dict(_FALLBACK_LIMITS)

    @staticmethod
    def _limit_for(role: str, is_admin: bool, limits: dict[str, int]) -> int:
        if is_admin:
            return limits.get("admin", _FALLBACK_LIMITS["admin"])
        if role in limits:
            return limits[role]
        return limits.get("free", _FALLBACK_LIMITS["free"])

    @staticmethod
    def _role_label(role: str, is_admin: bool, limits: dict[str, int]) -> str:
        if is_admin:
            return "admin"
        if role in limits:
            return role
        return "free"

    async def try_acquire(self, user_id: str, role: str, is_admin: bool) -> tuple[bool, int, str]:
        """Non-blocking acquire.

        Returns ``(granted, capacity, role_label)`` where *capacity*
        reflects the slot's **current** capacity after any lazy resize and
        *role_label* is the slot's sticky label.
        """
        limits = await self._read_limits()
        target_capacity = self._limit_for(role, is_admin, limits)
        target_label = self._role_label(role, is_admin, limits)

        slot = self._slots.get(user_id)
        if slot is None:
            async with self._create_lock:
                slot = self._slots.get(user_id)
                if slot is None:
                    slot = _UserSlot(capacity=target_capacity, role=target_label)
                    self._slots[user_id] = slot

        # Lazy resize: only `capacity` is dynamic; role label stays sticky.
        if slot.capacity != target_capacity:
            slot.capacity = target_capacity

        granted = slot.try_acquire()
        label = slot.role
        if granted:
            USER_CONCURRENCY_ACQUIRES_TOTAL.labels(role=label, outcome="granted").inc()
            USER_CONCURRENCY_IN_FLIGHT.labels(role=label).inc()
        else:
            USER_CONCURRENCY_ACQUIRES_TOTAL.labels(role=label, outcome="rejected").inc()
            USER_CONCURRENCY_REJECTED_TOTAL.labels(role=label).inc()
            logger.warning(
                "concurrency_rejected",
                extra={
                    "event": "concurrency_rejected",
                    "user_id": user_id,
                    "role": label,
                },
            )
        return granted, slot.capacity, label

    def release(self, user_id: str) -> None:
        """Release a slot. Idempotent for unknown user_id."""
        slot = self._slots.get(user_id)
        if slot is None:
            return
        had_one = slot.in_use > 0
        slot.release()
        if had_one:
            USER_CONCURRENCY_IN_FLIGHT.labels(role=slot.role).dec()

    def role_for(self, user_id: str) -> str | None:
        """Return the role label captured at slot creation, or None."""
        slot = self._slots.get(user_id)
        return slot.role if slot is not None else None


# Dependency lives at the bottom of the module so it can reference the
# limiter class and metrics defined above.

from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from .auth import verify_api_key
from .deps import get_user_concurrency_limiter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


async def enforce_user_concurrency(
    request: Request,
    user: dict[str, Any] = Depends(verify_api_key),
    limiter: UserConcurrencyLimiter | None = Depends(get_user_concurrency_limiter),
) -> AsyncGenerator[None, None]:
    """Acquire a per-user concurrency slot or raise 429.

    Uses ``yield`` so FastAPI runs the cleanup ``finally`` block after the
    response (including streaming body) is fully sent, on exception, or
    on client disconnect.
    """
    if limiter is None:
        # If the limiter isn't configured (e.g., misconfigured deployment),
        # fail open — never block requests when the gate itself is broken.
        logger.warning("user_concurrency: limiter is None; passing request through unguarded")
        yield
        return

    user_id = user["user_id"]
    role = user.get("role", "free") or "free"
    is_admin = bool(user.get("is_admin", False))

    granted, limit, role_label = await limiter.try_acquire(user_id, role, is_admin)
    if not granted:
        logger.info(
            "per-user concurrency limit hit",
            extra={
                "user_id": user_id,
                "role": role_label,
                "limit": limit,
                "route": request.url.path,
            },
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "concurrency_limit_exceeded",
                    "message": f"Too many concurrent requests (limit: {limit})",
                    "limit": limit,
                    "role": role_label,
                }
            },
            headers={"Retry-After": "1"},
        )

    try:
        yield
    finally:
        try:
            limiter.release(user_id)
        except Exception:
            # Never let cleanup break the request lifecycle.
            logger.exception(
                "user_concurrency: release failed",
                extra={"user_id": user_id},
            )
