"""In-memory sliding-window rate limiter for the signup endpoint.

State is a per-process dict and is intentionally lost on restart: signup
abuse is a rate problem, not an audit problem, so durability is not worth
the cost of disk I/O. With multiple uvicorn workers the effective limit
multiplies by the worker count, which is acceptable.

When client provenance is resolved, rate-limits on the client IP.
When unresolved (e.g., behind a misconfigured proxy), there is no information
to distinguish clients behind the shared proxy. In that case, the canonical
resolver emits the alertable warning and the limiter uses a coarse global
process-local budget rather than collapsing all clients onto one per-client
bucket. Deployments that require trustworthy provenance can set
``SIGNUP_REQUIRE_RESOLVED_CLIENT_IP=1`` to fail closed.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from serving.config.settings import settings
from serving.utils.request_ip import get_client_ip_info, normalize_ip_bucket

_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400
_SWEEP_EVERY = 1024

_attempts: dict[str, deque[float]] = {}
_unresolved_attempts: deque[float] = deque()
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


async def check_and_record_signup(request) -> tuple[bool, str | None]:
    """Record a signup attempt and return whether it should be allowed.

    Returns (True, None) if the IP is under both the per-hour and per-day
    limits, or (False, "hour"|"day") indicating which window tripped.

    When client provenance is resolved, rate-limits on the client IP.
    When unresolved (e.g., behind a misconfigured proxy), there is no
    information to distinguish clients behind the shared proxy. The warning
    signal is emitted and a coarse global budget limits the traffic without
    pretending the proxy is a client identity. A deployment can fail closed
    instead.
    """
    global _sweep_counter

    ip_info = get_client_ip_info(request)

    # When client provenance is unresolved, we cannot distinguish clients
    # behind a shared proxy. The canonical resolver already emitted the
    # per-request alertable signal; let deployments that require trustworthy
    # proxy provenance fail closed for public signup.
    if not ip_info.resolved:
        if settings.signup_require_resolved_client_ip:
            return False, "unresolved"
        now = _now()
        hour_cutoff = now - _HOUR_SECONDS
        day_cutoff = now - _DAY_SECONDS
        hour_limit = settings.unresolved_signup_rate_limit_per_hour
        day_limit = settings.unresolved_signup_rate_limit_per_day
        async with _lock:
            while _unresolved_attempts and _unresolved_attempts[0] < day_cutoff:
                _unresolved_attempts.popleft()
            day_count = len(_unresolved_attempts)
            hour_count = sum(1 for t in _unresolved_attempts if t >= hour_cutoff)
            _unresolved_attempts.append(now)
        if day_count >= day_limit:
            return False, "unresolved_day"
        if hour_count >= hour_limit:
            return False, "unresolved_hour"
        return True, None

    ip_key = f"client:{normalize_ip_bucket(ip_info.client_ip)}"
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

        bucket = _attempts.get(ip_key)
        if bucket is None:
            bucket = deque()
            _attempts[ip_key] = bucket

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
    _unresolved_attempts.clear()
    _sweep_counter = 0
