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

When client provenance is resolved, rate-limits on the client IP.
When unresolved (e.g., behind a misconfigured proxy), there is no information
to distinguish clients behind the shared proxy. In that case, the canonical
resolver emits the alertable warning and a coarse global process-local budget
applies rather than collapsing all clients onto one per-client bucket;
per-email limiting still applies.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from serving.config.settings import settings
from serving.utils.request_ip import get_client_ip_info, normalize_ip_bucket

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

_email_attempts: dict[str, deque[float]] = {}
_ip_attempts: dict[str, deque[float]] = {}
_unresolved_attempts: deque[float] = deque()
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


async def check_and_record_login(email: str, request) -> tuple[bool, str | None]:
    """Record a login attempt and return whether it should be allowed.

    Returns (True, None) if both the per-email and per-IP windows have
    capacity, or (False, "email"|"ip") indicating which bucket tripped.
    Recording happens on entry so probing with varied payloads cannot
    bypass the limit.

    When client provenance is resolved, the per-IP bucket is enforced on
    the client IP. When unresolved (e.g., behind a misconfigured proxy), there
    is no information to distinguish clients behind the shared proxy. A coarse
    global budget applies with an alertable warning rather than collapsing all
    clients onto one per-client bucket. Per-email rate limiting still applies.
    """
    global _sweep_counter

    ip_info = get_client_ip_info(request)
    # Normalize email so case variants share the same bucket.
    email_key = email.strip().lower()

    # Use resolved client IP if available; otherwise skip per-IP limiting.
    ip_key = f"client:{normalize_ip_bucket(ip_info.client_ip)}" if ip_info.resolved else None
    unresolved = not ip_info.resolved

    now = _now()
    email_cutoff = now - _FIFTEEN_MIN_SECONDS
    ip_cutoff = now - _HOUR_SECONDS
    per_email = settings.login_rate_limit_per_15min
    per_ip = settings.login_rate_limit_per_hour_per_ip
    unresolved_count = 0

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            # Sweep each bucket using its own oldest cutoff (ip is the longer
            # window, so use it for ip_attempts).
            _sweep_inactive(_email_attempts, email_cutoff)
            if _ip_attempts:
                _sweep_inactive(_ip_attempts, ip_cutoff)

        email_bucket = _email_attempts.get(email_key)
        if email_bucket is not None:
            while email_bucket and email_bucket[0] < email_cutoff:
                email_bucket.popleft()
            if not email_bucket:
                del _email_attempts[email_key]
                email_bucket = None

        email_count = len(email_bucket) if email_bucket else 0

        ip_bucket = None
        if ip_key is not None:
            ip_bucket = _ip_attempts.get(ip_key)
            if ip_bucket is not None:
                while ip_bucket and ip_bucket[0] < ip_cutoff:
                    ip_bucket.popleft()
                if not ip_bucket:
                    del _ip_attempts[ip_key]
                    ip_bucket = None

            ip_count = len(ip_bucket) if ip_bucket else 0
        else:
            ip_count = 0  # No per-IP limiting when unresolved

        if unresolved:
            while _unresolved_attempts and _unresolved_attempts[0] < ip_cutoff:
                _unresolved_attempts.popleft()
            unresolved_count = len(_unresolved_attempts)
        if unresolved and unresolved_count >= settings.unresolved_login_rate_limit_per_hour:
            return False, "unresolved"
        if ip_count >= per_ip:
            return False, "ip"
        if email_count >= per_email:
            return False, "email"

        if email_bucket is None:
            email_bucket = deque()
            _email_attempts[email_key] = email_bucket
        email_bucket.append(now)
        if ip_bucket is not None:
            ip_bucket.append(now)
        elif ip_key is not None:
            ip_bucket = deque()
            _ip_attempts[ip_key] = ip_bucket
            ip_bucket.append(now)
        if unresolved:
            _unresolved_attempts.append(now)
    return True, None


def reset_login_rate_limit_state() -> None:
    """Wipe all recorded attempts. Test-only helper."""
    global _sweep_counter
    _email_attempts.clear()
    _ip_attempts.clear()
    _unresolved_attempts.clear()
    _sweep_counter = 0
