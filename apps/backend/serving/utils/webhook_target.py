"""Validate a caller-supplied webhook target before the gateway posts to it.

A batch's webhook URL comes from the caller, so posting to it makes the gateway
issue an outbound request on that caller's behalf. Requiring HTTPS and a public
final address keeps that from reaching loopback, private, link-local or
cloud-metadata targets. Resolution runs on every attempt: a host that was public
when the batch was created may answer with a private address later.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

#: Longest URL accepted, matching the bound the admin alert webhooks use.
MAX_WEBHOOK_URL_LENGTH = 4096

_DEFAULT_HTTPS_PORT = 443


class UnsafeWebhookTarget(ValueError):
    """A webhook URL the gateway refuses to post to."""


def webhook_url_problem(url: str) -> str | None:
    """Return why *url* cannot be a webhook target, or None when its shape is usable.

    Only the transport shape is checked here; whether the host resolves to a
    public address is decided when a delivery is attempted.
    """
    if not url:
        return "must not be empty"
    if len(url) > MAX_WEBHOOK_URL_LENGTH:
        return f"must be at most {MAX_WEBHOOK_URL_LENGTH} characters"
    if "#" in url:
        return "must not have a fragment"
    if any(char.isspace() or ord(char) < 32 for char in url):
        return "must not contain whitespace or control characters"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "is not a valid URL"
    if parts.scheme != "https":
        return "must use https"
    if not parts.netloc:
        return "must include a host"
    if parts.username is not None or parts.password is not None:
        return "must not include credentials"
    if parts.hostname is None:
        return "must include a host"
    try:
        port = parts.port
    except ValueError:
        return "has an invalid port"
    if port is not None and not 1 <= port <= 65535:
        return "has an invalid port"
    return None


async def resolve_public_webhook(url: str) -> None:
    """Raise :class:`UnsafeWebhookTarget` unless *url* resolves only to public addresses.

    Every address a hostname resolves to is checked, because the HTTP client may
    connect to any of them. A resolution failure is refused rather than retried
    as a network error: a host the deployment cannot resolve is not a target.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        raise UnsafeWebhookTarget("has no host")
    port = parts.port or _DEFAULT_HTTPS_PORT
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeWebhookTarget(f"host {host!r} could not be resolved") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeWebhookTarget(f"host {host!r} resolved to no address")
    for raw in addresses:
        _require_public_address(raw, host)


def _require_public_address(raw: str, host: str) -> None:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeWebhookTarget(f"host {host!r} resolved to an invalid address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global:
        raise UnsafeWebhookTarget(f"host {host!r} resolves to a non-public address")
