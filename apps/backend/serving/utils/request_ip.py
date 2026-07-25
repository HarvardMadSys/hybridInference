"""Helpers for extracting a stable client IP from proxied requests."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

# IPv6 clients are routinely delegated a /64 (frequently a /56 or /48), and
# RFC 4941 privacy addresses rotate within it, so a single client can present
# effectively unlimited distinct addresses. Abuse-control and affinity buckets
# therefore collapse IPv6 to its /64 network. IPv4 keeps full-address buckets.
IPV6_BUCKET_PREFIXLEN = 64


def _header_value(value: object) -> str | None:
    """Return a stripped header value when the request provides a real string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class ClientIpInfo:
    """Client IP plus enough provenance to debug proxy hops."""

    client_ip: str
    peer_ip: str
    source: str
    trusted_proxy_headers: bool
    x_forwarded_for: str | None = None
    x_real_ip: str | None = None
    cf_connecting_ip: str | None = None


def normalize_ip_bucket(ip: str) -> str:
    """Return the grouping key used for per-client rate limits and affinity.

    IPv4 addresses (and anything unparseable, such as ``"unknown"`` or a
    scoped literal) bucket on the value itself. IPv6 addresses bucket on
    their ``/64`` network so a client cannot escape a limit by rotating
    through the prefix it was delegated.

    IPv4-mapped literals (``::ffff:192.0.2.1``, which a dual-stack listener
    reports for IPv4 peers) bucket on the embedded IPv4 address. Folding them
    by prefix would collapse every IPv4 client into a single ``::/64``.
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if parsed.version == 4:
        return str(parsed)
    mapped_v4 = parsed.ipv4_mapped
    if mapped_v4 is not None:
        return str(mapped_v4)
    network = ipaddress.ip_network(f"{parsed}/{IPV6_BUCKET_PREFIXLEN}", strict=False)
    return str(network)


def get_client_ip_info(request: Request) -> ClientIpInfo:
    """Return the originating client IP and the socket/proxy peer that supplied it.

    ``TRUST_PROXY_HEADERS`` asserts only that *some* trusted proxy rewrites the
    forwarding headers. Trusting ``CF-Connecting-IP`` additionally requires
    ``TRUST_CLOUDFLARE_HEADERS=1``, which asserts that the immediate proxy is
    Cloudflare and therefore overwrites that header. A non-Cloudflare proxy may
    rewrite ``X-Forwarded-For`` correctly while passing a client-supplied
    ``CF-Connecting-IP`` straight through, so the two facts are gated apart.

    When Cloudflare is trusted its header wins, because Cloudflare always
    overwrites ``CF-Connecting-IP`` but only *appends* to a client-supplied
    ``X-Forwarded-For``, leaving the leftmost entry attacker-controlled.
    """
    peer_ip = request.client.host if request.client else "unknown"
    trusted = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
    trust_cloudflare = trusted and os.getenv("TRUST_CLOUDFLARE_HEADERS", "0") == "1"
    x_forwarded_for = _header_value(request.headers.get("x-forwarded-for"))
    x_real_ip = _header_value(request.headers.get("x-real-ip"))
    cf_connecting_ip = _header_value(request.headers.get("cf-connecting-ip"))

    if trusted:
        if trust_cloudflare and cf_connecting_ip:
            return ClientIpInfo(
                client_ip=cf_connecting_ip,
                peer_ip=peer_ip,
                source="cf-connecting-ip",
                trusted_proxy_headers=True,
                x_forwarded_for=x_forwarded_for,
                x_real_ip=x_real_ip,
                cf_connecting_ip=cf_connecting_ip,
            )

        if x_forwarded_for:
            first_ip = x_forwarded_for.split(",", 1)[0].strip()
            if first_ip:
                return ClientIpInfo(
                    client_ip=first_ip,
                    peer_ip=peer_ip,
                    source="x-forwarded-for",
                    trusted_proxy_headers=True,
                    x_forwarded_for=x_forwarded_for,
                    x_real_ip=x_real_ip,
                    cf_connecting_ip=cf_connecting_ip,
                )

        if x_real_ip:
            return ClientIpInfo(
                client_ip=x_real_ip,
                peer_ip=peer_ip,
                source="x-real-ip",
                trusted_proxy_headers=True,
                x_forwarded_for=x_forwarded_for,
                x_real_ip=x_real_ip,
                cf_connecting_ip=cf_connecting_ip,
            )

    return ClientIpInfo(
        client_ip=peer_ip,
        peer_ip=peer_ip,
        source="socket",
        trusted_proxy_headers=trusted,
        x_forwarded_for=x_forwarded_for,
        x_real_ip=x_real_ip,
        cf_connecting_ip=cf_connecting_ip,
    )


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request."""
    return get_client_ip_info(request).client_ip


def get_client_ip_bucket(request: Request) -> str:
    """Return the rate-limit/affinity bucket key for a request's client IP."""
    return normalize_ip_bucket(get_client_ip(request))
