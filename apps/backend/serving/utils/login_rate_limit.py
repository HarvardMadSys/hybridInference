"""In-memory sliding-window rate limits for login and first-run setup.

Login distinguishes admitted attempts from refusals. An admitted login is one
allowed through to password verification; only admitted attempts enter the
account/address, account-wide, and IP-wide windows. A refusal caused by the
account/address window increments a separate, saturating per-IP counter. The
IP check combines that pressure with admitted IP attempts, preserving abuse
escalation without growing admission queues or extending account-wide windows.

Account-wide refusals add to a separate, fixed-expiry per-IP pressure count.
Repeated polling of a locked account therefore reaches the existing IP-wide
threshold without extending the account's admission window. Once address or
account refusals reach that threshold, the IP-wide policy applies to every
account at that address. Recognized devices retain their independent window
and do not consume or get blocked by the other login windows.

Login bucket maps are bounded. Expired keys are reclaimed first. New
account/address keys are not accounted through a shared overflow window: once
that map is full, a distributed attacker could fill its slots and deny legitimate
new sign-ins. That scope fails open while the account-wide and IP windows still
apply. Other scopes use fixed-size overflow windows selected by a
process-randomized hash. Collisions can conservatively rate-limit unrelated
keys for the remainder of a window, but overflow requests are never admitted
without accounting. Live entries are never evicted. Recognized devices keep
their independent escape hatch: existing device keys retain their own window,
while new device keys beyond the per-account tracking cap share that account's
bounded overflow window.

Unresolved provenance (no routable client address can be attributed, e.g.
every browser behind the console proxy in a default Compose deployment) does
not receive a synthetic per-client address. It keeps the per-account/address
window, keyed on the shared ``"unresolved"`` address bucket together with the
account-wide and the coarse hourly admission budgets. Setup-code accounting
is unchanged. State is process-local and lost on restart, as with
:mod:`serving.utils.signup_rate_limit`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from serving.config.settings import settings
from serving.utils.request_ip import ClientIpInfo, get_client_ip_info, normalize_ip_bucket

if TYPE_CHECKING:
    from fastapi import Request

_FIFTEEN_MIN_SECONDS = 15 * 60
_HOUR_SECONDS = 3600
_SWEEP_EVERY = 1024
_CAPACITY_SWEEP_INTERVAL_SECONDS = 60
# Bound attacker-controlled login identities in each process-local map. When a
# map is full, expired keys are swept first; new keys then use fixed-cardinality
# hashed overflow windows rather than being admitted without accounting.
# Live entries are never evicted from the attacker-populated maps.
_MAX_ACTIVE_BUCKETS = 20_000
_MAX_DEVICES_PER_ACCOUNT = 64
_OVERFLOW_BUCKET_COUNT = 4096
_MAX_CAPACITY_PRESSURE_COUNT = 2**31 - 1
_CAPACITY_PRESSURE_LOG_INTERVAL_SECONDS = 60

# Wrong first-run setup codes an IP may send in any 15-minute window before
# further wrong ones are answered 429.
SETUP_ATTEMPTS_PER_15MIN = 10

# Login admissions, keyed by device, account/address, account, and address.
_device_attempts: dict[tuple[str, str], deque[float]] = {}
_device_keys_by_account: dict[str, set[tuple[str, str]]] = {}
_account_address_attempts: dict[tuple[str, str], deque[float]] = {}
_account_attempts: dict[str, deque[float]] = {}
_ip_attempts: dict[str, deque[float]] = {}
_unresolved_attempts: deque[float] = deque()
_overflow_attempts: dict[tuple[str, int], deque[float]] = {}

# Shared address bucket for clients whose provenance could not be resolved.
# There is no information to distinguish them, so they share one bucket per
# account rather than one bucket per synthetic client address.
_UNRESOLVED_ADDRESS_BUCKET = "unresolved"


@dataclass(slots=True)
class _RefusalPressure:
    """Constant-size refusal accounting with an expiry fixed at first refusal."""

    started_at: float
    count: int = 0


# Refusal pressure is separate from every admission queue. Its count is capped
# at the configured IP limit, and repeated refusals never move started_at.
_ip_refusal_pressure: dict[str, _RefusalPressure] = {}
_ip_refusal_overflow: dict[int, _RefusalPressure] = {}
_setup_attempts: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_sweep_counter = 0
_setup_sweep_counter = 0
_last_capacity_sweep: dict[str, float] = {}
_capacity_pressure_counts = dict.fromkeys(
    ("device", "account_address", "account", "ip", "refusal"),
    0,
)
_last_capacity_pressure_log: dict[str, float] = {}
logger = logging.getLogger(__name__)

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


def _prune_bucket(
    buckets: dict[_Key, deque[float]], key: _Key, cutoff: float
) -> deque[float] | None:
    """Expire one bucket and remove its key when no admissions remain."""
    bucket = buckets.get(key)
    if bucket is None:
        return None
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if not bucket:
        del buckets[key]
        return None
    return bucket


def _overflow_slot(scope: str, identity: object) -> tuple[str, int]:
    """Return a fixed-cardinality slot using this process's randomized hash."""
    return scope, hash(identity) % _OVERFLOW_BUCKET_COUNT


