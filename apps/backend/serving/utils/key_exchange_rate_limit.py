"""Limit on failed API key exchanges, for each client address.

``POST /v1/auth/keys`` is public: a one-time code and its PKCE verifier are the
whole credential. A code is 256 random bits and its first exchange spends it,
so guessing one is hopeless; what an unlimited caller can still do is make the
gateway look codes up as fast as it can send them. Failed exchanges are
counted per client address over an hour, and an address past
:data:`FAILURES_PER_HOUR` is refused before any code is looked up until its
earlier failures age out.

A successful exchange is not counted: an application redeeming its users'
codes from one server is never slowed by its own successes.

A client whose address could not be resolved -- every caller behind a proxy
the gateway is not told to trust -- is not counted at all, as the mail limits
do: one window shared by every such caller would let any one of them stop the
others' exchanges. IPv6 addresses count per /64 (:func:`normalize_ip_bucket`).

State is per process and lost on restart, like the login and sign-on limits;
with several workers the limit multiplies by their number.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING

from serving.utils.request_ip import normalize_ip_bucket

if TYPE_CHECKING:
    from serving.utils.request_ip import ClientIpInfo

#: Failed exchanges one client address may make in an hour.
FAILURES_PER_HOUR = 30

_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024

_failures: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_calls = 0


def _now() -> float:
    return time.monotonic()


def _key(client: ClientIpInfo) -> str | None:
    return normalize_ip_bucket(client.client_ip) if client.resolved else None


def _window(key: str, cutoff: float) -> deque[float]:
    bucket = _failures.setdefault(key, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return bucket


async def exchange_refused(client: ClientIpInfo) -> bool:
    """Whether *client* has failed too many exchanges this hour to try again yet."""
    key = _key(client)
    if key is None:
        return False
    async with _lock:
        return len(_window(key, _now() - _HOUR_SECONDS)) >= FAILURES_PER_HOUR


async def record_exchange_failure(client: ClientIpInfo) -> None:
    """Count one failed exchange against *client*'s address."""
    global _calls
    key = _key(client)
    if key is None:
        return
    now = _now()
    cutoff = now - _HOUR_SECONDS
    async with _lock:
        _calls += 1
        if _calls % _SWEEP_EVERY == 0:
            for stale in [
                k for k, bucket in _failures.items() if not bucket or bucket[-1] < cutoff
            ]:
                del _failures[stale]
        _window(key, cutoff).append(now)


def reset_key_exchange_rate_limit_state() -> None:
    """Forget every address's failures (tests)."""
    global _calls
    _failures.clear()
    _calls = 0
