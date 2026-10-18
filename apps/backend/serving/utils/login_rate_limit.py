"""In-memory sliding-window rate limiters for the login and first-run setup endpoints.

Login maintains four buckets. A sign-in from a browser that has not signed in
to the account before is counted in three of them:

* Per account per client address: caps password-guessing against one account
  from one source (``settings.login_rate_limit_per_15min`` attempts per 15
  minutes), keyed by the identifier the client signed in with — an email
  address, or the login name of an account created without one — together
  with the client address, IPv6 bucketed to its /64. Whoever guesses from
  their own address uses up only that address's attempts: the account's
  owner, signing in from another, is not locked out with them.
* Per account: caps guessing spread across many addresses
  (``settings.login_account_rate_limit_per_15min`` attempts per 15 minutes,
  every address together). It counts only the attempts that are let through,
  so one address adds at most its per-account allowance, and filling it takes
  ten addresses at the defaults.
* Per-IP: caps credential-stuffing across many accounts from the same
  source (``settings.login_rate_limit_per_hour_per_ip`` attempts per hour).

The per-address buckets tell clients apart only as well as the client address
does. A backend that cannot resolve one — every browser reaching it through
the console's proxy, without trusted-proxy settings, is ``"unknown"`` — puts
all of them on one address, where the per-account-per-address bucket is per
account again. A browser that has signed in to the account before presents
the device cookie that sign-in gave it (:mod:`serving.utils.device_cookie`)
and is counted in the fourth bucket alone:

* Per device: its own ``settings.login_rate_limit_per_15min`` attempts per
  15 minutes, keyed by the cookie entry's nonce. Such an attempt neither
  counts in nor is refused by the other three, so no one else's attempts,
  from any address, lock that browser out.

First-run setup counts wrong setup codes per IP (``SETUP_ATTEMPTS_PER_15MIN``
per 15 minutes). Only failures are recorded and a correct code is never
refused, so junk sent from an address the operator shares (every browser
behind the console's proxy can) cannot lock the operator out.

State is per-process and lost on restart; with multiple uvicorn workers
the effective limit multiplies by worker count, which is acceptable for
this defense (the goal is throttling, not audit). Mirrors the design of
``serving.utils.signup_rate_limit``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TypeVar

from serving.config.settings import settings
from serving.utils.request_ip import normalize_ip_bucket

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

# Wrong first-run setup codes an IP may send in any 15-minute window before
# further wrong ones are answered 429.
SETUP_ATTEMPTS_PER_15MIN = 10

# Login attempts keyed by device-cookie nonce, by (identifier, address bucket),
# by identifier alone, and by address bucket alone.
_device_attempts: dict[str, deque[float]] = {}
_account_address_attempts: dict[tuple[str, str], deque[float]] = {}
_account_attempts: dict[str, deque[float]] = {}
_ip_attempts: dict[str, deque[float]] = {}
_setup_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0
_setup_sweep_counter = 0

_Key = TypeVar("_Key")


def _now() -> float:
    return time.time()


def _sweep_inactive(buckets: dict[_Key, deque[float]], cutoff: float) -> None:
    for key in list(buckets):
        bucket = buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del buckets[key]


def _window(buckets: dict[_Key, deque[float]], key: _Key, cutoff: float) -> deque[float]:
    """Return *key*'s bucket, created if new, without the attempts before *cutoff*."""
    bucket = buckets.setdefault(key, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return bucket


async def check_and_record_login(
    email: str, ip: str, *, device: str | None = None
) -> tuple[bool, str | None]:
    """Record a login attempt and return whether it should be allowed.

    *email* is the identifier the client signed in with: an email address or
    a login name (which never contains ``@``, so the two cannot share a
    bucket). *device* is the key of a valid device-cookie entry for that
    identifier (:func:`serving.utils.device_cookie.recognized_device`), when
    the request carries one; the attempt is then counted in that device's
    window alone. Returns (True, None) if every window has capacity, or
    (False, reason) naming the bucket that tripped: ``"device"`` (this
    browser), ``"ip"`` (this address, any account), ``"email"`` (this account
    from this address) or ``"account"`` (this account from every address
    together).

    Recording happens on entry, in the same critical section as the check,
    so probing with varied payloads or concurrent requests cannot bypass the
    limit. Both per-address buckets record every attempt, refused or not, so
    an address that keeps trying stays refused. The account-wide bucket
    records only the attempts that are let through: one address's refused
    attempts never use it up, which is what keeps that address from locking
    the owner out everywhere.
    """
    global _sweep_counter

    now = _now()
    window_cutoff = now - _FIFTEEN_MIN_SECONDS
    ip_cutoff = now - _HOUR_SECONDS
    per_account_address = settings.login_rate_limit_per_15min
    per_account = settings.login_account_rate_limit_per_15min
    per_ip = settings.login_rate_limit_per_hour_per_ip

    # Normalize the identifier so case variants share the same bucket.
    account_key = email.strip().lower()
    # Normalize IPv6 to its /64 so rotating within a delegated prefix cannot
    # reset either per-address window.
    ip_key = normalize_ip_bucket(ip)

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            # Sweep each bucket using its own window's cutoff (ip is the
            # longer window, so use it for ip_attempts).
            _sweep_inactive(_device_attempts, window_cutoff)
            _sweep_inactive(_account_address_attempts, window_cutoff)
            _sweep_inactive(_account_attempts, window_cutoff)
            _sweep_inactive(_ip_attempts, ip_cutoff)

        if device is not None:
            # Only the backend issues device entries, so these keys are as
            # many as the browsers that signed in, not as an attacker likes.
            device_bucket = _window(_device_attempts, device, window_cutoff)
            device_count = len(device_bucket)
            device_bucket.append(now)
            if device_count >= per_account_address:
                return False, "device"
            return True, None

        address_bucket = _window(_account_address_attempts, (account_key, ip_key), window_cutoff)
        ip_bucket = _window(_ip_attempts, ip_key, ip_cutoff)
        address_count = len(address_bucket)
        ip_count = len(ip_bucket)

        # Record on entry so varied payloads cannot bypass the limit.
        address_bucket.append(now)
        ip_bucket.append(now)

        # Prefer the longer window when both trip so Retry-After reflects a
        # realistic wait (telling an hour-blocked client to retry in 15 min
        # would be wrong).
        if ip_count >= per_ip:
            return False, "ip"
        if address_count >= per_account_address:
            return False, "email"

        # Only attempts both per-address buckets let through reach this one,
        # so each address adds at most its own allowance to the account's.
        account_bucket = _window(_account_attempts, account_key, window_cutoff)
        if len(account_bucket) >= per_account:
            return False, "account"
        account_bucket.append(now)

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
    _device_attempts.clear()
    _account_address_attempts.clear()
    _account_attempts.clear()
    _ip_attempts.clear()
    _setup_attempts.clear()
    _sweep_counter = 0
    _setup_sweep_counter = 0
