"""Tests for proxied client IP extraction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.utils.request_ip import (
    get_client_ip,
    get_client_ip_bucket,
    get_client_ip_info,
    normalize_ip_bucket,
)


def _request(headers: dict[str, str], peer_ip: str = "10.0.0.2"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer_ip))


def test_client_ip_ignores_forwarded_headers_by_default(monkeypatch):
    """Ignore spoofable forwarding headers unless proxy trust is explicitly enabled."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    request = _request({"x-forwarded-for": "203.0.113.9"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert get_client_ip(request) == "172.19.0.8"
    assert info.client_ip == "172.19.0.8"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "socket"
    assert info.x_forwarded_for == "203.0.113.9"


def test_client_ip_uses_first_forwarded_for_when_trusted(monkeypatch):
    """Use the leftmost X-Forwarded-For address from trusted proxies."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

    request = _request(
        {"x-forwarded-for": "203.0.113.9, 198.51.100.4", "x-real-ip": "198.51.100.7"},
        peer_ip="172.19.0.8",
    )

    info = get_client_ip_info(request)
    assert get_client_ip(request) == "203.0.113.9"
    assert info.client_ip == "203.0.113.9"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "x-forwarded-for"
    assert info.x_forwarded_for == "203.0.113.9, 198.51.100.4"
    assert info.x_real_ip == "198.51.100.7"


def test_client_ip_falls_back_to_x_real_ip_when_trusted(monkeypatch):
    """Use X-Real-IP when trusted and X-Forwarded-For is absent."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

    request = _request({"x-real-ip": "198.51.100.7"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert info.client_ip == "198.51.100.7"
    assert info.peer_ip == "172.19.0.8"
    assert info.source == "x-real-ip"


def test_cf_connecting_ip_wins_over_forwarded_for(monkeypatch):
    """Cloudflare overwrites CF-Connecting-IP but only appends to X-Forwarded-For.

    A client that supplies its own X-Forwarded-For leaves the leftmost entry
    attacker-controlled, so the Cloudflare header must take precedence.
    """
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    request = _request(
        {
            "x-forwarded-for": "1.2.3.4, 203.0.113.9",
            "x-real-ip": "198.51.100.7",
            "cf-connecting-ip": "203.0.113.9",
        },
        peer_ip="127.0.0.1",
    )

    info = get_client_ip_info(request)
    assert get_client_ip(request) == "203.0.113.9"
    assert info.client_ip == "203.0.113.9"
    assert info.source == "cf-connecting-ip"
    assert info.cf_connecting_ip == "203.0.113.9"
    assert info.x_forwarded_for == "1.2.3.4, 203.0.113.9"


def test_cf_connecting_ip_ignored_when_untrusted(monkeypatch):
    """The Cloudflare header is as spoofable as the others without proxy trust."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("TRUST_CLOUDFLARE_HEADERS", raising=False)

    request = _request({"cf-connecting-ip": "203.0.113.9"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert info.client_ip == "172.19.0.8"
    assert info.source == "socket"
    # Provenance is still recorded so the log shows what the client claimed.
    assert info.cf_connecting_ip == "203.0.113.9"


def test_cf_connecting_ip_ignored_behind_non_cloudflare_proxy(monkeypatch):
    """Generic proxy trust must not imply the Cloudflare header is trustworthy.

    A non-Cloudflare proxy may rewrite X-Forwarded-For correctly while passing
    a client-supplied CF-Connecting-IP straight through.
    """
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.delenv("TRUST_CLOUDFLARE_HEADERS", raising=False)

    request = _request(
        {
            "x-forwarded-for": "203.0.113.9",
            "cf-connecting-ip": "1.2.3.4",
        },
        peer_ip="127.0.0.1",
    )

    info = get_client_ip_info(request)
    assert info.client_ip == "203.0.113.9"
    assert info.source == "x-forwarded-for"
    # Still logged, so a spoof attempt remains visible after the fact.
    assert info.cf_connecting_ip == "1.2.3.4"


def test_cf_trust_requires_generic_proxy_trust(monkeypatch):
    """TRUST_CLOUDFLARE_HEADERS alone does not enable header trust."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    request = _request({"cf-connecting-ip": "203.0.113.9"}, peer_ip="172.19.0.8")

    info = get_client_ip_info(request)
    assert info.client_ip == "172.19.0.8"
    assert info.source == "socket"


def test_cf_connecting_ipv6_wins_under_pseudo_ipv4(monkeypatch):
    """Pseudo IPv4 "Overwrite headers" replaces CF-Connecting-IP with a synthetic.

    Cloudflare puts a Class E address derived from the visitor in
    CF-Connecting-IP and the real address in CF-Connecting-IPv6. Taking the
    synthetic would route the client down the IPv4 bucketing path and undo the
    /64 grouping this module exists to provide.
    """
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    request = _request(
        {
            "cf-connecting-ip": "240.1.2.3",
            "cf-connecting-ipv6": "2001:db8:abcd:1234::5",
        },
        peer_ip="127.0.0.1",
    )

    info = get_client_ip_info(request)
    assert info.client_ip == "2001:db8:abcd:1234::5"
    assert info.source == "cf-connecting-ipv6"
    # Both are retained so the synthetic remains visible in logs.
    assert info.cf_connecting_ip == "240.1.2.3"
    assert info.cf_connecting_ipv6 == "2001:db8:abcd:1234::5"
    assert get_client_ip_bucket(request) == "2001:db8:abcd:1234::/64"


def test_pseudo_ipv4_rotation_stays_in_one_bucket(monkeypatch):
    """Rotation within the /64 must not escape limits just because Pseudo IPv4 is on.

    Each rotated address yields a different synthetic Class E value, so
    bucketing on CF-Connecting-IP would hand out a fresh bucket every time.
    """
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    buckets = {
        get_client_ip_bucket(
            _request(
                {"cf-connecting-ip": synthetic, "cf-connecting-ipv6": real},
                peer_ip="127.0.0.1",
            )
        )
        for synthetic, real in (
            ("240.1.2.3", "2001:db8:abcd:1234::1"),
            ("240.9.8.7", "2001:db8:abcd:1234:9c2b:1f4e:aa01:7d3f"),
            ("240.4.5.6", "2001:db8:abcd:1234:4411:beef:0:2"),
        )
    }
    assert buckets == {"2001:db8:abcd:1234::/64"}


def test_cf_connecting_ipv6_ignored_without_cloudflare_trust(monkeypatch):
    """The IPv6 variant is gated by the same flag as CF-Connecting-IP."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.delenv("TRUST_CLOUDFLARE_HEADERS", raising=False)

    request = _request(
        {
            "x-forwarded-for": "203.0.113.9",
            "cf-connecting-ipv6": "2001:db8:abcd:1234::5",
        },
        peer_ip="127.0.0.1",
    )

    info = get_client_ip_info(request)
    assert info.client_ip == "203.0.113.9"
    assert info.source == "x-forwarded-for"
    assert info.cf_connecting_ipv6 == "2001:db8:abcd:1234::5"


def test_cf_connecting_ip_preserves_ipv6_client(monkeypatch):
    """An IPv6 client reaching an IPv4-only origin through Cloudflare logs in full."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    request = _request(
        {"cf-connecting-ip": "2001:db8:abcd:1234::5"},
        peer_ip="127.0.0.1",
    )

    info = get_client_ip_info(request)
    assert info.client_ip == "2001:db8:abcd:1234::5"
    assert info.peer_ip == "127.0.0.1"


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        # IPv4 buckets on the exact address.
        ("203.0.113.9", "203.0.113.9"),
        # IPv6 collapses to the delegated /64.
        ("2001:db8:abcd:1234::5", "2001:db8:abcd:1234::/64"),
        ("2001:db8:abcd:1234:ffff:ffff:ffff:ffff", "2001:db8:abcd:1234::/64"),
        # A different /64 is a different bucket.
        ("2001:db8:abcd:9999::1", "2001:db8:abcd:9999::/64"),
        # IPv4-mapped literals keep their embedded IPv4 identity — folding them
        # by prefix would collapse every IPv4 client into a single ::/64.
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        # Unparseable values pass through untouched.
        ("unknown", "unknown"),
        ("", ""),
    ],
)
def test_normalize_ip_bucket(ip, expected):
    """IPv6 buckets on /64; IPv4 and non-addresses bucket on themselves."""
    assert normalize_ip_bucket(ip) == expected


