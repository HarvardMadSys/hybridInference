"""What an admin is told when a request to an upstream they named fails.

Admin probes reach whatever an administrator typed: a provider being added
(``POST /admin/provider-definitions/verify``) or a route being saved
(``_verify_provider_route``). A failed answer can therefore come from a service
that is not an inference API at all -- an internal dashboard, a metadata-like
agent, a proxy's error page -- and the client library's connection errors name
the resolved address and the operating system's error. Neither is shown: only
the message an API puts in its error envelope, and the kind of failure.
"""

from __future__ import annotations

import errno
import json
from typing import Any

import aiohttp

#: Longest quoted message.
MAX_MESSAGE_CHARS = 160
#: How much of a failed answer is read to look for an error message.
MAX_ERROR_BODY_BYTES = 16 * 1024


def one_line(value: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """*value* on one line, without NULs, at most *limit* characters."""
    text = " ".join(value.replace("\x00", "").split())
    return text[:limit] if text else ""


def error_message(raw: bytes | str | None, *, secret: str | None = None) -> str:
    """The message of an API's error answer, or "" for any other body.

    Recognized: ``{"error": {"message": ...}}`` (OpenAI, Anthropic, Gemini),
    ``{"error": "..."}`` (Ollama) and vLLM's older ``{"object": "error",
    "message": ...}``. *secret*, the key the request carried, is masked should
    the upstream echo it.
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    message: Any = None
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    elif body.get("object") == "error":
        message = body.get("message")
    if not isinstance(message, str):
        return ""
    if secret:
        message = message.replace(secret, "***")
    return one_line(message)


def connection_failure(exc: aiohttp.ClientError, *, prefix: str) -> str:
    """What a request that could not complete says, without the client library's text."""
    if isinstance(exc, aiohttp.ClientConnectorCertificateError | aiohttp.ClientSSLError):
        return f"{prefix}: the provider's TLS certificate could not be verified."
    if isinstance(exc, aiohttp.ClientConnectorError):
        if getattr(exc.os_error, "errno", None) == errno.EACCES:
            return (
                f"{prefix}: this deployment does not allow connections to the provider's address."
            )
        return f"{prefix}: could not connect to the provider."
    return f"{prefix} ({type(exc).__name__})."
