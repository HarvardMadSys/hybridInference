"""IPv6 clients must not escape per-IP limits by rotating within their /64."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import serving.utils.login_rate_limit as login_rate_limit
import serving.utils.signup_rate_limit as signup_rate_limit
from serving.config.settings import Settings
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
_SAME_PREFIX = [f"2001:4860:4860:1234::{n:x}" for n in range(1, 64)]
_OTHER_PREFIX = "2001:4860:4860:9999::1"


def _request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    reset_signup_rate_limit_state()
    reset_login_rate_limit_state()
    yield
    reset_signup_rate_limit_state()
    reset_login_rate_limit_state()


@pytest.fixture
def _live_settings():
    """Use real Settings instance."""
    return Settings()


@pytest.mark.asyncio
async def test_signup_limit_survives_ipv6_rotation(_live_settings):
    """Rotating the low 64 bits shares one signup bucket.

    Every attempt lands inside the same hour, so the hourly window trips
    first even though the daily cap is higher.
    """
    per_hour = _live_settings.signup_rate_limit_per_hour
    addresses = _SAME_PREFIX[: per_hour + 1]
    assert len(addresses) == per_hour + 1

    from unittest.mock import patch

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        results = [await check_and_record_signup(_request(ip)) for ip in addresses]

    assert all(allowed for allowed, _ in results[:per_hour])
    assert results[-1] == (False, "hour")


@pytest.mark.asyncio
async def test_signup_limit_isolates_distinct_ipv6_prefixes(_live_settings):
    """A different /64 is a separate bucket — no collateral throttling."""
    from unittest.mock import patch

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        # Fill the hourly window from the first prefix.
        for _ in range(_live_settings.signup_rate_limit_per_hour + 1):
            await check_and_record_signup(_request(_SAME_PREFIX[0]))
        assert (await check_and_record_signup(_request(_SAME_PREFIX[0])))[0] is False

        # A client from a separate /64 is unaffected.
        assert (await check_and_record_signup(_request(_OTHER_PREFIX)))[0] is True


@pytest.mark.asyncio
async def test_login_per_ip_limit_survives_ipv6_rotation(_live_settings):
    """Per-IP login limit folds to /64 so rotation cannot dodge it."""
    per_ip = _live_settings.login_rate_limit_per_hour_per_ip

    from unittest.mock import patch

    with patch("serving.utils.login_rate_limit.settings", _live_settings):
        # Use different emails so we're testing the per-IP limit, not per-email
        for i, ip in enumerate(_SAME_PREFIX[: per_ip + 1]):
            email = f"user{i}@example.com"
            result = await check_and_record_login(email, _request(ip))
            if not result[0]:
                assert result == (False, "ip")
                break
        else:
            pytest.fail("per_ip limit did not trip across a full /64")


@pytest.mark.asyncio
async def test_login_ipv4_buckets_remain_per_address(_live_settings):
    """IPv4 clients keep per-address buckets — /64 folding does not apply."""
    from unittest.mock import patch

    with patch("serving.utils.login_rate_limit.settings", _live_settings):
        email = "stable@example.com"

        first = await check_and_record_login(email, _request("8.8.8.20"))
        second = await check_and_record_login(email, _request("8.8.8.21"))
        # Each address is its own bucket, so both succeed independently.
        assert first[0] is True
        assert second[0] is True


@pytest.mark.asyncio
async def test_signup_unresolved_skips_limit(_live_settings):
    """Unresolved signup uses a coarse budget without using the proxy IP."""
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.19.0.1"))

    from unittest.mock import patch

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        _live_settings.unresolved_signup_rate_limit_per_hour = 3
        for _ in range(3):
            assert await check_and_record_signup(request) == (True, None)
        assert await check_and_record_signup(request) == (False, "unresolved_hour")


@pytest.mark.asyncio
async def test_signup_unresolved_can_fail_closed(_live_settings):
    """Deployments can reject signup when provenance is unresolved."""
    _live_settings.signup_require_resolved_client_ip = True
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.19.0.1"))

    from unittest.mock import patch

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        assert await check_and_record_signup(request) == (False, "unresolved")


@pytest.mark.asyncio
async def test_login_unresolved_skips_ip_limit(_live_settings):
    """Unresolved login uses a coarse budget, never a proxy-IP bucket."""
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.19.0.1"))

    from unittest.mock import patch

    with patch("serving.utils.login_rate_limit.settings", _live_settings):
        _live_settings.unresolved_login_rate_limit_per_hour = 3
        for i in range(3):
            email = f"user{i}@example.com"
            allowed, reason = await check_and_record_login(email, request)
            assert allowed is True
            assert reason is None
        assert await check_and_record_login("user3@example.com", request) == (False, "unresolved")


@pytest.mark.asyncio
async def test_signup_rejected_attempts_do_not_grow_or_delay_recovery(_live_settings, monkeypatch):
    """Rejected attempts remain outside both the queue and its expiry window."""
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.19.0.1"))
    _live_settings.unresolved_signup_rate_limit_per_hour = 2
    _live_settings.unresolved_signup_rate_limit_per_day = 10
    now = 1_000.0

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        monkeypatch.setattr(signup_rate_limit, "_now", lambda: now)
        assert await check_and_record_signup(request) == (True, None)
        assert await check_and_record_signup(request) == (True, None)
        for _ in range(500):
            assert await check_and_record_signup(request) == (False, "unresolved_hour")
        assert len(signup_rate_limit._unresolved_attempts) == 2

        now += 3_601
        assert await check_and_record_signup(request) == (True, None)


@pytest.mark.asyncio
async def test_signup_per_ip_rejections_do_not_grow_bucket(_live_settings, monkeypatch):
    """A saturated resolved-client bucket stores only permitted attempts."""
    request = _request("8.8.8.20")
    _live_settings.signup_rate_limit_per_hour = 2
    _live_settings.signup_rate_limit_per_day = 10
    now = 2_000.0

    with patch("serving.utils.signup_rate_limit.settings", _live_settings):
        monkeypatch.setattr(signup_rate_limit, "_now", lambda: now)
        assert await check_and_record_signup(request) == (True, None)
        assert await check_and_record_signup(request) == (True, None)
        for _ in range(500):
            assert await check_and_record_signup(request) == (False, "hour")
        assert len(signup_rate_limit._attempts["client:8.8.8.20"]) == 2


@pytest.mark.asyncio
async def test_login_rejected_attempts_do_not_grow_any_bucket(_live_settings, monkeypatch):
    """Email, IP, and unresolved queues retain only admitted attempts."""
    _live_settings.login_rate_limit_per_15min = 2
    _live_settings.login_rate_limit_per_hour_per_ip = 2
    _live_settings.unresolved_login_rate_limit_per_hour = 2
    now = 3_000.0

    with patch("serving.utils.login_rate_limit.settings", _live_settings):
        monkeypatch.setattr(login_rate_limit, "_now", lambda: now)
        request = _request("8.8.8.20")
        assert await check_and_record_login("user@example.com", request) == (True, None)
        assert await check_and_record_login("user@example.com", request) == (True, None)
        for _ in range(500):
            assert await check_and_record_login("user@example.com", request) == (False, "ip")
        assert len(login_rate_limit._email_attempts["user@example.com"]) == 2
        assert len(login_rate_limit._ip_attempts["client:8.8.8.20"]) == 2

        unresolved_request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.19.0.1"))
        _live_settings.login_rate_limit_per_15min = 10
        for index in range(2):
            assert await check_and_record_login(
                f"unresolved-{index}@example.com", unresolved_request
            ) == (True, None)
        for index in range(500):
            assert await check_and_record_login(
                f"rejected-{index}@example.com", unresolved_request
            ) == (False, "unresolved")
        assert len(login_rate_limit._unresolved_attempts) == 2
        assert len(login_rate_limit._email_attempts) == 3
