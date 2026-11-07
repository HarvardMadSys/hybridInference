"""Windows for the anonymous routes that send mail: per address asked about, per client."""

from __future__ import annotations

import pytest

from serving.utils import mail_rate_limit
from serving.utils.mail_rate_limit import (
    MAIL_REQUESTS_PER_ADDRESS_PER_HOUR,
    MAIL_REQUESTS_PER_IP_PER_HOUR,
    check_and_record_mail_request,
    reset_mail_rate_limit_state,
)

ATTACKER = "198.51.100.7"
OWNER = "192.0.2.44"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_mail_rate_limit_state()
    yield
    reset_mail_rate_limit_state()


def test_shipped_limits():
    assert (MAIL_REQUESTS_PER_ADDRESS_PER_HOUR, MAIL_REQUESTS_PER_IP_PER_HOUR) == (3, 10)


async def test_one_address_receives_at_most_three_of_a_kind_an_hour():
    for n in range(3):
        sender = f"203.0.113.{n + 1}"
        assert await check_and_record_mail_request(
            "password_reset", "victim@example.com", sender
        ) == (True, None)

    # Case and surrounding space name the same inbox; any client is refused.
    assert await check_and_record_mail_request("password_reset", " Victim@Example.COM", OWNER) == (
        False,
        "address",
    )
    # The other kind of mail, and other addresses, keep their own windows.
    assert await check_and_record_mail_request("verification", "victim@example.com", OWNER) == (
        True,
        None,
    )
    assert await check_and_record_mail_request("password_reset", "other@example.com", OWNER) == (
        True,
        None,
    )


async def test_one_client_sends_at_most_ten_an_hour_over_both_kinds():
    for n in range(10):
        kind = "password_reset" if n % 2 else "verification"
        assert await check_and_record_mail_request(kind, f"user{n}@example.com", ATTACKER) == (
            True,
            None,
        )

    assert await check_and_record_mail_request("password_reset", "fresh@example.com", ATTACKER) == (
        False,
        "ip",
    )
    assert await check_and_record_mail_request("password_reset", "fresh@example.com", OWNER) == (
        True,
        None,
    )


async def test_unresolved_clients_do_not_share_one_window():
    """The reported case: behind an unresolving proxy, ten requests stopped every reset.

    Every such client used to count as ``"unknown"``, one window for the whole
    deployment. Without an address to tell them apart they are limited per
    address asked about alone.
    """
    for n in range(MAIL_REQUESTS_PER_IP_PER_HOUR * 3):
        assert await check_and_record_mail_request(
            "password_reset", f"junk{n}@example.com", None
        ) == (True, None)

    assert await check_and_record_mail_request("password_reset", "owner@example.com", None) == (
        True,
        None,
    )
    assert mail_rate_limit._ip_requests == {}
    # Each inbox still receives at most its three an hour.
    for _ in range(2):
        await check_and_record_mail_request("password_reset", "owner@example.com", None)
    assert await check_and_record_mail_request("password_reset", "owner@example.com", None) == (
        False,
        "address",
    )


async def test_an_ipv6_client_is_counted_by_its_slash_64():
    for n in range(10):
        assert await check_and_record_mail_request(
            "password_reset", f"user{n}@example.com", f"2001:db8:1:2::{n + 1}"
        ) == (True, None)

    assert await check_and_record_mail_request(
        "password_reset", "fresh@example.com", "2001:db8:1:2:ffff::1"
    ) == (False, "ip")


async def test_refused_requests_are_not_recorded():
    """Hammering never extends a refusal, so the owner's inbox keeps getting links."""
    for _ in range(3):
        await check_and_record_mail_request("password_reset", "victim@example.com", ATTACKER)
    for _ in range(100):
        assert await check_and_record_mail_request(
            "password_reset", "victim@example.com", ATTACKER
        ) == (False, "address")

    assert len(mail_rate_limit._address_requests[("password_reset", "victim@example.com")]) == 3
    assert len(mail_rate_limit._ip_requests[ATTACKER]) == 3


async def test_windows_slide(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(mail_rate_limit, "_now", lambda: now[0])
    for _ in range(3):
        await check_and_record_mail_request("verification", "user@example.com", OWNER)
    for n in range(10):
        await check_and_record_mail_request("verification", f"user{n}@example.com", ATTACKER)
    assert await check_and_record_mail_request("verification", "x@example.com", ATTACKER) == (
        False,
        "ip",
    )
    assert await check_and_record_mail_request("verification", "user@example.com", OWNER) == (
        False,
        "address",
    )

    now[0] += 3600 + 1

    assert await check_and_record_mail_request("verification", "x@example.com", ATTACKER) == (
        True,
        None,
    )
    assert await check_and_record_mail_request("verification", "user@example.com", OWNER) == (
        True,
        None,
    )


async def test_sweep_drops_expired_windows(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(mail_rate_limit, "_now", lambda: now[0])
    monkeypatch.setattr(mail_rate_limit, "_SWEEP_EVERY", 2)
    await check_and_record_mail_request("password_reset", "old@example.com", ATTACKER)

    now[0] += 3600 + 1
    await check_and_record_mail_request("password_reset", "new@example.com", OWNER)

    assert set(mail_rate_limit._address_requests) == {("password_reset", "new@example.com")}
    assert set(mail_rate_limit._ip_requests) == {OWNER}
