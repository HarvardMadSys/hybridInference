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

from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.observability.metrics import (
    USER_CONCURRENCY_ACQUIRES_TOTAL,
    USER_CONCURRENCY_IN_FLIGHT,
    USER_CONCURRENCY_REJECTED_TOTAL,
)
from serving.observability.rejection_log import extract_prompt_from_body, log_rejection
from serving.utils.logging import get_logger

logger = get_logger(__name__)

LimitsProvider = Callable[[], Awaitable[dict[str, int]]]

# Per-user concurrency cap applied to concurrency-exempt models ("not limited
# by concurrency"). Exempt-model requests do not count against the user's
# normal role-based budget, but are still bounded to this many concurrent
# in-flight requests per user so a single user cannot open unbounded
# concurrent requests against an exempt model.
EXEMPT_MODEL_USER_CONCURRENCY_LIMIT = 64


def _build_fallback_limits() -> dict[str, int]:
    """Derive fallback caps from the runtime-settings registry.

    The registry import is deferred to the function body so importing this
    module never forces an early import of ``runtime_settings``.
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    return {
        role: int(RUNTIME_SETTINGS_REGISTRY[f"user_concurrency_{role}"]["default"])
        for role in ("free", "pro", "internal", "admin")
    }


# Conservative fallback when the provider raises (e.g., DB hiccup). Computed
# once at import time from the registry defaults so the two sources cannot
# drift over time.
_FALLBACK_LIMITS: dict[str, int] = _build_fallback_limits()


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
            logger.exception("user_concurrency: limits provider failed; falling back to defaults")
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

    async def try_acquire(
        self,
        user_id: str,
        role: str,
        is_admin: bool,
        max_concurrent_requests: int | None = None,
    ) -> tuple[bool, int, str]:
        """Non-blocking acquire.

        Returns ``(granted, capacity, role_label)`` where *capacity*
        reflects the slot's **current** capacity after any lazy resize and
        *role_label* is the slot's sticky label.

        *max_concurrent_requests* overrides the role-based default when set.
        """
        limits = await self._read_limits()
        target_capacity = (
            max_concurrent_requests
            if max_concurrent_requests is not None
            else self._limit_for(role, is_admin, limits)
        )
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
from .deps import get_model_concurrency_resolver, get_router, get_user_concurrency_limiter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _exempt_slot_key(user_id: str) -> str:
    """Return the separate per-user budget key for concurrency-exempt models.

    Exempt-model requests must not consume the user's normal in-flight slots,
    so they acquire against a distinct key. The NUL separator can never appear
    in a real ``user_id``, so the two budgets can never collide.
    """
    return f"{user_id}\x00exempt"


async def enforce_user_concurrency(
    request: Request,
    user: dict[str, Any] = Depends(verify_api_key),
    limiter: UserConcurrencyLimiter | None = Depends(get_user_concurrency_limiter),
    router: Any = Depends(get_router),
    concurrency_resolver: Any = Depends(get_model_concurrency_resolver),
) -> AsyncGenerator[None, None]:
    """Acquire a per-user concurrency slot or raise 429.

    Models flagged as concurrency-exempt ("not limited by concurrency") do not
    consume the user's normal role-based in-flight budget. Instead they draw on
    a separate per-user budget capped at ``EXEMPT_MODEL_USER_CONCURRENCY_LIMIT``,
    so a single user still cannot open unbounded concurrent requests against an
    exempt model.

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

    # Concurrency-exemption check. Restricted to POST requests: only the
    # inference routes (chat/completions/embeddings/messages) carry a JSON
    # body with a ``model`` field, so parsing anything else (e.g. GET
    # ``/v1/models``) is wasted work and a slow-body attack surface. Parsing
    # must never break the gate, so any failure here falls through to the
    # normal acquire/release path. FastAPI caches the parsed body, so the
    # handler's own ``await request.json()`` still works.
    is_exempt_model = False
    if concurrency_resolver is not None and router is not None and request.method == "POST":
        try:
            body = await request.json()
            model = body.get("model") if isinstance(body, dict) else None
            if model:
                # Resolve Anthropic display aliases (e.g. claude-3-opus-latest)
                # to registry IDs before the route lookup, matching how the
                # Anthropic Messages handler canonicalizes models. Only models
                # recognized by the router reach the resolver: unknown strings
                # are never cached, so arbitrary input can't pollute its
                # in-memory cache.
                resolved = resolve_anthropic_alias(model)
                route = router.routes.get(resolved)
                canonical = (
                    route.adapters[0][0].config.id if route is not None and route.adapters else None
                )
                if canonical is not None and await concurrency_resolver.is_exempt(canonical):
                    logger.debug(
                        "user_concurrency: model exempt; using separate per-user budget",
                        extra={"model": model, "canonical": canonical},
                    )
                    is_exempt_model = True
        except Exception:
            # Never let exemption parsing break the gate; fall through to the
            # normal per-user concurrency path below.
            logger.debug("user_concurrency: exemption check skipped", exc_info=True)

    user_id = user["user_id"]
    role = user.get("role", "free") or "free"
    is_admin = bool(user.get("is_admin", False))
    max_concurrent = user.get("max_concurrent_requests")

    if is_exempt_model:
        # "Not limited by concurrency" models don't draw on the user's normal
        # role-based budget, but are still capped per user so a single user
        # cannot open unbounded concurrent requests against an exempt model.
        slot_key = _exempt_slot_key(user_id)
        max_concurrent = EXEMPT_MODEL_USER_CONCURRENCY_LIMIT
    else:
        slot_key = user_id

    granted, limit, role_label = await limiter.try_acquire(slot_key, role, is_admin, max_concurrent)
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
        # Best-effort prompt capture for the rejection log. Only read the body
        # if it was already parsed and cached (Starlette stores it on
        # ``request._json`` after ``await request.json()`` — which the exemption
        # check above does for POSTs with a resolver/router). Never trigger a
        # fresh body read here: forcing the server to await the full body of a
        # request it is rejecting would be a slowloris/DoS foothold.
        rejected_prompt: list[dict[str, Any]] | str = ""
        cached_json = getattr(request, "_json", None)
        if cached_json is not None:
            try:
                rejected_prompt = extract_prompt_from_body(cached_json)
            except Exception:
                rejected_prompt = ""
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=429,
                error_code="concurrency_limit_exceeded",
                reason=f"limit={limit} role={role_label}",
                user=user,
                prompt=rejected_prompt,
            )
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
            limiter.release(slot_key)
        except Exception:
            # Never let cleanup break the request lifecycle.
            logger.exception(
                "user_concurrency: release failed",
                extra={"user_id": user_id},
            )
