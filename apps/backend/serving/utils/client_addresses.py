"""The key client addresses are compared by: an IPv4 address, or an IPv6 /64.

Where two pieces of activity came from is compared by this key rather than by
the raw address. A host's temporary IPv6 privacy addresses rotate inside its
/64, so keying an IPv6 address by its /64 keeps one host one key while its
addresses change. What the gateway records when it could not attribute a
request to a client (``"unknown"``), and addresses that cannot be a client's,
have no key.
"""

from __future__ import annotations

import ipaddress

from serving.utils.ip_classification import parse_ip

#: An IPv6 address is keyed by this prefix: a host's temporary addresses
#: rotate inside its /64.
IPV6_PREFIX_LENGTH = 64


def address_key(raw: object) -> str | None:
    """The key *raw* is compared by, or None for no usable client address.

    An IPv4 address is its own key, an IPv4-mapped IPv6 address included. An
    IPv6 address is keyed by its /64. ``"unknown"`` (what the gateway records
    when it could not attribute a request to a client), loopback, link-local,
    multicast, reserved and unspecified addresses have no key. Private
    addresses keep theirs: the gateway records one only for a network the
    operator authorized as direct clients (``TRUSTED_DIRECT_CLIENT_NETWORKS``).
    The first entry of a comma-separated list (an old forwarding chain) is the
    one keyed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.split(",", 1)[0].strip()
    if not text:
        return None
    try:
        ip = parse_ip(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((ip, IPV6_PREFIX_LENGTH), strict=False))
    return str(ip)
