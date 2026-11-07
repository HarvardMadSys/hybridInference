"""In-memory sliding-window rate limiters for the login and first-run setup endpoints.

Login maintains five buckets. A sign-in from a browser that has not signed in
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
and is counted in the last two buckets alone:

* Per device: its own ``settings.login_rate_limit_per_15min`` attempts per
  15 minutes, keyed by the cookie entry's nonce. While the shared window
  below has room, such an attempt neither counts in nor is refused by the
  other three, so no one else's attempts, from any address, lock that
  browser out.
* Per account and password, every browser recognized under it together:
  ``settings.login_account_rate_limit_per_15min`` attempts per 15 minutes.
  Every successful sign-in issues a fresh entry, and with it a fresh device
  window, so the per-device bucket alone let an account's owner sign in
  without limit by presenting the newest entry each time, each sign-in
  running Argon2id and writing a session. Only attempts carrying a valid
  entry for the account count here -- an entry is issued only to a browser
  that gave the password -- so no one without one can use it up; and like
  the account-wide bucket above it counts only the attempts a device's own
  window lets through, so one browser adds at most its own allowance. It is
  kept per password epoch (the entry carries it): entries issued before the
  password was last changed or reset, which whoever knew the old password
  may still hold, share a window apart from the browsers that signed in
  with the new one. An attempt this window cannot take is limited like any
  other client's instead of refused, so filling it locks no browser out.

First-run setup counts wrong setup codes per IP (``SETUP_ATTEMPTS_PER_15MIN``
per 15 minutes). Only failures are recorded and a correct code is never
refused, so junk sent from an address the operator shares (every browser
behind the console's proxy can) cannot lock the operator out.

State is per-process and lost on restart; with multiple uvicorn workers
the effective limit multiplies by worker count, which is acceptable for
this defense (the goal is throttling, not audit). Mirrors the design of
``serving.utils.signup_rate_limit``.

What one source can make the process hold is bounded. An address past its
own window is refused before anything is looked up or kept for the name it
tried, and a refused attempt adds nothing a bucket does not already count:
each keeps at most as many attempts as its limit, the most it ever needs to
count. Each window keeps at most :data:`_MAX_BUCKETS` buckets, in the order
they were last used, so expired ones are dropped from the front as new ones
are made, with no scan of the rest on the event loop.
"""

from __future__ import annotations

import asyncio
import time
from bisect import bisect_left
from collections import OrderedDict, deque
from itertools import islice
from typing import TYPE_CHECKING, TypeVar

from serving.config.settings import settings
from serving.utils.request_ip import normalize_ip_bucket

if TYPE_CHECKING:
    from serving.utils.device_cookie import RecognizedDevice

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

# Wrong first-run setup codes an IP may send in any 15-minute window before
# further wrong ones are answered 429.
SETUP_ATTEMPTS_PER_15MIN = 10

#: Buckets each login window keeps at most. Making one more replaces, among the
#: :data:`_EVICTION_SAMPLE` used longest ago, the one counting the fewest
#: attempts: a flood of fresh names or addresses, an attempt or two each,
#: displaces its own buckets, not the full one of an account under attack.
_MAX_BUCKETS = 50_000
_EVICTION_SAMPLE = 8

#: How often one address's refusals for one reason are each worth a
#: ``login_events`` row: once a minute, not once an attempt.
REFUSAL_RECORD_INTERVAL_SEC = 60

# Login attempts, oldest first, keyed by device-cookie nonce, by (identifier,
# password epoch) for the attempts that carried a device entry, by
# (identifier, address bucket), by identifier alone, and by address bucket
# alone. Each is in the order its buckets were last used: an OrderedDict,
# whose front is reached in constant time however many have left it.
_device_attempts: OrderedDict[str, list[float]] = OrderedDict()
_account_device_attempts: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_account_address_attempts: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_account_attempts: OrderedDict[str, list[float]] = OrderedDict()
_ip_attempts: OrderedDict[str, list[float]] = OrderedDict()
# When a refusal was last recorded, by (address bucket, reason).
_refusals_recorded: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_setup_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
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


def _window(buckets: OrderedDict[_Key, list[float]], key: _Key, cutoff: float) -> list[float]:
    """Return *key*'s bucket, created if new, without the attempts before *cutoff*.

    The bucket moves to the end of *buckets*, which so stay in the order they
    were last used: a new bucket is made after dropping expired ones from the
    front, and over :data:`_MAX_BUCKETS` after evicting one (see there).
    """
    bucket = buckets.get(key)
    if bucket is None:
        _make_room(buckets, cutoff)
        bucket = buckets[key] = []
    else:
        buckets.move_to_end(key)
        if bucket and bucket[0] < cutoff:
            del bucket[: bisect_left(bucket, cutoff)]
    return bucket


