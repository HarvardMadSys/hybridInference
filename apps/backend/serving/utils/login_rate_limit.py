"""In-memory sliding-window rate limiters for the login and first-run setup endpoints.

Login maintains two independent buckets:

* Per-account: caps password-guessing against a single account
  (``settings.login_rate_limit_per_15min`` attempts per 15 minutes), keyed
  by the identifier the client signed in with — an email address, or the
  login name of an account created without one.
* Per-IP: caps credential-stuffing across many accounts from the same
  source (``settings.login_rate_limit_per_hour_per_ip`` attempts per hour).

First-run setup counts wrong setup codes per IP (``SETUP_ATTEMPTS_PER_15MIN``
per 15 minutes). Only failures are recorded and a correct code is never
refused, so junk sent from an address the operator shares (every browser
behind the console's proxy can) cannot lock the operator out.

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
from serving.utils.request_ip import ClientIpInfo, get_client_ip_info, normalize_ip_bucket

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

# Wrong first-run setup codes an IP may send in any 15-minute window before
# further wrong ones are answered 429.
SETUP_ATTEMPTS_PER_15MIN = 10

_email_attempts: dict[str, deque[float]] = {}
_ip_attempts: dict[str, deque[float]] = {}
_unresolved_attempts: deque[float] = deque()
_setup_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0
_setup_sweep_counter = 0


def _now() -> float:
    return time.time()


def _sweep_inactive(buckets: dict[str, deque[float]], cutoff: float) -> None:
    for key in list(buckets):
        bucket = buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del buckets[key]


async def check_and_record_login(email: str, provenance) -> tuple[bool, str | None]:
    """Record a login attempt and return whether it should be allowed.

    *email* is the identifier the client signed in with: an email address or
    a login name (which never contains ``@``, so the two cannot share a
    bucket). Returns (True, None) if both the per-account and per-IP windows
    have capacity, or (False, "email"|"ip") indicating which bucket tripped.
    Recording happens on entry so probing with varied payloads cannot
    bypass the limit.

    When client provenance is resolved, the per-IP bucket is enforced on
    the client IP. When unresolved (e.g., behind a misconfigured proxy), there
    is no information to distinguish clients behind the shared proxy. A coarse
    global budget applies with an alertable warning rather than collapsing all
    clients onto one per-client bucket. Per-email rate limiting still applies.
    """
    global _sweep_counter

    ip_info = provenance if isinstance(provenance, ClientIpInfo) else get_client_ip_info(provenance)
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


async def record_failed_setup_attempt(ip: str) -> bool:
    """Record a wrong first-run setup code and return whether the IP is still under the limit.

    One sliding 15-minute window per client IP (IPv6 bucketed to its /64).
    Returns False once the IP had already sent ``SETUP_ATTEMPTS_PER_15MIN``
    wrong codes in the window; the caller answers 429 instead of 403. Only
    failures are recorded: a correct code never reaches this function.
    """
    global _setup_sweep_counter

    now = _now()
    cutoff = now - _FIFTEEN_MIN_SECONDS
    ip_key = normalize_ip_bucket(ip)

    async with _lock:
        _setup_sweep_counter += 1
        if _setup_sweep_counter >= _SWEEP_EVERY:
            _setup_sweep_counter = 0
            _sweep_inactive(_setup_attempts, cutoff)

        bucket = _setup_attempts.setdefault(ip_key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        count = len(bucket)
        bucket.append(now)

    return count < SETUP_ATTEMPTS_PER_15MIN


def reset_login_rate_limit_state() -> None:
    """Wipe all recorded attempts (login and setup). Test-only helper."""
    global _sweep_counter, _setup_sweep_counter
    _email_attempts.clear()
    _ip_attempts.clear()
    _unresolved_attempts.clear()
    _setup_attempts.clear()
    _sweep_counter = 0
    _setup_sweep_counter = 0
