"""What an IP address reaches, IPv4 carried inside IPv6 included.

An IPv6 address can carry an IPv4 address that a connection to it ends up at:
an IPv4-mapped address is the IPv4 host itself on a dual-stack socket, a NAT64
prefix is translated to the IPv4 host by the network's translator, and 6to4 and
Teredo tunnel to it. Python's ``ipaddress`` judges the IPv6 address alone, so
``::169.254.169.254`` and ``64:ff9b::169.254.169.254`` are "global" and have no
``ipv4_mapped``: a check that unwraps only the mapped form lets them through to
the address they embed.

This module is the one place that unwraps them, structurally rather than by
pattern: every check that decides where the gateway may connect -- the cloud
metadata guard (:mod:`serving.utils.metadata_addresses`), batch webhooks and
remote media -- reads its answer.

The embeddings recognized:

* ``::ffff:0:0/96`` IPv4-mapped (RFC 4291 §2.5.5.2);
* ``::/96`` IPv4-compatible (RFC 4291 §2.5.5.1, deprecated but still routed by
  some stacks);
* ``::ffff:0:0:0/96`` IPv4-translated (RFC 2765);
* ``64:ff9b::/96`` the well-known NAT64 prefix (RFC 6052);
* ``64:ff9b:1::/48`` the local-use NAT64 prefix (RFC 8215), where the operator
  chooses the prefix length, so each RFC 6052 position is considered;
* ``2002::/16`` 6to4 (RFC 3056);
* ``2001::/32`` Teredo (RFC 4380), server and client.

A zone (``fe80::1%eth0``) is dropped before anything else: ``ipaddress`` counts
it in equality, so a scoped address would not match the plain one, while the
kernel connects both to the same host.
"""

from __future__ import annotations

import ipaddress
from typing import TypeAlias

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address

IPV4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")

#: The prefixes that carry an IPv4 address in their last 32 bits.
_LAST_32_BITS = (IPV4_COMPATIBLE, IPV4_TRANSLATED, NAT64_WELL_KNOWN)

#: The RFC 6052 §2.2 prefix lengths a NAT64 prefix inside ``64:ff9b:1::/48`` may
#: have (it is at least /48), with the bytes of the address that hold the IPv4
#: address for each. Byte 8 (bits 64-71) is the reserved "u" octet, never part
#: of it.
_RFC6052_IPV4_BYTES: dict[int, tuple[slice, ...]] = {
    48: (slice(6, 8), slice(9, 11)),
    56: (slice(7, 8), slice(9, 12)),
    64: (slice(9, 13),),
    96: (slice(12, 16),),
}


def parse_ip(address: str | IPAddress) -> IPAddress:
    """Parse *address* into an IP address without its IPv6 zone.

    Raises:
        ValueError: *address* is not an IP address.
    """
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is None:
            return address
        return ipaddress.IPv6Address(address.packed)
    return ipaddress.ip_address(str(address).strip().split("%", 1)[0])


def _rfc6052_ipv4(ip: ipaddress.IPv6Address, prefixlen: int) -> ipaddress.IPv4Address:
    packed = ip.packed
    return ipaddress.IPv4Address(b"".join(packed[part] for part in _RFC6052_IPV4_BYTES[prefixlen]))


def embedded_ipv4(address: str | IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """Every IPv4 address a connection to *address* may reach, other than itself.

    Empty for an IPv4 address and for an IPv6 address that embeds none. A
    local-use NAT64 address yields one candidate per prefix length its operator
    may have chosen, and a Teredo address both its server and its client: the
    caller judges every one.

    Raises:
        ValueError: *address* is not an IP address.
    """
    ip = parse_ip(address)
    if isinstance(ip, ipaddress.IPv4Address):
        return ()
    if ip.ipv4_mapped is not None:
        return (ip.ipv4_mapped,)
    if any(ip in network for network in _LAST_32_BITS):
        return (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF),)
    if ip in NAT64_LOCAL_USE:
        return tuple(dict.fromkeys(_rfc6052_ipv4(ip, length) for length in _RFC6052_IPV4_BYTES))
    if ip.sixtofour is not None:
        return (ip.sixtofour,)
    if ip.teredo is not None:
        return tuple(dict.fromkeys(ip.teredo))
    return ()


def reachable_addresses(address: str | IPAddress) -> tuple[IPAddress, ...]:
    """*address* itself, without its zone, followed by every IPv4 address it embeds.

    Raises:
        ValueError: *address* is not an IP address.
    """
    ip = parse_ip(address)
    return (ip, *embedded_ipv4(ip))


def is_public_unicast(address: str | IPAddress) -> bool:
    """Whether *address* is a public unicast destination, in any spelling.

    IPv4 carried in IPv6 is judged as the IPv4 address it reaches (every
    candidate of it must be public). Teredo and local-use NAT64 are refused
    outright: the first tunnels through a relay the gateway does not choose,
    and the second is, by definition, a private translator's. Anything that is
    not an IP address is not public.
    """
    try:
        ip = parse_ip(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.teredo is not None or ip in NAT64_LOCAL_USE:
            return False
        embedded = embedded_ipv4(ip)
        if embedded:
            return all(_is_public(candidate) for candidate in embedded)
    return _is_public(ip)


def _is_public(ip: IPAddress) -> bool:
    return ip.is_global and not ip.is_multicast
