"""IPv6 clients must not escape per-IP limits by rotating within their /64."""

from __future__ import annotations

import pytest

from serving.config.settings import settings
from serving.utils.login_rate_limit import (
    check_and_record_login,
    reset_login_rate_limit_state,
)
from serving.utils.signup_rate_limit import (
    check_and_record_signup,
    reset_signup_rate_limit_state,
)

# Distinct /128 addresses inside one delegated /64 — what an attacker rotates
# through, and what RFC 4941 privacy addressing produces on its own.
_SAME_PREFIX = [f"2001:db8:abcd:1234::{n:x}" for n in range(1, 64)]
_OTHER_PREFIX = "2001:db8:abcd:9999::1"


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    reset_signup_rate_limit_state()
    reset_login_rate_limit_state()
    yield
    reset_signup_rate_limit_state()
    reset_login_rate_limit_state()


@pytest.mark.asyncio
async def test_signup_limit_survives_ipv6_rotation():
    """Rotating the low 64 bits shares one signup bucket.

    Every attempt lands inside the same hour, so the hourly window trips
    first even though the daily cap is higher.
    """
    per_hour = settings.signup_rate_limit_per_hour
    addresses = _SAME_PREFIX[: per_hour + 1]
    assert len(addresses) == per_hour + 1

    results = [await check_and_record_signup(ip) for ip in addresses]

    assert all(allowed for allowed, _ in results[:per_hour])
    assert results[-1] == (False, "hour")


@pytest.mark.asyncio
async def test_signup_limit_isolates_distinct_ipv6_prefixes():
    """A separate /64 is a separate bucket, so real users are not collateral."""
    for ip in _SAME_PREFIX[: settings.signup_rate_limit_per_hour + 1]:
        await check_and_record_signup(ip)

    allowed, reason = await check_and_record_signup(_OTHER_PREFIX)

    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_login_per_ip_limit_survives_ipv6_rotation():
    """Credential stuffing from one /64 trips the per-IP window."""
    per_ip = settings.login_rate_limit_per_hour_per_ip
    addresses = _SAME_PREFIX[: per_ip + 1]
    assert len(addresses) == per_ip + 1

    # A distinct email per attempt so only the per-IP bucket can trip.
    results = [
        await check_and_record_login(f"user{i}@example.com", ip) for i, ip in enumerate(addresses)
    ]

    assert all(allowed for allowed, _ in results[:per_ip])
    assert results[-1] == (False, "ip")


@pytest.mark.asyncio
async def test_login_ipv4_buckets_remain_per_address():
    """IPv4 keeps full-address buckets — /64 folding is IPv6-only."""
    per_ip = settings.login_rate_limit_per_hour_per_ip

    for i in range(per_ip):
        allowed, _ = await check_and_record_login(f"user{i}@example.com", "203.0.113.9")
        assert allowed is True

    blocked, reason = await check_and_record_login("last@example.com", "203.0.113.9")
    assert (blocked, reason) == (False, "ip")

    # A different IPv4 address is untouched.
    allowed, reason = await check_and_record_login("other@example.com", "203.0.113.10")
    assert (allowed, reason) == (True, None)