def _make_room(buckets: OrderedDict[_Key, list[float]], cutoff: float) -> None:
    """Drop up to two expired buckets from the front, then one more if still full."""
    for _ in range(2):
        if not buckets:
            return
        oldest, attempts = next(iter(buckets.items()))
        if attempts and attempts[-1] >= cutoff:
            break
        del buckets[oldest]
    if len(buckets) >= _MAX_BUCKETS:
        sample = list(islice(buckets, _EVICTION_SAMPLE))
        del buckets[min(sample, key=lambda key: len(buckets[key]))]


def _record(bucket: list[float], now: float, keep: int) -> None:
    """Add an attempt at *now*, keeping the latest *keep*: a window only counts to its limit."""
    bucket.append(now)
    if len(bucket) > keep:
        del bucket[: len(bucket) - keep]


async def check_and_record_login(
    email: str, ip: str, *, device: RecognizedDevice | str | None = None
) -> tuple[bool, str | None]:
    """Record a login attempt and return whether it should be allowed.

    *email* is the identifier the client signed in with: an email address or
    a login name (which never contains ``@``, so the two cannot share a
    bucket). *device* is the request's valid device-cookie entry for that
    identifier (:func:`serving.utils.device_cookie.recognized_device`), when
    it carries one; the attempt is then counted in that device's window and
    the window its account's browsers share under one password alone (a bare
    key string stands for an entry with no epoch). When that shared window
    is full the attempt is counted like one without an entry instead. Returns
    (True, None) if every window has capacity, or (False, reason) naming the
    bucket that tripped: ``"device"`` (this browser), ``"ip"`` (this address,
    any account), ``"email"`` (this account from this address) or
    ``"account"`` (this account from every address together).

    Recording happens on entry, in the same critical section as the check,
    so probing with varied payloads or concurrent requests cannot bypass the
    limit. The per-IP bucket, and the per-device one, record every attempt,
    refused or not, so a source that keeps trying stays refused; the
    per-account-per-address bucket records every attempt the per-IP one lets
    through. The two account-wide buckets record only the attempts that are
    let through: one address's or one browser's refused attempts never use
    them up, which is what keeps that source from locking the owner out
    everywhere.
    """
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
        if device is not None:
            device_key, epoch = (device, "") if isinstance(device, str) else device
            # Only the backend issues device entries, so these keys are as
            # many as the browsers that signed in, not as an attacker likes.
            device_bucket = _window(_device_attempts, device_key, window_cutoff)
            device_count = len(device_bucket)
            _record(device_bucket, now, per_account_address)
            if device_count >= per_account_address:
                return False, "device"
            # Each sign-in issues a new entry, and a new device window with
            # it; this one is what bounds an account that presents its newest
            # entry every time. Only attempts its devices let through reach it.
            devices_bucket = _window(_account_device_attempts, (account_key, epoch), window_cutoff)
            if len(devices_bucket) < per_account:
                devices_bucket.append(now)
                return True, None
            # Full: entries this password's other holders kept may be what
            # filled it. Limited like any other client's attempt, not refused.

        # The address first, and on entry so varied payloads cannot bypass
        # the limit. Past it, nothing is kept for the name it tried: a source
        # sending a new name each time adds nothing per name. Checked ahead of
        # the 15-minute windows, it also makes Retry-After a realistic wait
        # (telling an hour-blocked client to retry in 15 min would be wrong).
        ip_bucket = _window(_ip_attempts, ip_key, ip_cutoff)
        ip_count = len(ip_bucket)
        _record(ip_bucket, now, per_ip)
        if ip_count >= per_ip:
            return False, "ip"

        address_bucket = _window(_account_address_attempts, (account_key, ip_key), window_cutoff)
        address_count = len(address_bucket)
        _record(address_bucket, now, per_account_address)
        if address_count >= per_account_address:
            return False, "email"

        # Only attempts both per-address buckets let through reach this one,
        # so each address adds at most its own allowance to the account's.
        account_bucket = _window(_account_attempts, account_key, window_cutoff)
        if len(account_bucket) >= per_account:
            return False, "account"
        account_bucket.append(now)

    return True, None


def refusal_worth_recording(ip: str, reason: str) -> bool:
    """Whether a refused sign-in from *ip* should be written to ``login_events``.

    True for the first refusal for *reason* from the address in
    :data:`REFUSAL_RECORD_INTERVAL_SEC`: a source that keeps trying past a
    limit is refused at no cost but its row, and one a minute still records
    that it did, without a database write per attempt.
    """
    now = _now()
    key = (normalize_ip_bucket(ip), reason)
    bucket = _window(_refusals_recorded, key, now - REFUSAL_RECORD_INTERVAL_SEC)
    if bucket:
        return False
    bucket.append(now)
    return True


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
    global _setup_sweep_counter
    _device_attempts.clear()
    _account_device_attempts.clear()
    _account_address_attempts.clear()
    _account_attempts.clear()
    _ip_attempts.clear()
    _refusals_recorded.clear()
    _setup_attempts.clear()
    _setup_sweep_counter = 0
