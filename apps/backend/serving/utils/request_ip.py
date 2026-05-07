"""Helpers for extracting a stable client IP from proxied requests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


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


def get_client_ip_info(request: Request) -> ClientIpInfo:
    """Return the originating client IP and the socket/proxy peer that supplied it."""
    peer_ip = request.client.host if request.client else "unknown"
    trusted = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
    x_forwarded_for = _header_value(request.headers.get("x-forwarded-for"))
    x_real_ip = _header_value(request.headers.get("x-real-ip"))

    if trusted:
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
                )

        if x_real_ip:
            return ClientIpInfo(
                client_ip=x_real_ip,
                peer_ip=peer_ip,
                source="x-real-ip",
                trusted_proxy_headers=True,
                x_forwarded_for=x_forwarded_for,
                x_real_ip=x_real_ip,
            )

    return ClientIpInfo(
        client_ip=peer_ip,
        peer_ip=peer_ip,
        source="socket",
        trusted_proxy_headers=trusted,
        x_forwarded_for=x_forwarded_for,
        x_real_ip=x_real_ip,
    )


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request."""
    return get_client_ip_info(request).client_ip
