"""Custom exceptions for business logic.

This module defines domain-specific exceptions that represent business errors.
These exceptions are caught by global exception handlers and converted to
appropriate HTTP responses with consistent error codes.
"""

from __future__ import annotations

import json
import re

import aiohttp


class HybridInferenceError(Exception):
    """Base exception for all business errors."""

    pass


class UserFacingError(HybridInferenceError):
    """Marker base for exceptions whose message is safe to surface verbatim.

    Subclasses' str(exc) is passed through scrub_error_for_user unchanged
    (with a request_id suffix appended).
    """

    pass


# Authentication errors
class AuthenticationError(UserFacingError):
    """Authentication related errors."""

    pass


class UserAlreadyExistsError(AuthenticationError):
    """User with email already exists."""

    def __init__(self, email: str):
        self.email = email
        super().__init__(f"Email {email} already registered")


class WeakPasswordError(AuthenticationError):
    """Password doesn't meet security requirements."""

    pass


class InvalidCredentialsError(AuthenticationError):
    """Invalid email or password."""

    pass


class EmailNotVerifiedError(AuthenticationError):
    """Email not verified."""

    pass


class AccountSuspendedError(AuthenticationError):
    """Account is suspended."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Account is {status}")


# User management errors
class UserNotFoundError(UserFacingError):
    """User not found."""

    pass


class DuplicateAPIKeyError(UserFacingError):
    """User already has an active API key."""

    pass


class APIKeyNotFoundError(UserFacingError):
    """API key not found."""

    pass


# Token errors
class TokenExpiredError(AuthenticationError):
    """Token has expired."""

    pass


class InvalidTokenError(AuthenticationError):
    """Invalid token."""

    pass


class TokenAlreadyUsedError(AuthenticationError):
    """Token has already been used."""

    pass


# Session errors
class SessionNotFoundError(AuthenticationError):
    """Session not found."""

    pass


class SessionRevokedError(AuthenticationError):
    """Session has been revoked."""

    pass


# Quota errors
class QuotaExceededError(UserFacingError):
    """User has exceeded their quota."""

    def __init__(self, quota: float, spent: float):
        self.quota = quota
        self.spent = spent
        super().__init__(f"Quota exceeded: ${spent:.2f} / ${quota:.2f}")


# ----------------------------------------------------------------------
# User-facing error scrubbing
# ----------------------------------------------------------------------

_GENERIC_MESSAGES_BY_STATUS: dict[int, str] = {
    400: "Invalid request",
    401: "Authentication failed",
    403: "Authentication failed",
    422: "Invalid request",
    429: "Rate limit exceeded",
}

# Vendor/provider identity tokens that must never appear in user-facing output.
# The human-readable upstream *message* is surfaced, but the identity of the
# upstream provider (name, host, URL) is scrubbed out.
_PROVIDER_NAME_TOKENS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "openrouter",
    "deepseek",
    "minimax",
    "sglang",
    "vllm",
    "ollama",
    "gemini",
    "google",
    "googleapis",
    "zhipu",
    "zai",
    "glm",
    "claude",
    "mistral",
    "groq",
    "together",
    "fireworks",
    "qwen",
    "moonshot",
    "kimi",
    "perplexity",
    "xai",
    "grok",
)

# Full URLs, e.g. https://api.anthropic.com/v1/messages
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# aiohttp's ClientResponseError renders the request URL as ", url='https://...'"
# (and occasionally ", url=URL('https://...')"). Drop the whole segment.
_AIOHTTP_URL_RE = re.compile(r",?\s*url=(?:URL\()?['\"]?[^'\"\s)]+['\"]?\)?", re.IGNORECASE)
# Bare hostnames (no scheme), restricted to common TLDs to avoid mangling
# model names / file references that merely contain a dot.
_HOSTNAME_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|ai|org|net|io|co|dev|app|cloud|gov|cn|us|me|xyz)\b",
    re.IGNORECASE,
)
_PROVIDER_NAME_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(t) for t in _PROVIDER_NAME_TOKENS) + r")\b"
)
_SECRET_RE = re.compile(
    r'(?i)("?(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|secret|token|authorization)"?'
    r'\s*[:=]\s*)("?)[^"\s,}]+("?)'
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
# Provider API-key token shapes, redacted by value regardless of the surrounding
# phrasing. Upstream 401 bodies commonly echo the key without an ``api_key=``
# separator, e.g. OpenAI's ``Incorrect API key provided: sk-...`` — which the
# assignment-based _SECRET_RE above does not catch.
_API_KEY_TOKEN_RE = re.compile(
    # OpenAI / Anthropic / DeepSeek / OpenRouter (sk-…, sk-ant-…, sk-proj-…),
    # Groq (gsk_…), xAI (xai-…), Stripe-style restricted keys (rk_…).
    r"(?i)\b(?:sk|gsk|xai|rk)[-_][a-z0-9._-]{6,}"
    # Google / Gemini API keys (AIza…).
    r"|\bAIza[0-9A-Za-z_-]{10,}"
)

# Upstream messages that reveal the operator's own upstream account has hit a
# quota, balance, billing, or credit limit. Surfacing these verbatim would tell
# end users that *our* provider account is out of quota/funds — an internal
# operational detail. When an upstream message matches, the user-facing path
# suppresses it in favour of a generic status-based message; operators still get
# the full text via ``operator_safe_error`` / ``api_logs.error``.
# Plural forms are enumerated explicitly (rather than via a regex ``s?`` suffix,
# which produces malformed patterns on multi-word markers). ``billing`` is kept
# specific ("billing hard limit" / "billing limit") so ordinary billing-related
# messages ("Invalid billing address") are not suppressed; ``quota`` is left bare
# on purpose — providers phrase exhaustion many ways ("exceeded your current
# quota", "monthly quota depleted") and missing one would leak the very fact we
# must hide, whereas an occasional false positive only yields a safe generic
# message.
_UPSTREAM_QUOTA_MARKERS: tuple[str, ...] = (
    "quota",
    "quotas",
    "insufficient balance",
    "insufficient balances",
    "insufficient funds",
    "insufficient credit",
    "insufficient credits",
    "not enough balance",
    "balance is too low",
    "account balance",
    "account balances",
    "out of credit",
    "out of credits",
    "no credit",
    "no credits",
    "credit balance",
    "credit balances",
    "billing hard limit",
    "billing limit",
    "payment required",
    "add funds",
    "top up",
    "recharge",
    "arrearage",
    "spending limit",
    "spending limits",
    "usage limit",
    "usage limits",
    "purchase more",
)
# Word-boundary anchored so "quota" does not match "quotation".
_UPSTREAM_QUOTA_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(m) for m in _UPSTREAM_QUOTA_MARKERS) + r")\b"
)


def _reveals_upstream_quota(text: str) -> bool:
    """Return True if ``text`` exposes an upstream quota/balance/billing limit."""
    # ``_`` is a regex word character (and multi-word markers are space-separated),
    # so the ``\b`` anchored markers never match across machine-style tokens like
    # ``insufficient_quota`` / ``payment-required`` / ``billing_hard_limit_reached``.
    # Fold ``_`` and ``-`` separators to spaces for the detection pass (the
    # surfaced message itself is left untouched).
    normalized = text.replace("_", " ").replace("-", " ")
    return bool(_UPSTREAM_QUOTA_RE.search(normalized))


# Upstream error string shapes we know how to unwrap into a bare message.
_UPSTREAM_BODY_MARKER = "upstream_body="
_AIOHTTP_MESSAGE_RE = re.compile(r"message=(['\"])(?P<msg>.*?)\1(?:,\s*url=|$)", re.DOTALL)
_CLAUDE_WRAPPER_PREFIX = "Upstream API error:"


def _message_from_json(text: str) -> str | None:
    """Pull the human-readable message out of a JSON error body, if present.

    Handles the common OpenAI/Anthropic-style shapes:
    ``{"error": {"message": ...}}``, ``{"error": "..."}``, ``{"message": ...}``.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "msg", "detail"):
            val = err.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(err, str) and err.strip():
        return err.strip()
    for key in ("message", "detail", "msg"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_upstream_message(raw: str) -> str:
    """Reduce a raw/wrapped upstream error string to its human-readable message.

    Unwraps the shapes we persist or raise internally:
      * streaming DB format: ``"<exc> | upstream_body=<body>"``
      * aiohttp ``ClientResponseError`` str: ``"<status>, message='<body>', url='<url>'"``
      * claude adapter wrapper: ``"Upstream API error: <msg>"``
      * JSON bodies: ``{"error": {"message": "..."}}`` and friends

    Falls back to the input text when nothing more specific is found.
    """
    text = raw.strip()

    if _UPSTREAM_BODY_MARKER in text:
        text = text.split(_UPSTREAM_BODY_MARKER, 1)[1].strip()

    match = _AIOHTTP_MESSAGE_RE.search(text)
    if match:
        text = match.group("msg").strip()

    if text.startswith(_CLAUDE_WRAPPER_PREFIX):
        text = text[len(_CLAUDE_WRAPPER_PREFIX) :].strip()

    json_msg = _message_from_json(text)
    if json_msg:
        text = json_msg

    return text.strip()


def scrub_provider_identity(text: str) -> str:
    """Strip provider identity (URLs, hostnames, vendor names) and secrets.

    Leaves the human-readable substance of the message intact.
    """
    text = _AIOHTTP_URL_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _HOSTNAME_RE.sub("", text)
    text = _PROVIDER_NAME_RE.sub("", text)
    text = _SECRET_RE.sub(r"\1\2[REDACTED]\3", text)
    text = _API_KEY_TOKEN_RE.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    # Tidy up artefacts left behind by the removals above.
    text = re.sub(r"\s+([.,:;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" \t\n\r,:;-")


def user_safe_upstream_error(raw: str | None, *, max_len: int = 500) -> str | None:
    """Return the upstream provider's message, with provider identity removed.

    Returns ``None`` when there is no usable message after scrubbing, or when the
    message would reveal that the operator's own upstream account has hit a
    quota/balance/billing limit, so callers can fall back to a generic
    status-based message.
    """
    if not raw:
        return None
    extracted = _extract_upstream_message(raw)
    # Never reveal that the operator's upstream account has hit a quota/balance
    # limit — fall back to the caller's generic status-based message instead.
    if _reveals_upstream_quota(extracted):
        return None
    msg = scrub_provider_identity(extracted)
    if not msg:
        return None
    if len(msg) > max_len:
        msg = msg[: max_len - 1].rstrip() + "…"
    return msg


def _upstream_error_raw(exc: BaseException | None) -> str | None:
    """Return the raw upstream error text if ``exc`` is an upstream API error.

    Internal/unexpected exceptions return ``None`` so they keep the generic,
    status-based message (we never surface internal error text to users).
    """
    if exc is None:
        return None
    body = getattr(exc, "error_body", None)
    if body:
        return body if isinstance(body, str) else str(body)
    if isinstance(exc, aiohttp.ClientResponseError):
        # Use the message (reason or upstream body) rather than str(exc): the
        # latter embeds the request URL and can also raise when request_info is
        # absent. The status code is surfaced separately by the caller.
        message = getattr(exc, "message", None)
        return str(message) if message else None
    return None


def generic_message_for_status(status_code: int) -> str:
    """Return the generic, provider-free message for an HTTP status code."""
    if status_code in _GENERIC_MESSAGES_BY_STATUS:
        return _GENERIC_MESSAGES_BY_STATUS[status_code]
    if 500 <= status_code < 600:
        return "Internal server error"
    return "Request failed"


def user_safe_error_for_log(raw: str | None, status_code: int | None) -> str | None:
    """Return user-safe error text for a stored ``api_logs`` row.

    Used by log-display surfaces (e.g. the ``/user/recent-requests`` dashboard)
    that read the persisted operator error string rather than a live exception.
    Returns ``None`` when the row carries no error. Otherwise surfaces the
    scrubbed upstream message, falling back to a generic status-based message
    when the message is empty or would reveal an upstream quota/balance limit —
    so a failed row never renders with blank error text.
    """
    if not raw or not raw.strip():
        return None
    msg = user_safe_upstream_error(raw)
    if msg:
        return msg
    return generic_message_for_status(status_code) if status_code is not None else "Request failed"


def scrub_error_for_user(
    exc: BaseException | None,
    request_id: str | None,
    status_code: int,
) -> str:
    """Return a user-safe error message that hides provider identity.

    Behavior:
      * ``UserFacingError`` subclasses (our own, provider-free) pass through.
      * Genuine upstream API errors surface the upstream provider's
        human-readable message with provider identity (name, host, URL) and
        secrets scrubbed out.
      * Everything else falls back to a generic, status-code-appropriate
        message.

    Callers are responsible for persisting the full operator-facing error to
    ``api_logs.error`` keyed by the same ``request_id``.
    """
    if isinstance(exc, UserFacingError):
        base = str(exc)
    else:
        base = user_safe_upstream_error(_upstream_error_raw(exc)) or ""
        if not base:
            base = generic_message_for_status(status_code)

    if request_id:
        return f"{base} (request_id: {request_id})"
    return base


def operator_safe_error(exc: BaseException | None, *, max_len: int = 500) -> str | None:
    """Return operator-facing error text with secrets and provider URLs removed.

    Intended for internal operator surfaces such as Slack alerts. Unlike
    :func:`scrub_error_for_user`, this keeps the raw error text for *any*
    exception (not just recognized upstream API errors) so alerts stay
    actionable, but it still runs the provider-identity scrubber so secrets
    can never leak.

    The leak this guards against: ``aiohttp.ClientResponseError`` renders the
    request URL in ``str(exc)``, and some adapters embed the API key in the
    URL (e.g. Gemini's ``?key=<api_key>``). We therefore prefer the safe
    extraction path (``error_body`` / aiohttp ``message``) over ``str(exc)``
    and scrub URLs/secrets from whatever we surface.

    Returns ``None`` when there is no usable text after scrubbing.
    """
    if exc is None:
        return None
    raw = _upstream_error_raw(exc)
    if not raw:
        # Non-upstream exception (timeout, connection error, ValueError, ...).
        # str() can raise on a malformed exception, so guard it.
        try:
            raw = str(exc)
        except Exception:
            raw = exc.__class__.__name__
    # Bound before scrubbing: error_body can hold a full upstream response body
    # (e.g. a large HTML 5xx page), and running every regex substitution over
    # it on each failed attempt is wasted work since only ``max_len`` chars are
    # ever surfaced. A margin above ``max_len`` keeps scrubbing context intact.
    if len(raw) > max_len * 4:
        raw = raw[: max_len * 4]
    cleaned = scrub_provider_identity(raw)
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned
