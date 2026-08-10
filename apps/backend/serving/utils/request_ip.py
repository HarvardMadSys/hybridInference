"""Helpers for extracting a stable client IP from proxied requests.

We prefer Cloudflare's edge-set ``CF-Connecting-IP``, then take the first
*routable* ``X-Forwarded-For`` hop — skipping any private / loopback / ULA hop an
intermediary inserted (e.g. an internal overlay) rather than reporting it as the
client. Skipping those hops is what stops internal-overlay addresses from being
logged as clients. When no forwarded hop is routable we fall back to the socket
peer, as before.

Note: a spoofed *public* leftmost ``X-Forwarded-For`` entry is still taken at face
value. Stripping it correctly requires a configured trusted-proxy CIDR set (so we
can tell our own proxies from client-supplied hops); taking the rightmost public
hop instead would misattribute every client behind a shared public intermediary,
so that hardening is left as a follow-up (see issue #1036).
"""

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

# Cloudflare's Pseudo IPv4 synthetics live in the reserved Class E space. A real
# client address is never drawn from it, so its presence in CF-Connecting-IP is
# what distinguishes a genuine Pseudo IPv4 rewrite from a forged pairing.
PSEUDO_IPV4_NETWORK = ipaddress.ip_network("240.0.0.0/4")

# Non-routable ranges that can never identify a remote client. This is an
# explicit list rather than ``ipaddress.is_private`` / ``is_global`` on purpose:
# those reclassified the documentation and benchmark ranges across CPython
# 3.12.4 / 3.13, so relying on them would make IP resolution depend on the
# interpreter version. RFC 1918, CGNAT (RFC 6598) and IPv6 ULA are stable.
_NON_ROUTABLE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)


def _header_value(value: object) -> str | None:
    """Return a stripped header value when the request provides a real string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _is_reportable_ip(value: str | None) -> bool:
    """True when *value* could plausibly identify a real remote client.

    Rejects anything unparseable plus loopback, link-local, multicast,
    unspecified, RFC 1918 / CGNAT and IPv6 ULA addresses. IPv4-mapped IPv6
    literals are judged by their embedded IPv4 address so a mapped private peer
    is still rejected.
    """
    if not value:
        return False
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return not any(ip in net for net in _NON_ROUTABLE_NETWORKS)


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
    cf_connecting_ipv6: str | None = None


def _pseudo_ipv4_origin(cf_connecting_ip: str | None, cf_connecting_ipv6: str | None) -> str | None:
    """Return the visitor's real IPv6 only for a genuine Pseudo IPv4 rewrite.

    Cloudflare overwrites ``CF-Connecting-IP`` on every request, but it emits
    ``CF-Connecting-IPv6`` *only* under Pseudo IPv4 "Overwrite headers" — when
    that setting is off the header is absent rather than cleared, so a caller
    can supply their own. Preferring it unconditionally would therefore hand an
    attacker the client identity on any ordinary Cloudflare request.

    Both halves of the pair must corroborate each other: the IPv6 header must
    parse as IPv6, and ``CF-Connecting-IP`` must hold the accompanying Class E
    synthetic. Cloudflare controls that second value and a real client address
    is never in ``240.0.0.0/4``, so the pairing cannot be forged from outside.
    """
    if not cf_connecting_ip or not cf_connecting_ipv6:
        return None
    try:
        synthetic = ipaddress.ip_address(cf_connecting_ip)
        original = ipaddress.ip_address(cf_connecting_ipv6)
    except ValueError:
        return None
    if original.version != 6 or synthetic.version != 4:
        return None
    if synthetic not in PSEUDO_IPV4_NETWORK:
        return None
    return str(original)


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


def derive_affinity_key(
    auth_key_hash: str | None,
    client_ip: str,
    *,
    grant_id: str | None = None,
) -> str:
    """Compute the affinity key used for sticky multi-key routing.

    Falls through the caller identities in order of how precisely each names one
    caller:

    1. ``auth_key_hash`` — the hyi-xxx key presented. The ordinary case.
    2. ``grant_id`` — an inference-grant token carries no key hash, so without
       this a sandbox would key on its IP and every sandbox behind one NAT or
       relay address would collapse onto a single binding.
    3. The client IP bucket, for traffic with no credential at all.

    Anonymous IPv6 clients key on their ``/64`` so rotating privacy addresses
    within the delegated prefix keeps landing on the same backend.

    Lives here rather than on a router so every request surface that dispatches
    to a pooled adapter (``/v1/chat/completions``, ``/v1/messages``,
    ``/v1/embeddings``) derives the caller identity the same way.
    """
    if auth_key_hash:
        return auth_key_hash
    if grant_id:
        return f"grant:{grant_id}"
    return f"ip:{normalize_ip_bucket(client_ip)}"


def get_client_ip_info(request: Request) -> ClientIpInfo:
    """Return the originating client IP and the socket/proxy peer that supplied it.

    ``TRUST_PROXY_HEADERS`` asserts only that *some* trusted proxy rewrites the
    forwarding headers. Trusting ``CF-Connecting-IP`` additionally requires
    ``TRUST_CLOUDFLARE_HEADERS=1``, which asserts that the immediate proxy is
    Cloudflare and therefore overwrites that header. A non-Cloudflare proxy may
    rewrite ``X-Forwarded-For`` correctly while passing a client-supplied
    ``CF-Connecting-IP`` straight through, so the two facts are gated apart.

    Resolution order, first match wins (proxy headers only when trusted):

    1. ``CF-Connecting-IP`` — Cloudflare's single, edge-set client address
       (``CF-Connecting-IPv6`` outranks it only for a corroborated Pseudo IPv4
       pair; see :func:`_pseudo_ipv4_origin`).
    2. The first *routable* ``X-Forwarded-For`` hop (left-to-right). Private /
       loopback / ULA hops an intermediary inserted are skipped rather than
       reported as the client; a spoofed public leftmost hop is still trusted
       (see the module docstring on why stripping it needs a trusted-proxy list).
    3. ``X-Real-IP`` when it is routable.
    4. The socket peer — a direct connection, or the last resort when no
       forwarded hop is routable.
    """
    peer_ip = request.client.host if request.client else "unknown"
    trusted = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
    trust_cloudflare = trusted and os.getenv("TRUST_CLOUDFLARE_HEADERS", "0") == "1"
    x_forwarded_for = _header_value(request.headers.get("x-forwarded-for"))
    x_real_ip = _header_value(request.headers.get("x-real-ip"))
    cf_connecting_ip = _header_value(request.headers.get("cf-connecting-ip"))
    cf_connecting_ipv6 = _header_value(request.headers.get("cf-connecting-ipv6"))

    def _info(client_ip: str, source: str) -> ClientIpInfo:
        return ClientIpInfo(
            client_ip=client_ip,
            peer_ip=peer_ip,
            source=source,
            trusted_proxy_headers=trusted,
            x_forwarded_for=x_forwarded_for,
            x_real_ip=x_real_ip,
            cf_connecting_ip=cf_connecting_ip,
            cf_connecting_ipv6=cf_connecting_ipv6,
        )

    if trusted:
        # Cloudflare's edge-set header is authoritative and un-spoofable behind CF.
        if trust_cloudflare and cf_connecting_ip:
            pseudo_origin = _pseudo_ipv4_origin(cf_connecting_ip, cf_connecting_ipv6)
            return _info(
                pseudo_origin or cf_connecting_ip,
                "cf-connecting-ipv6" if pseudo_origin else "cf-connecting-ip",
            )

        # First routable hop (left-to-right): skip private/loopback/ULA hops an
        # upstream inserted — reporting one is what leaked internal-overlay
        # addresses as clients. A spoofed *public* leftmost hop is still trusted;
        # discarding it needs a trusted-proxy list (see the module docstring).
        if x_forwarded_for:
            for hop in x_forwarded_for.split(","):
                hop = hop.strip()
                if _is_reportable_ip(hop):
                    return _info(hop, "x-forwarded-for")

        if _is_reportable_ip(x_real_ip):
            return _info(x_real_ip, "x-real-ip")  # type: ignore[arg-type]

    # No trustworthy forwarded hop: fall back to the socket peer, as before. A
    # direct public connection is a real client; an internal peer (docker bridge,
    # etc.) is a separate, pre-existing pollution class left untouched so this
    # change stays scoped to the spoofable-leftmost-X-Forwarded-For leak.
    return _info(peer_ip, "socket")


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request."""
    return get_client_ip_info(request).client_ip


def get_client_ip_bucket(request: Request) -> str:
    """Return the rate-limit/affinity bucket key for a request's client IP."""
    return normalize_ip_bucket(get_client_ip(request))
