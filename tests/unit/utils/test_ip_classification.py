"""IPv4 carried in IPv6 is judged by the IPv4 address a connection reaches.

``ipaddress`` judges an IPv6 address alone: ``::169.254.169.254`` and
``64:ff9b::10.0.0.5`` are "global" and have no ``ipv4_mapped``. These pin the
structural unwrapping every connection guard shares.
"""

from __future__ import annotations

import ipaddress

import pytest

from serving.utils.ip_classification import (
    embedded_ipv4,
    is_public_unicast,
    parse_ip,
    reachable_addresses,
)


@pytest.mark.parametrize(
    ("address", "embedded"),
    [
        ("::ffff:169.254.169.254", ["169.254.169.254"]),  # IPv4-mapped
        ("::169.254.169.254", ["169.254.169.254"]),  # IPv4-compatible
        ("::ffff:0:10.0.0.5", ["10.0.0.5"]),  # IPv4-translated
        ("64:ff9b::10.0.0.5", ["10.0.0.5"]),  # NAT64 well-known
        ("2002:a9fe:a9fe::1", ["169.254.169.254"]),  # 6to4
        ("8.8.8.8", []),
        ("2606:4700::6810:84e5", []),
        ("fd00::1", []),
    ],
)
def test_the_embedded_ipv4_address_is_unwrapped(address, embedded):
    assert [str(ip) for ip in embedded_ipv4(address)] == embedded


def test_local_use_nat64_considers_every_prefix_length():
    """RFC 8215 lets the operator choose the prefix length, so each RFC 6052 layout counts."""
    as_96 = "64:ff9b:1::a9fe:a9fe"
    as_64 = "64:ff9b:1:0:a9:fea9:fe00:0"
    as_56 = "64:ff9b:1:a9:fe:a9fe::"
    as_48 = "64:ff9b:1:a9fe:a9:fe00::"
    for address in (as_96, as_64, as_56, as_48):
        assert ipaddress.ip_address("169.254.169.254") in embedded_ipv4(address), address


def test_teredo_yields_its_server_and_client():
    teredo = "2001:0:4136:e378:8000:63bf:3fff:fdd2"
    assert {str(ip) for ip in embedded_ipv4(teredo)} == {"65.54.227.120", "192.0.2.45"}


def test_a_zone_is_dropped():
    assert parse_ip("fe80::1%eth0") == ipaddress.ip_address("fe80::1")
    assert parse_ip(ipaddress.IPv6Address("fe80::1%eth0")) == ipaddress.ip_address("fe80::1")
    assert reachable_addresses("::169.254.169.254%3")[-1] == ipaddress.ip_address("169.254.169.254")


def test_a_non_address_raises():
    with pytest.raises(ValueError):
        parse_ip("example.com")


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "93.184.216.34",
        "2606:4700::6810:84e5",
        "2a00:1450:4001:82a::200e",
        "64:ff9b::808:808",  # NAT64 to a public address: reachable, and public
        "2002:808:808::1",  # 6to4 of a public address
        "::ffff:8.8.8.8",
    ],
)
def test_a_public_address_is_public(address):
    assert is_public_unicast(address)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "::",
        "fd00::1",
        "fe80::1%eth0",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::127.0.0.1",
        "::ffff:0:10.0.0.5",
        "64:ff9b::10.0.0.5",
        "64:ff9b::169.254.169.254",
        "64:ff9b:1::808:808",  # local-use NAT64 is a private translator's
        "2002:a00:5::1",  # 6to4 of 10.0.0.5
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "not-an-address",
    ],
)
def test_a_private_loopback_or_embedded_private_address_is_not_public(address):
    assert not is_public_unicast(address)