def _prune_overflow_bucket(scope: str, identity: object, cutoff: float) -> deque[float] | None:
    return _prune_bucket(_overflow_attempts, _overflow_slot(scope, identity), cutoff)


def _record_overflow_attempt(scope: str, identity: object, now: float) -> None:
    _overflow_attempts.setdefault(_overflow_slot(scope, identity), deque()).append(now)


def _note_capacity_pressure(scope: str, now: float) -> None:
    """Increment a fixed-scope counter and rate-limit capacity warning logs."""
    if scope not in _capacity_pressure_counts:
        return
    _capacity_pressure_counts[scope] = min(
        _capacity_pressure_counts[scope] + 1,
        _MAX_CAPACITY_PRESSURE_COUNT,
    )
    last_log = _last_capacity_pressure_log.get(scope, float("-inf"))
    if now - last_log >= _CAPACITY_PRESSURE_LOG_INTERVAL_SECONDS:
        logger.warning(
            "login rate-limit bucket capacity reached; using bounded overflow window",
            extra={
                "capacity_scope": scope,
                "bucket_capacity": _MAX_ACTIVE_BUCKETS,
                "overflow_bucket_count": _OVERFLOW_BUCKET_COUNT,
                "capacity_pressure_count": _capacity_pressure_counts[scope],
            },
        )
        _last_capacity_pressure_log[scope] = now


def get_login_rate_limit_capacity_pressure_counts() -> dict[str, int]:
    """Return the bounded per-scope count of requests routed through overflow."""
    return dict(_capacity_pressure_counts)


def _prune_device_bucket(device_key: tuple[str, str], cutoff: float) -> deque[float] | None:
    bucket = _device_attempts.get(device_key)
    if bucket is None:
        return None
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if not bucket:
        del _device_attempts[device_key]
        account_devices = _device_keys_by_account.get(device_key[0])
        if account_devices is not None:
            account_devices.discard(device_key)
            if not account_devices:
                del _device_keys_by_account[device_key[0]]
        return None
    return bucket


def _prune_account_devices(account_key: str, cutoff: float) -> set[tuple[str, str]]:
    account_devices = _device_keys_by_account.get(account_key)
    if account_devices is None:
        return set()
    for device_key in tuple(account_devices):
        _prune_device_bucket(device_key, cutoff)
    return _device_keys_by_account.get(account_key, set())


def _sweep_device_attempts(cutoff: float) -> None:
    for device_key in tuple(_device_attempts):
        _prune_device_bucket(device_key, cutoff)


def _needs_overflow(
    buckets: dict[_Key, deque[float]],
    key: _Key,
    scope: str,
    identity: object,
    cutoff: float,
    *,
    now: float,
) -> bool:
    """Use stable overflow accounting for a new key that cannot use its map."""
    if key in buckets:
        return False
    overflow_bucket = _prune_overflow_bucket(scope, identity, cutoff)
    can_add = _can_add_bucket_key(
        buckets,
        key,
        cutoff,
        now=now,
        scope=scope,
    )
    use_overflow = not can_add or overflow_bucket is not None
    if use_overflow:
        _note_capacity_pressure(scope, now)
    return use_overflow


