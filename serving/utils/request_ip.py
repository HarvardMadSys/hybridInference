"""Helpers for extracting a stable client IP from proxied requests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


def _header_value(value: object) -> str | None:
    """Return a stripped header value when the request provides a real string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request."""
    if os.getenv("TRUST_PROXY_HEADERS", "0") == "1":
        x_forwarded_for = _header_value(request.headers.get("x-forwarded-for"))
        if x_forwarded_for:
            first_ip = x_forwarded_for.split(",", 1)[0].strip()
            if first_ip:
                return first_ip

        x_real_ip = _header_value(request.headers.get("x-real-ip"))
        if x_real_ip:
            return x_real_ip

    return request.client.host if request.client else "unknown"
