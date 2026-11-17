"""Where an administrator may point the gateway: anywhere but cloud metadata, by default.

Admins are trusted to name any upstream: a self-hosted engine (vLLM, SGLang)
on the LAN or on loopback is the ordinary case. What no setting allows is a
cloud instance metadata endpoint (:mod:`serving.utils.metadata_addresses`): no
inference upstream lives there, and it hands out the host's own credentials.

A deployment that keeps its gateway apart from its own network can also take
private and loopback addresses away from the admin console with
``ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS=0``. Then a Base URL an admin saves or
probes, and the model queue status address, must reach a public unicast
address (:func:`serving.utils.ip_classification.is_public_unicast`), checked
when the URL is validated and again for every socket the probe opens, which is
the check that holds when the name later resolves elsewhere.

The switch governs what the console accepts and probes. Inference traffic to a
saved route keeps the metadata-only connect-time guard
(``serving.http``): routes that were saved before the switch was turned off,
and the gateway's own local engines, keep working. Contain the gateway's
egress at the network layer as well when that matters.
"""

from __future__ import annotations

import errno
import socket
from typing import Any, Literal

from serving.config.app_config import config_value
from serving.utils.ip_classification import IPAddress, is_public_unicast
from serving.utils.metadata_addresses import is_metadata_address

#: The switch: on (the default) admins may name private and loopback upstreams.
ALLOW_PRIVATE_KEY = "ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS"

_OFF_VALUES = frozenset({"0", "false", "no", "off"})

Refusal = Literal["invalid", "metadata", "private"]


def private_upstreams_allowed() -> bool:
    """Whether an admin-configured upstream may be a private or loopback address.

    Anything but an explicit off value (``0``, ``false``, ``no``, ``off``) keeps
    the default: a typo must not silently change which upstreams work.
    """
    raw = (config_value(ALLOW_PRIVATE_KEY, "1") or "").strip().lower()
    return raw not in _OFF_VALUES


def upstream_refusal(address: str | IPAddress) -> Refusal | None:
    """Why an admin-configured upstream may not connect to *address*, or None."""
    try:
        if is_metadata_address(address):
            return "metadata"
    except ValueError:
        return "invalid"
    if not private_upstreams_allowed() and not is_public_unicast(address):
        return "private"
    return None


def admin_upstream_socket(addr_info: tuple[Any, ...]) -> socket.socket:
    """An aiohttp ``socket_factory`` for requests to an admin-configured upstream.

    Refuses a metadata address always, and a private or loopback one while
    :func:`private_upstreams_allowed` is off. The refusal is an ``OSError``, so
    aiohttp reports it as the ``ClientConnectorError`` of an unreachable host.
    """
    family, type_, proto, _, sockaddr = addr_info
    refusal = upstream_refusal(str(sockaddr[0]))
    if refusal is not None:
        what = "cloud metadata" if refusal == "metadata" else "non-public"
        raise OSError(errno.EACCES, f"connection to {what} address {sockaddr[0]} refused")
    return socket.socket(family=family, type=type_, proto=proto)