def _should_fail_open_full_map(
    buckets: dict[_Key, deque[float]],
    key: _Key,
    scope: str,
    cutoff: float,
    *,
    now: float,
) -> bool:
    """Return whether a new key should bypass accounting because the map is full."""
    if key in buckets or _can_add_bucket_key(buckets, key, cutoff, now=now, scope=scope):
        return False
    _note_capacity_pressure(scope, now)
    return True


def _can_add_bucket_key(
    buckets: dict[_Key, deque[float]],
    key: _Key,
    cutoff: float,
    *,
    now: float,
    scope: str,
) -> bool:
    """Check map capacity, sweeping expired keys at most once per interval.

    A live entry is never evicted. Callers route a new key to a fixed-size
    hashed overflow window when this returns false, keeping the attempt
    accounted without growing the identity map.
    """
    if key in buckets or len(buckets) < _MAX_ACTIVE_BUCKETS:
        return True
    if now - _last_capacity_sweep.get(scope, float("-inf")) >= _CAPACITY_SWEEP_INTERVAL_SECONDS:
        _sweep_inactive(buckets, cutoff)
        _last_capacity_sweep[scope] = now
    return key in buckets or len(buckets) < _MAX_ACTIVE_BUCKETS


def _sweep_expired_refusals(now: float) -> None:
    for ip_key, pressure in list(_ip_refusal_pressure.items()):
        if now - pressure.started_at >= _HOUR_SECONDS:
            del _ip_refusal_pressure[ip_key]
    for slot, pressure in list(_ip_refusal_overflow.items()):
        if now - pressure.started_at >= _HOUR_SECONDS:
            del _ip_refusal_overflow[slot]


def _active_refusal_count(ip_key: str, now: float) -> int:
    pressure = _ip_refusal_pressure.get(ip_key)
    if pressure is not None and now - pressure.started_at >= _HOUR_SECONDS:
        del _ip_refusal_pressure[ip_key]
        pressure = None
    if pressure is not None:
        return pressure.count

    overflow_slot = _overflow_slot("refusal", ip_key)[1]
    pressure = _ip_refusal_overflow.get(overflow_slot)
    if pressure is None:
        return 0
    if now - pressure.started_at >= _HOUR_SECONDS:
        del _ip_refusal_overflow[overflow_slot]
        return 0
    return pressure.count


def _record_ip_refusal_pressure(ip_key: str, now: float, per_ip: int) -> None:
    """Record bounded refusal pressure without turning map saturation global."""
    pressure = _ip_refusal_pressure.get(ip_key)
    overflow_slot = _overflow_slot("refusal", ip_key)[1]
    overflow_pressure = _ip_refusal_overflow.get(overflow_slot)
    if overflow_pressure is not None and now - overflow_pressure.started_at >= _HOUR_SECONDS:
        del _ip_refusal_overflow[overflow_slot]
        overflow_pressure = None
    if pressure is None:
        if overflow_pressure is not None:
            pressure = overflow_pressure
        else:
            if len(_ip_refusal_pressure) >= _MAX_ACTIVE_BUCKETS:
                last_sweep = _last_capacity_sweep.get("refusal", float("-inf"))
                if now - last_sweep >= _CAPACITY_SWEEP_INTERVAL_SECONDS:
                    _sweep_expired_refusals(now)
                    _last_capacity_sweep["refusal"] = now
            if len(_ip_refusal_pressure) < _MAX_ACTIVE_BUCKETS:
                pressure = _RefusalPressure(started_at=now)
                _ip_refusal_pressure[ip_key] = pressure
            else:
                _note_capacity_pressure("refusal", now)
                pressure = _ip_refusal_overflow.setdefault(
                    overflow_slot,
                    _RefusalPressure(started_at=now),
                )

    pressure.count = min(pressure.count + 1, per_ip)