def test_rotating_ipv6_privacy_addresses_share_a_bucket():
    """RFC 4941 rotation within one /64 must not create fresh rate-limit buckets."""
    rotated = [
        "2001:db8:abcd:1234::1",
        "2001:db8:abcd:1234:9c2b:1f4e:aa01:7d3f",
        "2001:db8:abcd:1234:4411:beef:0:2",
    ]
    assert len({normalize_ip_bucket(ip) for ip in rotated}) == 1


def test_ipv4_mapped_clients_keep_distinct_buckets():
    """Dual-stack listeners report IPv4 peers as ::ffff:… — they must not merge.

    Collapsing them would put every IPv4 client into one rate-limit bucket,
    locking out unrelated users once any single client hit the limit.
    """
    buckets = {
        normalize_ip_bucket(ip)
        for ip in ("::ffff:192.0.2.1", "::ffff:203.0.113.9", "::ffff:8.8.8.8")
    }
    assert len(buckets) == 3


def test_ipv4_mapped_bucket_matches_plain_ipv4():
    """The same client reaches one bucket whether or not the peer is mapped."""
    assert normalize_ip_bucket("::ffff:192.0.2.1") == normalize_ip_bucket("192.0.2.1")


def test_get_client_ip_bucket_normalizes_resolved_ip(monkeypatch):
    """The bucket helper applies /64 folding to the resolved client IP."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

    request = _request(
        {"cf-connecting-ip": "2001:db8:abcd:1234::5"},
        peer_ip="127.0.0.1",
    )

    assert get_client_ip_bucket(request) == "2001:db8:abcd:1234::/64"
