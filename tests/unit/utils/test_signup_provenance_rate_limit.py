"""Checks for signup rate limiting with unresolved client provenance."""

from __future__ import annotations

import asyncio

import pytest

from serving.config.settings import settings
from serving.utils import signup_rate_limit as limiter
from serving.utils.request_ip import ClientIpInfo


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(settings, "signup_rate_limit_per_hour", 2)
    monkeypatch.setattr(settings, "signup_rate_limit_per_day", 10)
    monkeypatch.setattr(settings, "signup_require_resolved_client_ip", False)
    monkeypatch.setattr(settings, "unresolved_signup_rate_limit_per_hour", 2)
    monkeypatch.setattr(settings, "unresolved_signup_rate_limit_per_day", 3)
    limiter.reset_signup_rate_limit_state()
    yield
    limiter.reset_signup_rate_limit_state()


def _unresolved_provenance() -> ClientIpInfo:
    return ClientIpInfo(
        client_ip="unknown",
        peer_ip="10.0.0.2",
        source="unknown",
        trusted_proxy_headers=False,
        resolved=False,
    )


@pytest.mark.asyncio
async def test_unresolved_signup_refusals_do_not_grow_budget_and_expire(monkeypatch):
    now = [5_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    unknown = _unresolved_provenance()

    assert await limiter.check_and_record_signup(unknown) == (True, None)
    assert await limiter.check_and_record_signup(unknown) == (True, None)
    for _ in range(500):
        assert await limiter.check_and_record_signup(unknown) == (False, "unresolved_hour")

    assert len(limiter._unresolved_attempts) == 2
    assert not limiter._attempts

    now[0] += 3_601
    assert await limiter.check_and_record_signup(unknown) == (True, None)
    assert len(limiter._unresolved_attempts) == 3

    now[0] += 86_401
    assert await limiter.check_and_record_signup(unknown) == (True, None)
    assert len(limiter._unresolved_attempts) == 1


@pytest.mark.asyncio
async def test_resolved_signup_refusals_do_not_grow_ip_bucket_or_block_other_ip(
    monkeypatch,
):
    now = [6_000.0]
    monkeypatch.setattr(limiter, "_now", lambda: now[0])
    ip = "198.51.100.4"

    assert await limiter.check_and_record_signup(ip) == (True, None)
    assert await limiter.check_and_record_signup(ip) == (True, None)
    for _ in range(50):
        assert await limiter.check_and_record_signup(ip) == (False, "hour")

    assert len(limiter._attempts[f"client:{ip}"]) == 2
    assert await limiter.check_and_record_signup("198.51.100.5") == (True, None)

    now[0] += 3_601
    assert await limiter.check_and_record_signup(ip) == (True, None)


@pytest.mark.asyncio
async def test_configured_signup_policy_fails_closed_without_recording(monkeypatch):
    monkeypatch.setattr(settings, "signup_require_resolved_client_ip", True)
    unknown = _unresolved_provenance()

    for _ in range(20):
        assert await limiter.check_and_record_signup(unknown) == (False, "unresolved")
    assert not limiter._unresolved_attempts


@pytest.mark.asyncio
async def test_concurrent_unresolved_signup_admission_is_atomic():
    attempts = await asyncio.gather(
        *(limiter.check_and_record_signup(_unresolved_provenance()) for _ in range(30))
    )

    assert sum(allowed for allowed, _ in attempts) == 2
    assert sum(reason == "unresolved_hour" for _, reason in attempts) == 28
    assert len(limiter._unresolved_attempts) == 2
