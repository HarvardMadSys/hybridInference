"""Wrong first-run setup codes: ten per client IP in any 15 minutes."""

from __future__ import annotations

import pytest

from serving.utils import login_rate_limit
from serving.utils.login_rate_limit import (
    SETUP_ATTEMPTS_PER_15MIN,
    record_failed_setup_attempt,
    reset_login_rate_limit_state,
)


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    reset_login_rate_limit_state()
    yield
    reset_login_rate_limit_state()


async def test_allows_ten_then_refuses():
    results = [await record_failed_setup_attempt("203.0.113.7") for _ in range(11)]
    assert SETUP_ATTEMPTS_PER_15MIN == 10
    assert results == [True] * 10 + [False]


async def test_buckets_are_per_ip_and_ipv6_prefix():
    for _ in range(SETUP_ATTEMPTS_PER_15MIN):
        await record_failed_setup_attempt("2001:db8:1:2::1")

    assert await record_failed_setup_attempt("2001:db8:1:2::ffff") is False
    assert await record_failed_setup_attempt("2001:db8:1:3::1") is True
    assert await record_failed_setup_attempt("198.51.100.1") is True


async def test_window_slides(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(login_rate_limit, "_now", lambda: now[0])
    for _ in range(SETUP_ATTEMPTS_PER_15MIN):
        await record_failed_setup_attempt("203.0.113.7")
    assert await record_failed_setup_attempt("203.0.113.7") is False

    now[0] += 15 * 60 + 1

    assert await record_failed_setup_attempt("203.0.113.7") is True


async def test_independent_of_login_attempts():
    for _ in range(SETUP_ATTEMPTS_PER_15MIN):
        await record_failed_setup_attempt("203.0.113.7")

    allowed, reason = await login_rate_limit.check_and_record_login("admin", "203.0.113.7")

    assert (allowed, reason) == (True, None)


async def test_reset_clears_setup_attempts():
    for _ in range(SETUP_ATTEMPTS_PER_15MIN + 1):
        await record_failed_setup_attempt("203.0.113.7")

    reset_login_rate_limit_state()

    assert await record_failed_setup_attempt("203.0.113.7") is True
