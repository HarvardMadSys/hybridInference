"""The key client addresses are compared by (``serving.utils.client_addresses``)."""

from __future__ import annotations

import pytest

from serving.utils.client_addresses import address_key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("198.51.100.7", "198.51.100.7"),
        (" 198.51.100.7 ", "198.51.100.7"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("198.51.100.7, 10.0.0.1", "198.51.100.7"),
        ("2a00:1450:4001:81c::200e", "2a00:1450:4001:81c::/64"),
        ("2a00:1450:4001:81c:aaaa:bbbb:cccc:dddd", "2a00:1450:4001:81c::/64"),
        # Recorded only for an authorized direct client network, so it counts.
        ("10.20.30.40", "10.20.30.40"),
        ("unknown", None),
        ("", None),
        (None, None),
        (42, None),
        ("127.0.0.1", None),
        ("::1", None),
        ("169.254.10.1", None),
        ("fe80::1%eth0", None),
        ("0.0.0.0", None),
        ("224.0.0.1", None),
        ("240.0.0.1", None),
    ],
)
def test_address_key(raw: object, key: str | None) -> None:
    assert address_key(raw) == key


def test_a_hosts_rotating_ipv6_addresses_share_one_key() -> None:
    assert address_key("2a00:1450:4001:81c::1") == address_key("2a00:1450:4001:81c:ffff::2")
    assert address_key("2a00:1450:4001:81c::1") != address_key("2a00:1450:4001:81d::1")
