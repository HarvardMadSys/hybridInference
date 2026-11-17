"""Limit on starting a single sign-on, for each address and for each account.

Starting a sign-on tests no credential: the identity provider does that, and
throttles it. What a start costs here is one flow row, written before anyone
has signed in, so the limit bounds those writes from one address. It is kept in
process memory, like the login limits, and is generous enough for many people
behind one campus or office address.

A start made by a signed-in account (a confirmation, a link from Settings) is
counted for the account instead. An address tells clients apart only as well
as the gateway can see it: behind a proxy it is not told to trust, every
browser has the proxy's address, and anyone's starts would use up the
allowance a signed-in user needs.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING

from serving.utils.request_ip import normalize_ip_bucket

if TYPE_CHECKING:
    from serving.utils.request_ip import ClientIpInfo

#: Starts allowed from one address in an hour.
STARTS_PER_HOUR = 120

_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024
#: Clients whose address could not be resolved share this one bucket.
_UNRESOLVED = "unresolved"

_buckets: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_calls = 0


def _now() -> float:
    return time.monotonic()


async def check_and_record_sso_start(client: ClientIpInfo, *, account: str | None = None) -> bool:
    """Count one start; False when the hour's allowance is spent.

    Counted for *account* when a signed-in account makes it, and for the
    client's address otherwise. Refused attempts are not recorded, so a source
    that keeps trying is let through again as its earlier starts age out of
    the window.
    """
    global _calls
    if account is not None:
        key = f"account:{account}"
    else:
        key = normalize_ip_bucket(client.client_ip) if client.resolved else _UNRESOLVED
    now = _now()
    cutoff = now - _HOUR_SECONDS
    async with _lock:
        _calls += 1
        if _calls % _SWEEP_EVERY == 0:
            for stale in [k for k, bucket in _buckets.items() if not bucket or bucket[-1] < cutoff]:
                del _buckets[stale]
        bucket = _buckets.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= STARTS_PER_HOUR:
            return False
        bucket.append(now)
        return True


def reset_sso_rate_limit_state() -> None:
    """Forget every address's starts (tests)."""
    global _calls
    _buckets.clear()
    _calls = 0
