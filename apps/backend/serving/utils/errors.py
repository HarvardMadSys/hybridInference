"""Small, low-cardinality error categorization helpers."""

from __future__ import annotations

import re

_CATEGORIES: dict[str, tuple[str, ...]] = {
    "timeout": ("TimeoutError", "ReadTimeout", "WriteTimeout", "timed out"),
    "rate_limited": ("429", "RateLimit", "TooManyRequests"),
    "network": ("ConnectionError", "ConnectError", "DNSError", "Connection reset"),
    "server_error": ("500", "502", "503", "504", "InternalServerError"),
    "validation": ("ValidationError",),
}


def categorize_exception(exc: BaseException) -> str:
    """Map an exception to a coarse error category.

    Returns one of: timeout|rate_limited|network|server_error|validation|unknown
    """
    s = f"{type(exc).__name__}: {exc}"
    ls = s.lower()
    for cat, needles in _CATEGORIES.items():
        for needle in needles:
            if needle.lower() in ls:
                return cat
    return "unknown"


_SECRET_RE = re.compile(
    r'(?i)("?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization)"?'
    r'\s*[:=]\s*)("?)[^"\s,}]+("?)'
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")


def format_exception_for_db(exc: BaseException, max_len: int = 4000) -> str:
    """Return capped, secret-redacted operator-facing error text for api_logs.error.

    Operators keep full provider context (status, message, URL, upstream body);
    only credentials are redacted. The user-facing endpoints scrub provider
    identity separately at read time. The upstream response body, when present,
    is appended so the actual provider error survives into the log.

    Falls back to the exception class name when ``str(exc)`` is empty, so
    message-less exceptions (notably ``asyncio.CancelledError`` and
    ``GeneratorExit`` from a request timeout or client disconnect) still record
    an identifiable error rather than a blank string.
    """
    exc_text = str(exc) or type(exc).__name__
    upstream_body = getattr(exc, "error_body", None)
    if upstream_body is None:
        value = exc_text
    else:
        body_text = upstream_body if isinstance(upstream_body, str) else str(upstream_body)
        value = f"{exc_text} | upstream_body={body_text}"

    value = _SECRET_RE.sub(r"\1\2[REDACTED]\3", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    if len(value) > max_len:
        return value[: max_len - 14] + "...[truncated]"
    return value


__all__ = ["categorize_exception", "format_exception_for_db"]
