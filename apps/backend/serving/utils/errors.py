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


# The value group deliberately consumes an optional ``Bearer `` prefix. Without
# it, ``Authorization: Bearer <token>`` redacted only the word "Bearer" -- the
# value pattern stops at whitespace -- and left the credential itself in the
# text, where it was written to api_logs.error verbatim. _BEARER_RE could not
# recover it either, because the word it keys on had just been replaced.
_SECRET_RE = re.compile(
    r'(?i)("?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization)"?'
    r'\s*[:=]\s*)("?)(?:bearer\s+)?[^"\s,}]+("?)'
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")


def format_exception_for_db(exc: BaseException, max_len: int = 4000) -> str:
    """Return capped, secret-redacted operator-facing error text for api_logs.error.

    Operators keep full provider context (status, message, URL, upstream body);
    only credentials are redacted. The user-facing endpoints scrub provider
    identity separately at read time. The upstream response body, when present,
    is appended so the actual provider error survives into the log.

    The exception class name is always prefixed. It used to appear only when
    ``str(exc)`` was empty, so a message-less exception stayed identifiable but
    an unattributable one did not: an ``IndexError`` from inside the router
    recorded the bare string "list index out of range" -- no type, no module, no
    frame -- and an operator grepping production for it found nothing. Rows
    written directly by a handler (the ``Model '<id>' not found`` 404s, which
    never pass through here) keep their exact text, so the alerter's exclusion
    patterns are unaffected.
    """
    exc_text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
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
