"""In-memory sliding-window rate limiter for the login endpoint.

Maintains two independent buckets:

* Per-email: caps password-guessing against a single account
  (``settings.login_rate_limit_per_15min`` attempts per 15 minutes).
* Per-IP: caps credential-stuffing across many accounts from the same
  source (``settings.login_rate_limit_per_hour_per_ip`` attempts per hour).

State is per-process and lost on restart; with multiple uvicorn workers
the effective limit multiplies by worker count, which is acceptable for
this defense (the goal is throttling, not audit). Mirrors the design of
``serving.utils.signup_rate_limit``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from serving.config.settings import settings

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

_email_attempts: dict[str, deque[float]] = {}
_ip_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0


def _now() -> float:
    return time.time()


def _sweep_inactive(buckets: dict[str, deque[float]], cutoff: float) -> None:
    for key in list(buckets):
        bucket = buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del buckets[key]


async def check_and_record_login(
    email: str, ip: str
) -> tuple[bool, str | None]:
    """Record a login attempt and return whether it should be allowed.

    Returns (True, None) if both the per-email and per-IP windows have
    capacity, or (False, "email"|"ip") indicating which bucket tripped.
    Recording happens on entry so probing with varied payloads cannot
    bypass the limit.
    """
    global _sweep_counter

    now = _now()
    email_cutoff = now - _FIFTEEN_MIN_SECONDS
    ip_cutoff = now - _HOUR_SECONDS
    per_email = settings.login_rate_limit_per_15min
    per_ip = settings.login_rate_limit_per_hour_per_ip

    # Normalize email so case variants share the same bucket.
    email_key = email.strip().lower()

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            # Sweep each bucket using its own oldest cutoff (ip is the longer
            # window, so use it for ip_attempts).
            _sweep_inactive(_email_attempts, email_cutoff)
            _sweep_inactive(_ip_attempts, ip_cutoff)

        email_bucket = _email_attempts.get(email_key)
        if email_bucket is None:
            email_bucket = deque()
            _email_attempts[email_key] = email_bucket
        while email_bucket and email_bucket[0] < email_cutoff:
            email_bucket.popleft()

        ip_bucket = _ip_attempts.get(ip)
        if ip_bucket is None:
            ip_bucket = deque()
            _ip_attempts[ip] = ip_bucket
        while ip_bucket and ip_bucket[0] < ip_cutoff:
            ip_bucket.popleft()

        email_count = len(email_bucket)
        ip_count = len(ip_bucket)

        # Record on entry so varied payloads cannot bypass the limit.
        email_bucket.append(now)
        ip_bucket.append(now)

    # Prefer the longer window when both trip so Retry-After reflects a
    # realistic wait (telling an hour-blocked client to retry in 15 min
    # would be wrong).
    if ip_count >= per_ip:
        return False, "ip"
    if email_count >= per_email:
        return False, "email"
    return True, None


def reset_login_rate_limit_state() -> None:
    """Wipe all recorded attempts. Test-only helper."""
    global _sweep_counter
    _email_attempts.clear()
    _ip_attempts.clear()
    _sweep_counter = 0