async def check_and_record_login(
    email: str, provenance: str | ClientIpInfo | Request, *, device: str | None = None
) -> tuple[bool, str | None]:
    """Return whether a login may proceed, recording only admitted attempts.

    *email* is the normalized login identifier (email or login name).
    *provenance* is a resolved address, :class:`ClientIpInfo`, or request.
    A valid recognized *device* uses only its own 15-minute window. Otherwise
    the limiter checks account/address (15 minutes), account-wide (15
    minutes), and address-wide (one hour) limits.

    If provenance is unresolved, no address identity is inferred; the attempt
    is keyed on the shared ``"unresolved"`` address bucket for the
    account/address window, uses the account-wide limit, and also draws on a
    separate coarse hourly admission budget shared by every unresolved
    client. A result reason is ``"device"``, ``"email"`` (account/address),
    ``"account"``, ``"ip"``, or ``"unresolved"``. Only resolved-address
    refusals caused by the account/address window add to fixed-expiry IP
    pressure. Refusals do not change admitted-attempt counts. Resolved
    account/address and account-wide refusals add bounded, fixed-expiry pressure
    to that source IP. The check, pressure update, and admissions are atomic
    under one lock.
    """
    global _sweep_counter

    now = _now()
    account_cutoff = now - _FIFTEEN_MIN_SECONDS
    ip_cutoff = now - _HOUR_SECONDS
    per_account_address = settings.login_rate_limit_per_15min
    per_account = settings.login_account_rate_limit_per_15min
    per_ip = settings.login_rate_limit_per_hour_per_ip
    account_key = email.strip().lower()
    if isinstance(provenance, ClientIpInfo):
        ip = provenance.client_ip
        resolved = provenance.resolved
    elif isinstance(provenance, str):
        ip = provenance
        # Plain-address callers are resolved by contract. Callers that need to
        # distinguish an unresolved client must pass ClientIpInfo.
        resolved = True
    else:
        info = get_client_ip_info(provenance)
        ip = info.client_ip
        resolved = info.resolved
    ip_key = normalize_ip_bucket(ip) if resolved else _UNRESOLVED_ADDRESS_BUCKET

    async with _lock:
        _sweep_counter += 1
        if _sweep_counter >= _SWEEP_EVERY:
            _sweep_counter = 0
            _sweep_device_attempts(account_cutoff)
            _sweep_inactive(_account_address_attempts, account_cutoff)
            _sweep_inactive(_account_attempts, account_cutoff)
            _sweep_inactive(_ip_attempts, ip_cutoff)
            for overflow_key in tuple(_overflow_attempts):
                scope, _ = overflow_key
                cutoff = ip_cutoff if scope == "ip" else account_cutoff
                _prune_bucket(_overflow_attempts, overflow_key, cutoff)
            while _unresolved_attempts and _unresolved_attempts[0] < ip_cutoff:
                _unresolved_attempts.popleft()
            _sweep_expired_refusals(now)
            _last_capacity_sweep.update(
                device=now,
                account_address=now,
                account=now,
                ip=now,
                refusal=now,
            )

        if device is not None:
            # Existing recognized devices retain their independent escape
            # hatch. New device IDs are bounded per account; excess IDs share
            # an account-scoped overflow window rather than gaining a fresh
            # independent allowance or evicting a live device.
            device_key = (account_key, device)
            device_bucket = _prune_device_bucket(device_key, account_cutoff)
            if device_bucket is not None and len(device_bucket) >= per_account_address:
                return False, "device"
            if device_bucket is not None:
                device_bucket.append(now)
                return True, None

            account_devices = _prune_account_devices(account_key, account_cutoff)
            device_overflow = _prune_overflow_bucket("device", account_key, account_cutoff)
            use_overflow = (
                len(account_devices) >= _MAX_DEVICES_PER_ACCOUNT
                or device_overflow is not None
                or not _can_add_bucket_key(
                    _device_attempts,
                    device_key,
                    account_cutoff,
                    now=now,
                    scope="device",
                )
            )
            if use_overflow:
                _note_capacity_pressure("device", now)
                if device_overflow is not None and len(device_overflow) >= per_account_address:
                    return False, "device"
                _record_overflow_attempt("device", account_key, now)
                return True, None

            _device_attempts[device_key] = deque([now])
            _device_keys_by_account.setdefault(account_key, set()).add(device_key)
            return True, None

        address_key = (account_key, ip_key)
        address_bucket = _prune_bucket(_account_address_attempts, address_key, account_cutoff)
        account_bucket = _prune_bucket(_account_attempts, account_key, account_cutoff)
        resolved_ip_bucket = _prune_bucket(_ip_attempts, ip_key, ip_cutoff) if resolved else None
        address_count = len(address_bucket) if address_bucket is not None else 0
        account_count = len(account_bucket) if account_bucket is not None else 0
        ip_count = len(resolved_ip_bucket) if resolved_ip_bucket is not None else 0
        refusal_count = _active_refusal_count(ip_key, now) if resolved else 0

        # Abuse escalation combines admitted IP traffic and bounded address
        # refusal pressure. Requests already refused by this policy add no
        # more pressure and cannot extend either window. Unresolved provenance
        # has no per-client address, so it is not subject to the per-IP window.
        if resolved and ip_count + refusal_count >= per_ip:
            return False, "ip"

        if address_count >= per_account_address:
            if resolved:
                _record_ip_refusal_pressure(ip_key, now, per_ip)
            return False, "email"

        if not resolved:
            while _unresolved_attempts and _unresolved_attempts[0] < ip_cutoff:
                _unresolved_attempts.popleft()
            if len(_unresolved_attempts) >= settings.unresolved_login_rate_limit_per_hour:
                return False, "unresolved"

        # Account-wide checks apply after the address-specific limit. Count
        # resolved refusals against the source IP's bounded, fixed-expiry
        # pressure so polling one locked account cannot bypass the IP policy.
        if account_count >= per_account:
            if resolved:
                _record_ip_refusal_pressure(ip_key, now, per_ip)
            return False, "account"

        # Check every map before writing to any of them, so a refusal in one
        # window cannot leave a partial admission in another. New keys that
        # cannot use a full map are still counted by its fixed-size overflow
        # window; hash collisions can conservatively share a limit.
        # The pair scope is the only per-user gate available to a legitimate
        # first-time client. Overflow hashing it would let distributed attackers
        # fill shared slots and lock out unrelated new sign-ins, so saturation
        # fails open here; the account-wide and IP windows below still apply.
        bypass_address = _should_fail_open_full_map(
            _account_address_attempts,
            address_key,
            "account_address",
            account_cutoff,
            now=now,
        )
        spill_account = _needs_overflow(
            _account_attempts,
            account_key,
            "account",
            account_key,
            account_cutoff,
            now=now,
        )
        spill_ip = (
            _needs_overflow(
                _ip_attempts,
                ip_key,
                "ip",
                ip_key,
                ip_cutoff,
                now=now,
            )
            if resolved
            else False
        )

        overflow_account = (
            _prune_overflow_bucket("account", account_key, account_cutoff)
            if spill_account
            else None
        )
        overflow_ip = _prune_overflow_bucket("ip", ip_key, ip_cutoff) if spill_ip else None
        if overflow_account is not None and len(overflow_account) >= per_account:
            return False, "account"
        if overflow_ip is not None and len(overflow_ip) >= per_ip:
            return False, "ip"

        if not bypass_address:
            _account_address_attempts.setdefault(address_key, deque()).append(now)
        if spill_account:
            _record_overflow_attempt("account", account_key, now)
        else:
            _account_attempts.setdefault(account_key, deque()).append(now)
        if resolved:
            if spill_ip:
                _record_overflow_attempt("ip", ip_key, now)
            else:
                _ip_attempts.setdefault(ip_key, deque()).append(now)
        else:
            _unresolved_attempts.append(now)

    return True, None


async def record_failed_setup_attempt(ip: str) -> bool:
    """Record a wrong first-run setup code and report whether the IP is under limit.

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
    _device_keys_by_account.clear()
    _account_address_attempts.clear()
    _account_attempts.clear()
    _ip_attempts.clear()
    _unresolved_attempts.clear()
    _overflow_attempts.clear()
    _ip_refusal_pressure.clear()
    _ip_refusal_overflow.clear()
    _setup_attempts.clear()
    _last_capacity_sweep.clear()
    for scope in _capacity_pressure_counts:
        _capacity_pressure_counts[scope] = 0
    _last_capacity_pressure_log.clear()
    _sweep_counter = 0
    _setup_sweep_counter = 0
