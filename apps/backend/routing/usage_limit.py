"""Detect subscription usage-limit errors and when their limit resets.

Subscription-based upstreams — coding-plan / usage-plan APIs such as Z.AI GLM,
Kimi, MiniMax, or an Anthropic subscription — reject requests once a usage
window is spent, for example::

    {"error": {"code": "1308", "message":
      "Usage limit reached for 5 hour. Your limit will reset at 2026-07-30 22:45:10"}}
    {"error": "you (1a1a11a) have reached your weekly usage limit, upgrade ..."}

These are *expected* exhaustions that only clear when the window resets, so the
circuit breaker uses this module to fire a single "Provider circuit opened"
alert per outage and stay quiet until the reset time (see
``routing.endpoint_health``) rather than re-paging every half-open probe for
hours.

The parser is intentionally conservative. It classifies only clear
"usage limit" phrasing — never a transient per-minute ``rate limit``, which
recovers on its own and must keep alerting normally — and prefers the
provider-declared reset timestamp, falling back to the named window
("weekly", "5 hour", ...) when no timestamp is given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = ["UsageLimit", "detect_usage_limit"]

# A usage-limit error must clearly name a *usage/subscription* limit. "usage
# limit" / "subscription limit" are unambiguous. A bare reset phrase counts only
# when the text is not a transient "rate limit" — a rate limit also "resets", but
# within seconds, and must keep paging normally.
_USAGE_MARKERS = ("usage limit", "subscription limit")
_RESET_MARKERS = ("limit will reset", "limit resets")


def _is_usage_limit(low: str) -> bool:
    """Return whether ``low`` (a lower-cased error) names a subscription usage limit."""
    if any(marker in low for marker in _USAGE_MARKERS):
        return True
    if any(marker in low for marker in _RESET_MARKERS):
        return "rate limit" not in low
    return False


# Suppression-window guards. A parsed reset is clamped into this range so a
# mis-parsed or clock-skewed timestamp can neither thrash (re-alert at once) nor
# mute an endpoint for an unbounded stretch. ``_MAX_SUPPRESS`` comfortably
# covers a weekly window; a monthly limit is capped to it (it re-alerts at most
# a few times per month rather than staying muted on a single parse).
_MIN_SUPPRESS = timedelta(minutes=5)
_MAX_SUPPRESS = timedelta(days=8)
# Used when the text is a usage limit but names neither a reset time nor a window.
_DEFAULT_SUPPRESS = timedelta(hours=1)

# "... reset at 2026-07-30 22:45:10", "resets at 2026-07-30T22:45:10Z",
# "will reset on 2026-07-30 22:45:10+08:00". Captures an ISO-8601-ish timestamp.
_RESET_AT_RE = re.compile(
    r"reset(?:s|ting)?\s+(?:at|on)\s+"
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
    r"(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?)",
    re.IGNORECASE,
)

# A named window ("5 hour", "5-hours", "hourly", "weekly", "per day", ...). The
# leading count is optional; ``\b`` around the unit keeps "day" out of "today".
# ``unit`` drives the window length; ``suffix`` is kept only to render the label.
_WINDOW_RE = re.compile(
    r"(?:(?P<num>\d+(?:\.\d+)?)[\s-]*)?\b(?P<unit>hour|day|week|month)(?P<suffix>ly|s)?\b",
    re.IGNORECASE,
)

_UNIT_LENGTH = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=31),
}


@dataclass(frozen=True)
class UsageLimit:
    """A detected subscription usage-limit error and when it resets.

    Attributes:
        reset_at: Timezone-aware UTC deadline until which repeat circuit-open
            alerts for the endpoint are suppressed. Already clamped to a sane
            window, so callers can use it directly.
        window: Human label for the limit period (``"5 hour"``, ``"weekly"``,
            ``"explicit"``, or ``"unspecified"``) for logs and alert context.
    """

    reset_at: datetime
    window: str


def _parse_reset_timestamp(raw: str) -> datetime | None:
    """Parse a captured ISO-8601-ish reset timestamp to UTC; None on failure.

    The date/time separator is normalized to ``T`` and a trailing ``Z`` to
    ``+00:00`` so :func:`datetime.fromisoformat` accepts the value on Python
    3.10. A naive timestamp (no offset) is assumed to be UTC: providers rarely
    declare a zone, and assuming UTC keeps the suppression window a slight over-
    rather than under-estimate for zones east of UTC.
    """
    text = raw.strip()
    if len(text) > 10 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _estimate_window(detail: str) -> tuple[timedelta, str] | None:
    """Return ``(length, label)`` for the first named usage window, or None."""
    match = _WINDOW_RE.search(detail)
    if match is None:
        return None
    unit = match.group("unit").lower()
    num_raw = match.group("num")
    if num_raw is not None:
        try:
            count = float(num_raw)
        except ValueError:
            count = 0.0
        if count > 0:
            return _UNIT_LENGTH[unit] * count, f"{num_raw} {unit}"
    suffix = match.group("suffix") or ""
    return _UNIT_LENGTH[unit], f"{unit}{suffix.lower()}"


def _clamp(reset_at: datetime, now: datetime) -> datetime:
    """Clamp ``reset_at`` to ``[now + _MIN_SUPPRESS, now + _MAX_SUPPRESS]``."""
    low = now + _MIN_SUPPRESS
    high = now + _MAX_SUPPRESS
    if reset_at < low:
        return low
    if reset_at > high:
        return high
    return reset_at


def detect_usage_limit(detail: str | None, *, now: datetime) -> UsageLimit | None:
    """Classify ``detail`` as a subscription usage-limit error with a reset time.

    Returns ``None`` when the text is not a usage-limit error, so the caller
    alerts normally. Otherwise returns the reset deadline (clamped, UTC) and a
    window label, preferring a provider-declared timestamp over the named
    window over a conservative default.

    Args:
        detail: Operator-facing upstream error text (already secret-scrubbed).
        now: Timezone-aware UTC reference time.
    """
    if not detail:
        return None
    if not _is_usage_limit(detail.lower()):
        return None

    window_est = _estimate_window(detail)

    # A provider-declared explicit reset timestamp wins, but only if it is still
    # in the future — a past time means clock skew or a wrong zone, so fall back.
    reset_at: datetime | None = None
    match = _RESET_AT_RE.search(detail)
    if match is not None:
        parsed = _parse_reset_timestamp(match.group(1))
        if parsed is not None and parsed > now:
            reset_at = parsed

    if reset_at is not None:
        window = window_est[1] if window_est is not None else "explicit"
    elif window_est is not None:
        length, window = window_est
        reset_at = now + length
    else:
        reset_at = now + _DEFAULT_SUPPRESS
        window = "unspecified"

    return UsageLimit(reset_at=_clamp(reset_at, now), window=window)
