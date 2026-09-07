"""In-memory per-IP auth-failure tracker that auto-blocks abusive sources.

Once a source IP accumulates ``auth_failure_block_threshold`` authentication
failures within ``auth_failure_block_window_sec``, it is blocked at the
API-key auth layer for ``auth_failure_block_duration_sec`` (default: 200
failures in a day → blocked for a day).

Mirrors the in-memory, per-process design of ``serving.utils.login_rate_limit``
and ``serving.utils.signup_rate_limit``: state is a per-process dict and is
intentionally lost on restart. Auth-failure flooding is a rate problem, not an
audit problem, so durability is not worth the disk I/O.

With multiple uvicorn workers each worker counts only the failures it happens
to serve, so the threshold is per worker, not per deployment: a source's
failures spread across ``N`` workers and no single worker reaches
``auth_failure_block_threshold`` until roughly ``N x`` that many have arrived
in total. The block therefore triggers *later* than the configured threshold
suggests, and once one worker blocks, only the share of traffic that worker
serves is refused. That is acceptable for a shed-load defense -- the database
cost still drops, and every worker converges on blocking a sustained source --
but a deployment tuning the threshold, or reading :func:`list_active_blocks`,
has to read both numbers per worker. The single-process default (see
``deploy/systemd/``) has no such spread.

IPv6 sources bucket on their ``/64`` via :func:`normalize_ip_bucket`, so an
attacker cannot dodge the block by rotating RFC 4941 privacy addresses within a
delegated prefix. IPv4 keeps full-address buckets.

Sources listed in ``auth_failure_block_exempt_ips`` (comma-separated IPs or
CIDR ranges) are exempt: their failures are never counted and an existing
block never applies to them. Exemption matches the raw client address, not the
bucket, so an exempt ``/128`` stays reachable even when the rest of its ``/64``
has blocked itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import deque
from dataclasses import dataclass

from serving.config.settings import settings
from serving.utils.logging import get_logger
from serving.utils.request_ip import normalize_ip_bucket

logger = get_logger(__name__)

#: Amortized cleanup cadence: sweep expired state once every N recorded
#: failures rather than on every call. Matches the login/signup limiters.
_SWEEP_EVERY = 1024

#: Per-bucket failure timestamps within the counting window.
_failures: dict[str, deque[float]] = {}
#: Per-bucket wall-clock deadline until which the source is blocked.
_blocked_until: dict[str, float] = {}
_lock = asyncio.Lock()
_sweep_counter = 0

#: Parsed exemption networks, cached against the raw setting string so a
#: changed value (tests, a future runtime reload) reparses instead of serving
#: stale networks.
_exempt_cache: tuple[str, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] | None = None


def _now() -> float:
    return time.time()


def _exempt_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse ``auth_failure_block_exempt_ips`` into networks, cached per value."""
    global _exempt_cache
    raw = settings.auth_failure_block_exempt_ips
    if _exempt_cache is not None and _exempt_cache[0] == raw:
        return _exempt_cache[1]
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            # strict=False so a host address with a prefix ("10.0.1.5/16")
            # exempts its whole network rather than being rejected.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(
                "auth_exempt_ip_invalid",
                extra={"event": "auth_exempt_ip_invalid", "entry": entry},
            )
    _exempt_cache = (raw, tuple(networks))
    return _exempt_cache[1]


def _is_exempt(ip: str) -> bool:
    """True when *ip* falls inside a configured exemption entry.

    Matches the raw client address — unwrapping IPv4-mapped IPv6 literals the
    way :func:`normalize_ip_bucket` does — rather than the bucket, so an IPv4
    exemption is never widened by IPv6 bucketing. An unparseable address is
    never exempt: exemption is an operator grant to a known source, and a
    source we cannot even parse is not one.
    """
    networks = _exempt_networks()
    if not networks:
        return False
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    # Mixed-version containment is defined as False, so one loop covers both.
    return any(parsed in net for net in networks)


