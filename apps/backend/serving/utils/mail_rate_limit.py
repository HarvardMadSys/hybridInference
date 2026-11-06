"""In-memory sliding-window limits for the anonymous routes that send mail.

``POST /auth/forgot-password`` and ``POST /auth/resend-verification`` take an
address from anyone and mail a link to the account that has it. Unlimited, one
loop floods a person's inbox and spends the deployment's mail quota and sender
reputation. Each request is counted in two one-hour windows:

* Per address and kind of mail: :data:`MAIL_REQUESTS_PER_ADDRESS_PER_HOUR`,
  keyed by the normalized address the request names, whether or not an
  account has it. A refusal therefore says nothing about whether one does,
  and an inbox receives at most that many of each kind an hour.
* Per client address: :data:`MAIL_REQUESTS_PER_IP_PER_HOUR`, both kinds
  together, IPv6 bucketed to its /64 (:func:`normalize_ip_bucket`), so one
  source cannot spread a flood over many inboxes.

Only requests both windows let through are recorded, as the signup limiter
does: a refused request never extends its own refusal. Whoever keeps asking for
someone else's address cannot keep that person from resetting a password
either, because every request let through mails a working link to that
person's own inbox (the route resends a live link rather than minting another).

A backend that cannot resolve client addresses -- every browser behind the
console's proxy without trusted-proxy settings is ``"unknown"`` -- counts every
client in one per-address window, which then caps the deployment as a whole.

State is per-process and lost on restart; with several uvicorn workers the
limits multiply by the worker count. Mirrors
:mod:`serving.utils.signup_rate_limit`.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Literal

from serving.utils.request_ip import normalize_ip_bucket

MailKind = Literal["password_reset", "verification"]

#: Requests one address may receive of one kind of mail in any hour.
MAIL_REQUESTS_PER_ADDRESS_PER_HOUR = 3
#: Requests for either kind of mail one client address may make in any hour.
MAIL_REQUESTS_PER_IP_PER_HOUR = 10

_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

_address_requests: dict[tuple[str, str], deque[float]] = {}
_ip_requests: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0


def _now() -> float:
    return time.time()


def _sweep_inactive(buckets: dict, cutoff: float) -> None:
    for key in list(buckets):
        bucket = buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del buckets[key]


def _window(buckets: dict, key: object, cutoff: float) -> deque[float]:
    """Return *key*'s bucket, created if new, without the requests before *cutoff*."""
    bucket = buckets.setdefault(key, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return bucket


async def check_and_record_mail_request(
    kind: MailKind, email: str, ip: str
) -> tuple[bool, str | None]:
    """Return whether a request to mail *kind* to *email* from *ip* may go ahead.

    Returns (True, None) and records the request when both windows have room,
    or (False, reason) without recording it: ``"ip"`` for the client address,
    ``"address"`` for the address asked about. The check runs before the
    account is looked up, so the answer is the same whether one exists.
    """
    global _sweep_counter

    now = _now()
    cutoff = now - _HOUR_SECONDS
    address_key = (kind, email.strip().lower())
    ip_key = normalize_ip_bucket(ip)

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            _sweep_inactive(_address_requests, cutoff)
            _sweep_inactive(_ip_requests, cutoff)

        ip_bucket = _window(_ip_requests, ip_key, cutoff)
        if len(ip_bucket) >= MAIL_REQUESTS_PER_IP_PER_HOUR:
            return False, "ip"
        address_bucket = _window(_address_requests, address_key, cutoff)
        if len(address_bucket) >= MAIL_REQUESTS_PER_ADDRESS_PER_HOUR:
            return False, "address"
        ip_bucket.append(now)
        address_bucket.append(now)

    return True, None


def reset_mail_rate_limit_state() -> None:
    """Wipe all recorded requests. Test-only helper."""
    global _sweep_counter
    _address_requests.clear()
    _ip_requests.clear()
    _sweep_counter = 0
