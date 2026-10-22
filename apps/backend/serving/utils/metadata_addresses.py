"""Cloud instance metadata endpoints: never an upstream, refused at connect time.

Every cloud hands a VM its own credentials over plain http at a fixed address
(``169.254.169.254`` and friends). No inference upstream lives there, so the
gateway refuses them as a destination -- both when an admin types a Base URL
(``provider_routes._validate_base_url``) and when a socket is actually opened.

The second check is the one that holds. A name is resolved again on every new
connection, so a host that answered ``10.1.2.3`` while its Base URL was being
validated can answer ``169.254.169.254`` when the probe or a later inference
request connects. ``refuse_metadata_socket`` is an aiohttp ``socket_factory``:
it sees the exact address about to be connected -- a resolved name or an IP
literal alike -- and refuses it before a socket exists.
"""

from __future__ import annotations

import errno
import ipaddress
import socket
from typing import Any

METADATA_HOSTNAMES = frozenset({"metadata.google.internal"})

_METADATA_NETWORKS = (
    # Link-local: the AWS, GCP, Azure and Tencent Cloud metadata endpoints, and
    # the AWS ECS / EKS Pod Identity credential agents (169.254.170.2 / .23).
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # AWS IPv6: instance metadata at ::254, EKS Pod Identity at ::23.
    ipaddress.ip_network("fd00:ec2::/64"),
)
_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("100.100.100.200")})  # Alibaba Cloud


def is_metadata_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``address`` reaches a cloud metadata endpoint, in any spelling.

    An IPv6 zone (``fd00:ec2::254%0``) is dropped first: ``ipaddress`` counts it
    in equality, so a scoped address would not match the plain one, while the
    kernel connects both to the same host. IPv4-mapped IPv6 is unwrapped too.
    Raises ``ValueError`` for a string that is not an IP address.
    """
    ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip in _METADATA_ADDRESSES or any(ip in network for network in _METADATA_NETWORKS)


def refuse_metadata_socket(addr_info: tuple[Any, ...]) -> socket.socket:
    """Open a socket for aiohttp, unless its address is a metadata endpoint.

    This is an aiohttp ``socket_factory``.

    The refusal is an ``OSError``, so aiohttp reports it as the
    ``ClientConnectorError`` of any other unreachable host.
    """
    family, type_, proto, _, sockaddr = addr_info
    try:
        refused = is_metadata_address(sockaddr[0])
    except ValueError:
        refused = True
    if refused:
        raise OSError(errno.EACCES, f"connection to cloud metadata address {sockaddr[0]} refused")
    return socket.socket(family=family, type=type_, proto=proto)