def _sweep_inactive(now: float, window_sec: int) -> None:
    """Drop stale failure history and lapsed blocks. Caller holds ``_lock``."""
    cutoff = now - window_sec
    for key in list(_failures):
        bucket = _failures[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del _failures[key]
    for key in list(_blocked_until):
        if _blocked_until[key] <= now:
            del _blocked_until[key]


async def is_ip_blocked(ip: str) -> tuple[bool, int]:
    """Return ``(blocked, retry_after_seconds)`` for *ip*.

    ``(False, 0)`` when the feature is disabled or the IP is clear. A lapsed
    block is cleared lazily on read so an expired entry never lingers as a
    false positive.
    """
    if not settings.auth_failure_block_enabled:
        return False, 0
    # Exemption outranks an existing block: an exempt address inside a blocked
    # /64 bucket must stay reachable, so this is checked before the bucket.
    if _is_exempt(ip):
        return False, 0
    key = normalize_ip_bucket(ip)
    now = _now()
    async with _lock:
        until = _blocked_until.get(key)
        if until is None:
            return False, 0
        if until <= now:
            del _blocked_until[key]
            return False, 0
        # Round up so a sub-second remainder never advertises Retry-After: 0.
        return True, max(1, int(until - now + 0.999))


async def record_auth_failure(ip: str) -> bool:
    """Record one auth failure for *ip*; return True if this call blocked it.

    Blocks the source for ``auth_failure_block_duration_sec`` once its failure
    count within ``auth_failure_block_window_sec`` reaches
    ``auth_failure_block_threshold``. A no-op returning False when the feature
    is disabled or the source is already blocked (callers reject blocked IPs
    before reaching here, so a True return marks the blocking transition).
    """
    global _sweep_counter
    if not settings.auth_failure_block_enabled:
        return False
    # An exempt source accrues no history at all: counting it would only
    # produce a block that is_ip_blocked then has to override on every read.
    if _is_exempt(ip):
        return False

    threshold = settings.auth_failure_block_threshold
    window_sec = settings.auth_failure_block_window_sec
    duration_sec = settings.auth_failure_block_duration_sec

    key = normalize_ip_bucket(ip)
    now = _now()
    cutoff = now - window_sec
    blocked_now = False

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            _sweep_inactive(now, window_sec)

        until = _blocked_until.get(key)
        if until is not None and until > now:
            # Already blocked: the block deadline is fixed, so do not keep
            # accruing history for it. Not a fresh transition.
            return False

        bucket = _failures.get(key)
        if bucket is None:
            bucket = deque()
            _failures[key] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)

        if len(bucket) >= threshold:
            _blocked_until[key] = now + duration_sec
            # The block is now authoritative; the counted history is spent.
            _failures.pop(key, None)
            blocked_now = True

    if blocked_now:
        # Emit outside the lock. Structured so operators (and any future alert
        # rule) can see which source was blocked and for how long.
        logger.warning(
            "auth_ip_blocked",
            extra={
                "event": "auth_ip_blocked",
                "ip_bucket": key,
                "threshold": threshold,
                "window_sec": window_sec,
                "block_seconds": duration_sec,
            },
        )
    return blocked_now


@dataclass(frozen=True)
class ActiveBlock:
    """One source bucket currently refused at the API-key auth layer."""

    ip_bucket: str
    blocked_until: float
    retry_after_sec: int


async def list_active_blocks() -> list[ActiveBlock]:
    """Return the buckets this process is refusing, longest remaining wait first.

    Lapsed entries are dropped as they are read -- the same lazy expiry
    :func:`is_ip_blocked` performs -- so a listing never reports a block that
    would no longer be enforced.

    Exemptions are deliberately *not* applied. An exempt address inside a
    blocked bucket is already let through by :func:`is_ip_blocked`, and
    omitting the bucket here would hide the fact that the rest of its ``/64``
    is still refused, which is exactly what an operator is looking for.

    Returns an empty list when the feature is disabled: nothing is being
    enforced then, whatever state an earlier configuration left behind.

    Per process, like every read in this module (see the module docstring): on
    a multi-worker deployment this is one worker's view, not the deployment's.
    """
    if not settings.auth_failure_block_enabled:
        return []
    now = _now()
    blocks: list[ActiveBlock] = []
    async with _lock:
        for key in list(_blocked_until):
            until = _blocked_until[key]
            if until <= now:
                del _blocked_until[key]
                continue
            blocks.append(
                ActiveBlock(
                    ip_bucket=key,
                    blocked_until=until,
                    # Round up so a sub-second remainder never reports 0,
                    # matching the Retry-After is_ip_blocked advertises.
                    retry_after_sec=max(1, int(until - now + 0.999)),
                )
            )
    blocks.sort(key=lambda b: (-b.blocked_until, b.ip_bucket))
    return blocks


async def clear_block(ip: str) -> bool:
    """Lift an active block on *ip*'s bucket; return whether one was lifted.

    The escape hatch for the situation this defense cannot tell apart from
    abuse: a deployment's own monitor, or a shared egress address, that crossed
    the threshold with a stale credential. Repairing that credential does not
    by itself bring the caller back, because :func:`is_ip_blocked` is consulted
    *before* the presented key is read (``servers/auth.py``) -- the bucket stays
    refused for the remainder of ``auth_failure_block_duration_sec``. This
    shortens that wait without restarting the gateway, which was otherwise the
    only way to clear in-memory state.

    Clears the counted history along with the deadline, so the bucket resumes
    from zero instead of re-blocking on its next single failure. It does not
    grant any lasting immunity: a source still presenting a bad key accrues
    failures again and is refused again on crossing the threshold. Permanent
    immunity is ``auth_failure_block_exempt_ips``.

    Accepts a raw address or an already-normalized bucket key, so an operator
    can paste back exactly what a listing or an ``auth_ip_blocked`` log record
    showed; :func:`normalize_ip_bucket` maps both onto the same key. Returns
    False when nothing was blocked -- already lapsed, never blocked, or the
    feature is off.
    """
    if not settings.auth_failure_block_enabled:
        return False
    key = normalize_ip_bucket(ip)
    now = _now()
    async with _lock:
        until = _blocked_until.pop(key, None)
        # Drop the counted history either way: an operator clearing a bucket
        # that is mid-window (counting up, not yet blocked) means the same
        # thing by it, and leaving 199 spent failures behind would re-block on
        # the next one.
        _failures.pop(key, None)
        lifted = until is not None and until > now
    if lifted:
        logger.warning(
            "auth_ip_block_cleared",
            extra={"event": "auth_ip_block_cleared", "ip_bucket": key},
        )
    return lifted


def reset_auth_failure_block_state() -> None:
    """Wipe all recorded failures, blocks and parsed exemptions. Test-only helper."""
    global _sweep_counter, _exempt_cache
    _failures.clear()
    _blocked_until.clear()
    _sweep_counter = 0
    _exempt_cache = None
