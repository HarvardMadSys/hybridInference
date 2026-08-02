"""In-memory per-IP auth-failure tracker that auto-blocks abusive sources.

Once a source IP accumulates ``auth_failure_block_threshold`` authentication
failures within ``auth_failure_block_window_sec``, it is blocked at the
API-key auth layer for ``auth_failure_block_duration_sec`` (default: 200
failures in a day → blocked for a day).

Mirrors the in-memory, per-process design of ``serving.utils.login_rate_limit``
and ``serving.utils.signup_rate_limit``: state is a per-process dict and is
intentionally lost on restart. Auth-failure flooding is a rate problem, not an
audit problem, so durability is not worth the disk I/O; with multiple uvicorn
workers each worker enforces its own copy, which only makes the block trigger
sooner in aggregate and is acceptable for a shed-load defense.

IPv6 sources bucket on their ``/64`` via :func:`normalize_ip_bucket`, so an
attacker cannot dodge the block by rotating RFC 4941 privacy addresses within a
delegated prefix. IPv4 keeps full-address buckets.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from serving.config.settings import settings
from serving.utils.logging import get_logger
from serving.utils.request_ip import normalize_ip_bucket

logger = get_logger(__name__)

#: Amortized cleanup cadence: sweep expired state once every N recorded
#: failures rather than on every call. Matches the login/signup limiters.
_SWEEP_EVERY = 1024

#: Per-bucket failure timestamps within the counting window.
_failures: dict[str, deque[float]] = {}
#: Per-bucket wall-clock deadline until which the source is blocked.
_blocked_until: dict[str, float] = {}
_lock = asyncio.Lock()
_sweep_counter = 0


def _now() -> float:
    return time.time()


def _sweep_inactive(now: float, window_sec: int) -> None:
    """Drop stale failure history and lapsed blocks. Caller holds ``_lock``."""
    cutoff = now - window_sec
    for key in list(_failures):
        bucket = _failures[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del _failures[key]
    for key in list(_blocked_until):
        if _blocked_until[key] <= now:
            del _blocked_until[key]


async def is_ip_blocked(ip: str) -> tuple[bool, int]:
    """Return ``(blocked, retry_after_seconds)`` for *ip*.

    ``(False, 0)`` when the feature is disabled or the IP is clear. A lapsed
    block is cleared lazily on read so an expired entry never lingers as a
    false positive.
    """
    if not settings.auth_failure_block_enabled:
        return False, 0
    key = normalize_ip_bucket(ip)
    now = _now()
    async with _lock:
        until = _blocked_until.get(key)
        if until is None:
            return False, 0
        if until <= now:
            del _blocked_until[key]
            return False, 0
        # Round up so a sub-second remainder never advertises Retry-After: 0.
        return True, max(1, int(until - now + 0.999))


async def record_auth_failure(ip: str) -> bool:
    """Record one auth failure for *ip*; return True if this call blocked it.

    Blocks the source for ``auth_failure_block_duration_sec`` once its failure
    count within ``auth_failure_block_window_sec`` reaches
    ``auth_failure_block_threshold``. A no-op returning False when the feature
    is disabled or the source is already blocked (callers reject blocked IPs
    before reaching here, so a True return marks the blocking transition).
    """
    global _sweep_counter
    if not settings.auth_failure_block_enabled:
        return False

    threshold = settings.auth_failure_block_threshold
    window_sec = settings.auth_failure_block_window_sec
    duration_sec = settings.auth_failure_block_duration_sec

    key = normalize_ip_bucket(ip)
    now = _now()
    cutoff = now - window_sec
    blocked_now = False

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            _sweep_inactive(now, window_sec)

        until = _blocked_until.get(key)
        if until is not None and until > now:
            # Already blocked: the block deadline is fixed, so do not keep
            # accruing history for it. Not a fresh transition.
            return False

        bucket = _failures.get(key)
        if bucket is None:
            bucket = deque()
            _failures[key] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)

        if len(bucket) >= threshold:
            _blocked_until[key] = now + duration_sec
            # The block is now authoritative; the counted history is spent.
            _failures.pop(key, None)
            blocked_now = True

    if blocked_now:
        # Emit outside the lock. Structured so operators (and any future alert
        # rule) can see which source was blocked and for how long.
        logger.warning(
            "auth_ip_blocked",
            extra={
                "event": "auth_ip_blocked",
                "ip_bucket": key,
                "threshold": threshold,
                "window_sec": window_sec,
                "block_seconds": duration_sec,
            },
        )
    return blocked_now


def reset_auth_failure_block_state() -> None:
    """Wipe all recorded failures and blocks. Test-only helper."""
    global _sweep_counter
    _failures.clear()
    _blocked_until.clear()
    _sweep_counter = 0
