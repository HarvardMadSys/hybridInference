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

Both checks judge an address by every host it reaches
(:func:`serving.utils.ip_classification.reachable_addresses`): an IPv6 address
that carries ``169.254.169.254`` -- IPv4-compatible, NAT64, 6to4 and the rest --
is a metadata address too, while any other IPv6 address, public or private, is
left alone.
"""

from __future__ import annotations

import errno
import ipaddress
import socket
from typing import Any

from serving.utils.ip_classification import IPAddress, reachable_addresses

#: Names that resolve to a metadata endpoint from inside the cloud that serves
#: it. The connect-time check refuses whatever they resolve to anyway; refusing
#: the name too gives a clear error before any lookup.
METADATA_HOSTNAMES = frozenset(
    {
        "metadata",  # GCP's short name, through the instance's search domain
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",  # AWS EC2
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",  # Tencent Cloud
    }
)

_METADATA_NETWORKS = (
    # Link-local: the AWS, GCP, Azure and Tencent Cloud metadata endpoints, and
    # the AWS ECS / EKS Pod Identity credential agents (169.254.170.2 / .23).
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # AWS IPv6: instance metadata at ::254, EKS Pod Identity at ::23.
    ipaddress.ip_network("fd00:ec2::/64"),
)
_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("100.100.100.200")})  # Alibaba Cloud


def is_metadata_hostname(hostname: str) -> bool:
    """Whether *hostname*, as a URL names it, is a metadata endpoint's name."""
    return hostname.strip().rstrip(".").lower() in METADATA_HOSTNAMES


def is_metadata_address(address: str | IPAddress) -> bool:
    """Whether ``address`` reaches a cloud metadata endpoint, in any spelling.

    An IPv6 zone (``fd00:ec2::254%0``) is dropped first, and every IPv4 address
    an IPv6 address carries is judged as well as the address itself
    (:func:`~serving.utils.ip_classification.reachable_addresses`).
    Raises ``ValueError`` for a string that is not an IP address.
    """
    return any(
        ip in _METADATA_ADDRESSES or any(ip in network for network in _METADATA_NETWORKS)
        for ip in reachable_addresses(address)
    )


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
