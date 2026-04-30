"""In-memory IP-based sliding-window rate limiter for the signup endpoint.

State is a per-process dict and is intentionally lost on restart: signup
abuse is a rate problem, not an audit problem, so durability is not worth
the cost of disk I/O. With multiple uvicorn workers the effective limit
multiplies by the worker count, which is acceptable.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from serving.config.settings import settings

_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400
_SWEEP_EVERY = 1024

_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0


def _now() -> float:
    return time.time()


def _sweep_inactive(cutoff: float) -> None:
    for ip in list(_attempts):
        bucket = _attempts[ip]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del _attempts[ip]


async def check_and_record_signup(ip: str) -> tuple[bool, str | None]:
    """Record a signup attempt and return whether it should be allowed.

    Returns (True, None) if the IP is under both the per-hour and per-day
    limits, or (False, "hour"|"day") indicating which window tripped.
    """
    global _sweep_counter

    now = _now()
    day_cutoff = now - _DAY_SECONDS
    hour_cutoff = now - _HOUR_SECONDS
    per_hour = settings.signup_rate_limit_per_hour
    per_day = settings.signup_rate_limit_per_day

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            _sweep_inactive(day_cutoff)

        bucket = _attempts.get(ip)
        if bucket is None:
            bucket = deque()
            _attempts[ip] = bucket

        while bucket and bucket[0] < day_cutoff:
            bucket.popleft()

        hour_count = sum(1 for t in bucket if t >= hour_cutoff)
        day_count = len(bucket)
        bucket.append(now)

    # Prefer the longer window when both trip so Retry-After reflects the
    # real wait (telling a 24h-blocked client to retry in 1h is wrong).
    if day_count >= per_day:
        return False, "day"
    if hour_count >= per_hour:
        return False, "hour"
    return True, None


def reset_signup_rate_limit_state() -> None:
    """Wipe all recorded attempts. Test-only helper."""
    global _sweep_counter
    _attempts.clear()
    _sweep_counter = 0
