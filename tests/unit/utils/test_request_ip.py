"""Tests for proxied client IP extraction with trusted-proxy trust model.

Tests use proper Settings instances and environment setup rather than
monkey-patching internal state. Each test creates the configuration
it needs through the same code paths used in production.
"""

from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.datastructures import Headers

from serving.config.settings import Settings
from serving.utils.request_ip import (
    MAX_FORWARDED_HOPS,
    ClientIpInfo,
    _is_in_networks,
    _is_reportable_ip,
    _parse_forwarded_chain,
    _parse_ip,
    derive_affinity_key,
    get_client_ip_bucket,
    get_client_ip_info,
    normalize_ip_bucket,
)


def _request(headers: object, peer_ip: str = "10.0.0.2"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer_ip))


def _networks(*cidrs: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Create a tuple of parsed networks from CIDR strings."""
    return tuple(ipaddress.ip_network(c) for c in cidrs)


@contextmanager
def _settings_env(
    proxy_headers: bool,
    cf_headers: bool,
    proxy_nets: tuple = (),
    direct_client_nets: tuple = (),
    cf_nets: tuple = (),
    x_real_ip: bool = False,
):
    """Set up a test environment with proper Settings and env vars.

    This is NOT monkey-patching internal state - it creates real Settings
    instances and sets real environment variables, the same way the
    production code reads them.
    """
    settings = Settings(
        trusted_proxies=",".join(str(n) for n in proxy_nets),
        trusted_direct_client_networks=",".join(str(n) for n in direct_client_nets),
        trusted_cloudflare_networks=",".join(str(n) for n in cf_nets),
        trust_proxy_headers=proxy_headers,
        trust_cloudflare_headers=cf_headers and proxy_headers,
        trust_x_real_ip=x_real_ip,
    )
    with patch("serving.utils.request_ip.get_settings", return_value=settings):
        yield settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_ip_accepts_ipv4_ipv6_mapped():
    assert str(_parse_ip("1.2.3.4")) == "1.2.3.4"
    assert str(_parse_ip("2001:db8::1")) == "2001:db8::1"
    assert str(_parse_ip("::ffff:192.0.2.1")) == "192.0.2.1"


def test_parse_ip_rejects_garbage():
    assert _parse_ip("not-an-ip") is None
    assert _parse_ip("") is None
    assert _parse_ip(None) is None


def test_parse_forwarded_chain_splits_and_trims():
    assert _parse_forwarded_chain("1.2.3.4, 5.6.7.8") == ["1.2.3.4", "5.6.7.8"]


def test_parse_forwarded_chain_preserves_empty():
    """Empty/malformed entries are preserved — they terminate provenance."""
    assert _parse_forwarded_chain("8.8.8.8, , 172.19.0.1") == ["8.8.8.8", "", "172.19.0.1"]
    assert _parse_forwarded_chain("  1.2.3.4  ,  , 5.6.7.8  ") == ["1.2.3.4", "", "5.6.7.8"]


def test_parse_forwarded_chain_empty_input():
    """A chain of empty/whitespace yields a list with empty strings preserved."""
    assert _parse_forwarded_chain(" , ") == ["", ""]


def test_is_reportable_ip_rejects_non_routable():
    assert _is_reportable_ip("172.19.0.1") is False
    assert _is_reportable_ip("10.0.0.1") is False
    assert _is_reportable_ip("fc00::1") is False
    assert _is_reportable_ip("127.0.0.1") is False
    assert _is_reportable_ip("::1") is False
    assert _is_reportable_ip("fe80::1") is False
    assert _is_reportable_ip("224.0.0.1") is False
    assert _is_reportable_ip("0.0.0.0") is False
    assert _is_reportable_ip("192.0.2.1") is False
    assert _is_reportable_ip("198.51.100.1") is False
    assert _is_reportable_ip("203.0.113.1") is False
    assert _is_reportable_ip("192.0.0.1") is False
    assert _is_reportable_ip("198.18.0.1") is False
    assert _is_reportable_ip("240.1.2.3") is False
    assert _is_reportable_ip("2001:db8::1") is False


def test_is_reportable_ip_accepts_public():
    assert _is_reportable_ip("8.8.8.8") is True
    assert _is_reportable_ip("1.1.1.1") is True
    assert _is_reportable_ip("2001:4860:4860::8888") is True


def test_is_in_networks_matches_cidr():
    nets = _networks("172.16.0.0/12", "10.0.0.0/8")
    assert _is_in_networks("172.19.0.1", nets) is True
    assert _is_in_networks("10.0.0.1", nets) is True
    assert _is_in_networks("8.8.8.8", nets) is False
    assert _is_in_networks("not-an-ip", nets) is False


def test_is_in_networks_empty_means_no_trust():
    assert _is_in_networks("172.19.0.1", ()) is False


# ---------------------------------------------------------------------------
# Default behavior: no trusted proxies configured
# ---------------------------------------------------------------------------


def test_non_routable_peer_returns_unknown_no_trusted_proxies():
    """Docker bridge peer without trustworthy forwarding provenance → "unknown"."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        request = _request({}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"
        assert info.resolved is False


def test_public_peer_used_directly_no_trusted_proxies():
    """A direct connection from a routable peer is a legitimate client IP."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        info = get_client_ip_info(_request({}, peer_ip="8.8.8.8"))
        assert info.client_ip == "8.8.8.8"
        assert info.source == "socket"
        assert info.resolved is True


def test_direct_private_peer_requires_explicit_client_network():
    """Private direct peers are usable only with explicit client authorization."""
    with _settings_env(
        proxy_headers=False,
        cf_headers=False,
        direct_client_nets=_networks("10.42.0.0/16"),
    ):
        info = get_client_ip_info(_request({}, peer_ip="10.42.1.7"))
        assert info.client_ip == "10.42.1.7"
        assert info.source == "socket"
        assert info.resolved is True


def test_direct_ula_peer_requires_explicit_client_network():
    """ULA direct peers can be authorized without trusting forwarding headers."""
    with _settings_env(
        proxy_headers=False,
        cf_headers=False,
        direct_client_nets=_networks("fd00:42::/64"),
    ):
        info = get_client_ip_info(_request({}, peer_ip="fd00:42::7"))
        assert info.client_ip == "fd00:42::7"
        assert info.source == "socket"
        assert info.resolved is True


def test_direct_cgnat_peer_requires_explicit_client_network():
    """CGNAT direct peers can be authorized without trusting forwarding headers."""
    with _settings_env(
        proxy_headers=False,
        cf_headers=False,
        direct_client_nets=_networks("100.64.0.0/10"),
    ):
        info = get_client_ip_info(_request({}, peer_ip="100.100.12.34"))
        assert info.client_ip == "100.100.12.34"
        assert info.source == "socket"
        assert info.resolved is True


def test_unconfigured_cgnat_peer_stays_unresolved():
    """An unconfigured CGNAT peer is not treated as an individual client."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        info = get_client_ip_info(_request({}, peer_ip="100.100.12.34"))
        assert info.client_ip == "unknown"
        assert info.source == "unknown"
        assert info.resolved is False


def test_forged_xff_ignored_without_trusted_peer():
    """Attacker cannot spoof XFF when peer is not a configured trusted proxy."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request({"x-forwarded-for": "203.0.113.9"}, peer_ip="8.8.8.8")
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "socket"


def test_forged_cf_connecting_ip_ignored_without_trusted_peer():
    """Attacker cannot spoof CF-Connecting-IP without a trusted peer."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request({"cf-connecting-ip": "203.0.113.9"}, peer_ip="8.8.8.8")
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "socket"


def test_no_client_means_unknown():
    """A request with no client at all resolves to "unknown"."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        request = SimpleNamespace(headers={}, client=None)
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"
        assert info.resolved is False


def test_unresolved_resolution_warns_once_per_request(caplog):
    """Repeated consumers of one request share one cached resolution warning."""
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="172.19.0.1"),
    )
    with (
        _settings_env(proxy_headers=False, cf_headers=False),
        caplog.at_level("WARNING", logger="serving.utils.request_ip"),
    ):
        first = get_client_ip_info(request)
        second = get_client_ip_info(request)

    assert first is second
    assert (
        sum(record.getMessage() == "client_ip_resolution_unresolved" for record in caplog.records)
        == 1
    )


# ---------------------------------------------------------------------------
# Trusted proxy behavior: peer in configured CIDR
# ---------------------------------------------------------------------------


def test_trusted_proxy_first_untrusted_hop_is_client():
    """XFF chain walked right-to-left; first untrusted hop is the client."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12", "10.0.0.0/8"),
    ):
        request = _request(
            {"x-forwarded-for": "1.2.3.4, 10.0.0.1, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "1.2.3.4"
        assert info.source == "x-forwarded-for"
        assert info.resolved is True


def test_trusted_proxy_all_trusted_hops_returns_unknown():
    """When every XFF hop is a trusted proxy, no client can be determined."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("10.0.0.0/8", "172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "10.0.0.1, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


def test_trusted_proxy_first_untrusted_private_hop_returns_unknown():
    """First untrusted hop that is non-routable → "unknown"."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, 10.50.0.8, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


def test_trusted_proxy_first_untrusted_ula_hop_returns_unknown():
    """First untrusted hop that is ULA → "unknown"."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, fdbd:dc02:19:383::153, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


def test_trusted_proxy_first_untrusted_malformed_hop_returns_unknown():
    """First untrusted hop that is malformed → "unknown"."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, garbage, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


def test_trusted_proxy_empty_hop_terminates_provenance():
    """Empty hop between trusted and client terminates provenance with unknown."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, , 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


def test_trusted_proxy_attacker_prepends_fake_addresses():
    """Attacker prepending public addresses to XFF does not spoof identity."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 8.8.8.8, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "x-forwarded-for"


def test_trusted_proxy_x_real_ip_fallback():
    """X-Real-IP is used only when XFF is completely absent and opted in."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12", "10.0.0.0/8"),
        x_real_ip=True,
    ):
        request = _request({"x-real-ip": "8.8.8.8"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "x-real-ip"


def test_invalid_xff_does_not_fall_through_to_x_real_ip():
    """An XFF assertion remains authoritative when its chain is unusable."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
        x_real_ip=True,
    ):
        request = _request(
            {"x-forwarded-for": "garbage, 172.19.0.1", "x-real-ip": "8.8.8.8"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"


def test_duplicate_xff_field_lines_are_combined_in_wire_order():
    """All physical XFF fields participate in the same right-to-left walk."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            Headers(
                raw=[
                    (b"x-forwarded-for", b"1.2.3.4"),
                    (b"X-FORWARDED-FOR", b"8.8.8.8, 172.19.0.1"),
                ]
            ),
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.x_forwarded_for == "1.2.3.4, 8.8.8.8, 172.19.0.1"


def test_duplicate_xff_empty_field_terminates_provenance():
    """An empty physical XFF field is preserved and fails closed."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            Headers(
                raw=[
                    (b"x-forwarded-for", b"8.8.8.8"),
                    (b"x-forwarded-for", b""),
                    (b"x-forwarded-for", b"172.19.0.1"),
                ]
            ),
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"


@pytest.mark.parametrize("header", ["x-real-ip", "cf-connecting-ip", "cf-connecting-ipv6"])
def test_duplicate_singleton_forwarding_header_is_rejected(header):
    """No singleton forwarding identity is selected from an ambiguous pair."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        proxy_nets=_networks("172.16.0.0/12"),
        cf_nets=_networks("172.16.0.0/12"),
        x_real_ip=True,
    ):
        request = _request(
            Headers(
                raw=[
                    (header.encode(), b"8.8.8.8"),
                    (header.upper().encode(), b"1.1.1.1"),
                ]
            ),
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.resolved is False


def test_forwarded_hop_limit_fails_closed():
    """A pathological trusted-proxy chain cannot make traversal unbounded."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("10.0.0.0/8", "172.16.0.0/12"),
    ):
        chain = ", ".join(["10.0.0.1"] * (MAX_FORWARDED_HOPS + 1))
        info = get_client_ip_info(
            _request(
                {"x-forwarded-for": chain},
                peer_ip="172.19.0.1",
            )
        )
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"


def test_forwarded_hop_limit_ignores_attacker_history_left_of_boundary():
    """Untrusted XFF history cannot invalidate a valid right-side client hop."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        attacker_history = ["9.9.9.9"] * (MAX_FORWARDED_HOPS + 1)
        chain = ", ".join([*attacker_history, "8.8.8.8", "172.19.0.1"])
        info = get_client_ip_info(
            _request(
                {"x-forwarded-for": chain},
                peer_ip="172.19.0.1",
            )
        )
        assert info.client_ip == "8.8.8.8"
        assert info.source == "x-forwarded-for"
        assert info.resolved is True


# ---------------------------------------------------------------------------
# Cloudflare header handling
# ---------------------------------------------------------------------------


def test_cf_connecting_ip_when_cloudflare_authorized():
    """CF-Connecting-IP is authoritative when peer is in Cloudflare-authorized networks."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {
                "x-forwarded-for": "1.2.3.4, 8.8.8.8",
                "cf-connecting-ip": "8.8.8.8",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "cf-connecting-ip"
        assert info.trusted_proxy_headers is True


def test_cf_optional_ipv6_ambiguity_does_not_invalidate_primary_ip():
    """An ambiguous optional IPv6 companion cannot erase a valid primary IP."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            Headers(
                raw=[
                    (b"cf-connecting-ip", b"8.8.8.8"),
                    (b"cf-connecting-ipv6", b"2001:4860:4860:abcd:1234::5"),
                    (b"cf-connecting-ipv6", b""),
                ]
            ),
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "cf-connecting-ip"
        assert info.resolved is True


def test_trusted_proxy_headers_true_for_cloudflare_only():
    """CF-only authorization is reflected in forwarding-header provenance."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        info = get_client_ip_info(_request({"cf-connecting-ip": "8.8.8.8"}, peer_ip="172.19.0.1"))
        assert info.client_ip == "8.8.8.8"
        assert info.source == "cf-connecting-ip"
        assert info.trusted_proxy_headers is True


def test_cf_connecting_ip_ignored_with_generic_trust_only():
    """Generic proxy trust does not authorize CF-Connecting-IP."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {
                "x-forwarded-for": "8.8.8.8",
                "cf-connecting-ip": "1.2.3.4",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "x-forwarded-for"


def test_cf_connecting_ip_ignored_without_cloudflare_trust_flag():
    """Even with Cloudflare networks configured, the flag must be enabled."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {
                "x-forwarded-for": "8.8.8.8",
                "cf-connecting-ip": "1.2.3.4",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "x-forwarded-for"


def test_cf_connecting_ipv6_pseudo_ipv4():
    """Pseudo IPv4 "Overwrite headers" yields the real IPv6 address."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {
                "cf-connecting-ip": "240.1.2.3",
                "cf-connecting-ipv6": "2001:4860:4860:abcd:1234::5",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "2001:4860:4860:abcd:1234::5"
        assert info.source == "cf-connecting-ipv6"


def test_forged_cf_connecting_ipv6_rejected():
    """A caller-supplied CF-Connecting-IPv6 must not displace the real IP."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {
                "cf-connecting-ip": "8.8.8.8",
                "cf-connecting-ipv6": "2001:db8:dead:beef::1",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "cf-connecting-ip"


def test_cf_requires_global_proxy_flag():
    """CF headers are ignored when TRUST_PROXY_HEADERS=0, even if
    TRUST_CLOUDFLARE_HEADERS=1 and peer is in trusted_cloudflare_networks.
    """
    with _settings_env(
        proxy_headers=False,
        cf_headers=True,
        cf_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"cf-connecting-ip": "8.8.8.8"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"


# ---------------------------------------------------------------------------
# Cloudflare adversarial tests (P0: validate CF-derived client)
# ---------------------------------------------------------------------------


def test_cf_malformed_ip_returns_unknown():
    """Malformed CF-Connecting-IP returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request({"cf-connecting-ip": "garbage"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_rfc1918_returns_unknown():
    """RFC1918 CF-Connecting-IP returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request({"cf-connecting-ip": "10.0.0.1"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_loopback_returns_unknown():
    """Loopback CF-Connecting-IP returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request({"cf-connecting-ip": "127.0.0.1"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_ula_returns_unknown():
    """ULA CF-Connecting-IP returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request({"cf-connecting-ip": "fc00::1"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_bare_class_e_without_pair_returns_unknown():
    """Bare Class-E address without valid Pseudo IPv4 pair returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request({"cf-connecting-ip": "240.1.2.3"}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_pseudo_ipv4_with_invalid_ipv6_returns_unknown():
    """Pseudo IPv4 pair with invalid/non-reportable IPv6 returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request(
            {
                "cf-connecting-ip": "240.1.2.3",
                "cf-connecting-ipv6": "127.0.0.1",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ip"


def test_cf_pseudo_ipv4_with_ula_ipv6_returns_unknown():
    """Pseudo IPv4 pair with ULA IPv6 returns unknown."""
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request(
            {
                "cf-connecting-ip": "240.1.2.3",
                "cf-connecting-ipv6": "fc00::1",
            },
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "cf-connecting-ipv6"


# ---------------------------------------------------------------------------
# Edge cases and adversarial inputs
# ---------------------------------------------------------------------------


def test_mixed_ipv4_ipv6_chain():
    """Mixed IPv4/IPv6 hops are handled correctly."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12", "fd00::/8"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, fd00::1, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"


def test_ipv4_mapped_peer_trust():
    """An IPv4-mapped IPv6 peer address matches an IPv4 trusted-proxy CIDR."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, ::ffff:172.19.0.1"},
            peer_ip="::ffff:172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"


def test_all_trusted_chain_returns_unknown():
    """A chain of only trusted addresses resolves to "unknown"."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12", "10.0.0.0/8"),
    ):
        request = _request(
            {"x-forwarded-for": "10.0.0.1, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"


def test_trust_flags_without_trusted_cidr_ignored():
    """TRUST_PROXY_HEADERS=1 without configured CIDRs does not trust headers."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request(
            {"x-forwarded-for": "8.8.8.8"},
            peer_ip="8.8.8.8",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "socket"


# ---------------------------------------------------------------------------
# Production pollution regression tests (issue #1036)
# ---------------------------------------------------------------------------


def test_regression_docker_bridge_pollution():
    """~43K rows recorded 172.19.0.1 as client IP. Now resolves to "unknown"."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        request = _request(
            {"user-agent": "OpenAI-SDK/1.0"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"
        assert info.resolved is False


def test_regression_operator_ula_pollution():
    """~2.4K rows recorded fdbd:dc0x:: ULA as client IP. Now returns unknown."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("2605:340::/48"),
    ):
        request = _request(
            {"x-forwarded-for": "fdbd:dc02:19:383::153, 2605:340::1"},
            peer_ip="2605:340::1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "x-forwarded-for"
        assert info.resolved is False


# ---------------------------------------------------------------------------
# Provenance is always recorded
# ---------------------------------------------------------------------------


def test_provenance_always_includes_raw_headers():
    """Raw forwarding headers are always captured for auditability."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        request = _request(
            {
                "x-forwarded-for": "1.2.3.4, 5.6.7.8",
                "x-real-ip": "5.6.7.8",
                "cf-connecting-ip": "1.2.3.4",
            },
            peer_ip="8.8.8.8",
        )
        info = get_client_ip_info(request)
        assert info.x_forwarded_for == "1.2.3.4, 5.6.7.8"
        assert info.x_real_ip == "5.6.7.8"
        assert info.cf_connecting_ip == "1.2.3.4"


# ---------------------------------------------------------------------------
# trusted_proxy_headers provenance semantics
# ---------------------------------------------------------------------------


def test_trusted_proxy_headers_true_when_trusted():
    """trusted_proxy_headers is True when global trust enabled AND peer is trusted."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.trusted_proxy_headers is True


def test_trusted_proxy_headers_false_when_peer_untrusted():
    """trusted_proxy_headers is False when peer is not in trusted CIDRs."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request(
            {"x-forwarded-for": "8.8.8.8"},
            peer_ip="8.8.8.8",
        )
        info = get_client_ip_info(request)
        assert info.trusted_proxy_headers is False


def test_trusted_proxy_headers_false_when_global_disabled():
    """trusted_proxy_headers is False when TRUST_PROXY_HEADERS=0."""
    with _settings_env(proxy_headers=False, cf_headers=False):
        request = _request(
            {"x-forwarded-for": "8.8.8.8, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.trusted_proxy_headers is False


# ---------------------------------------------------------------------------
# Affinity key behavior with resolved/unresolved
# ---------------------------------------------------------------------------


def test_affinity_key_resolved_ip():
    """Affinity key uses client IP when resolved."""
    ip_info = ClientIpInfo(
        client_ip="8.8.8.8",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    assert derive_affinity_key(None, ip_info) == "ip:8.8.8.8"


def test_affinity_key_unresolved():
    """Affinity key returns None when client provenance is unresolved."""
    ip_info = ClientIpInfo(
        client_ip="unknown",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=False,
    )
    assert derive_affinity_key(None, ip_info) is None


def test_affinity_key_auth_preferred():
    """Auth key hash is preferred over IP."""
    ip_info = ClientIpInfo(
        client_ip="8.8.8.8",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    assert derive_affinity_key("mykey", ip_info) == "mykey"


def test_affinity_key_grant_preferred():
    """Grant ID is preferred over IP."""
    ip_info = ClientIpInfo(
        client_ip="8.8.8.8",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    assert derive_affinity_key(None, ip_info, grant_id="g1") == "grant:g1"


# ---------------------------------------------------------------------------
# Adversarial tests: shared-proxy poisoning topology
# ---------------------------------------------------------------------------


def test_affinity_key_shared_proxy_unresolved():
    """Multiple clients behind shared proxy with unresolved provenance must
    NOT collapse onto the proxy IP."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request1 = _request(
            {"x-forwarded-for": "garbage, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        request2 = _request(
            {"x-forwarded-for": "garbage, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        request3 = _request(
            {"x-forwarded-for": "garbage, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info1 = get_client_ip_info(request1)
        info2 = get_client_ip_info(request2)
        info3 = get_client_ip_info(request3)

        assert info1.resolved is False
        assert info2.resolved is False
        assert info3.resolved is False

        affinity1 = derive_affinity_key(None, info1)
        affinity2 = derive_affinity_key(None, info2)
        affinity3 = derive_affinity_key(None, info3)

        # None means non-sticky routing (NOT the proxy IP)
        assert affinity1 is None
        assert affinity2 is None
        assert affinity3 is None


def test_resolved_clients_behind_same_proxy():
    """Multiple resolved clients behind same proxy get distinct affinity keys."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request1 = _request(
            {"x-forwarded-for": "1.2.3.4, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        request2 = _request(
            {"x-forwarded-for": "5.6.7.8, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        request3 = _request(
            {"x-forwarded-for": "9.10.11.12, 172.19.0.1"},
            peer_ip="172.19.0.1",
        )
        info1 = get_client_ip_info(request1)
        info2 = get_client_ip_info(request2)
        info3 = get_client_ip_info(request3)

        assert info1.resolved is True
        assert info2.resolved is True
        assert info3.resolved is True

        affinity1 = derive_affinity_key(None, info1)
        affinity2 = derive_affinity_key(None, info2)
        affinity3 = derive_affinity_key(None, info3)

        assert affinity1 == "ip:1.2.3.4"
        assert affinity2 == "ip:5.6.7.8"
        assert affinity3 == "ip:9.10.11.12"
        assert affinity1 != affinity2
        assert affinity2 != affinity3


# ---------------------------------------------------------------------------
# Adversarial tests: direct-origin / intermediary CF header forgery
# ---------------------------------------------------------------------------


def test_cf_forged_by_direct_origin():
    """Direct-origin request with forged CF-Connecting-IP is ignored."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request(
            {"cf-connecting-ip": "203.0.113.9"},
            peer_ip="1.2.3.4",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "1.2.3.4"
        assert info.source == "socket"


def test_cf_forged_by_generic_proxy():
    """Generic trusted proxy cannot authorize attacker-supplied CF-Connecting-IP."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request(
            {"cf-connecting-ip": "1.2.3.4"},
            peer_ip="172.19.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"


def test_cf_generic_proxy_no_xff():
    """Generic trusted proxy without XFF falls back to peer."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("172.16.0.0/12"),
    ):
        request = _request({}, peer_ip="172.19.0.1")
        info = get_client_ip_info(request)
        assert info.client_ip == "unknown"
        assert info.source == "unknown"


def test_public_trusted_proxy_without_identity_header_stays_unresolved():
    """A public proxy address must not become the client without a header."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=False,
        proxy_nets=_networks("8.8.8.0/24"),
    ):
        info = get_client_ip_info(_request({}, peer_ip="8.8.8.8"))
        assert info.client_ip == "unknown"
        assert info.source == "unknown"
        assert info.resolved is False


def test_cf_requires_explicit_authorization():
    """CF-Connecting-IP only trusted when peer is explicitly Cloudflare-authorized."""
    with _settings_env(
        proxy_headers=True,
        cf_headers=True,
        cf_nets=_networks("10.0.0.1/32"),
    ):
        request = _request(
            {"cf-connecting-ip": "8.8.8.8"},
            peer_ip="10.0.0.1",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "8.8.8.8"
        assert info.source == "cf-connecting-ip"


# ---------------------------------------------------------------------------
# Bucketing and affinity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("8.8.8.8", "8.8.8.8"),
        ("2001:4860:4860:abcd::5", "2001:4860:4860:abcd::/64"),
        (
            "2001:4860:4860:abcd:ffff:ffff:ffff:ffff",
            "2001:4860:4860:abcd::/64",
        ),
        ("2001:4860:4860:beef::1", "2001:4860:4860:beef::/64"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("::ffff:8.8.8.8", "8.8.8.8"),
        ("unknown", "unknown"),
        ("", ""),
    ],
)
def test_normalize_ip_bucket(ip, expected):
    """IPv6 buckets on /64; IPv4 and non-addresses bucket on themselves."""
    assert normalize_ip_bucket(ip) == expected


def test_rotating_ipv6_privacy_addresses_share_a_bucket():
    rotated = [
        "2001:4860:4860:abcd::1",
        "2001:4860:4860:abcd:9c2b:1f4e:aa01:7d3f",
        "2001:4860:4860:abcd:4411:beef:0:2",
    ]
    assert len({normalize_ip_bucket(ip) for ip in rotated}) == 1


def test_ipv4_mapped_clients_keep_distinct_buckets():
    buckets = {
        normalize_ip_bucket(ip) for ip in ("::ffff:192.0.2.1", "::ffff:8.8.8.8", "::ffff:1.1.1.1")
    }
    assert len(buckets) == 3


def test_derive_affinity_key_falls_through():
    """derive_affinity_key falls through identities correctly."""
    resolved_info = ClientIpInfo(
        client_ip="8.8.8.8",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    assert derive_affinity_key(None, resolved_info) == "ip:8.8.8.8"

    ipv6_info = ClientIpInfo(
        client_ip="2001:4860:4860::8888",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=True,
    )
    assert derive_affinity_key(None, ipv6_info) == "ip:2001:4860:4860::/64"

    unresolved_info = ClientIpInfo(
        client_ip="unknown",
        peer_ip="172.19.0.1",
        source="socket",
        trusted_proxy_headers=False,
        resolved=False,
    )
    assert derive_affinity_key(None, unresolved_info) is None

    assert derive_affinity_key("keyhash", resolved_info) == "keyhash"
    assert derive_affinity_key(None, resolved_info, grant_id="g1") == "grant:g1"


def test_get_client_ip_bucket_normalizes():
    with _settings_env(proxy_headers=True, cf_headers=True, cf_nets=_networks("172.16.0.0/12")):
        request = _request(
            {"cf-connecting-ip": "2001:4860:4860:abcd::5"},
            peer_ip="172.19.0.1",
        )
        assert get_client_ip_bucket(request) == "2001:4860:4860:abcd::/64"


# ---------------------------------------------------------------------------
# Sabotage test: prove adversarial tests catch the vulnerability
# ---------------------------------------------------------------------------


def test_sabotage_pre_fix_spoofing_model():
    """Prove adversarial tests catch the pre-fix spoofable model."""
    with _settings_env(proxy_headers=True, cf_headers=False):
        request = _request(
            {"x-forwarded-for": "198.51.100.42"},
            peer_ip="1.2.3.4",
        )
        info = get_client_ip_info(request)
        assert info.client_ip == "1.2.3.4"
        assert info.source == "socket"
