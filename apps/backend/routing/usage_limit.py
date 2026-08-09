"""Detect subscription usage-limit errors and when their limit resets.

Subscription-based upstreams — coding-plan / usage-plan APIs such as Z.AI GLM,
Kimi, MiniMax, or an Anthropic subscription — reject requests once a usage
window is spent, for example::

    {"error": {"code": "1308", "message":
      "Usage limit reached for 5 hour. Your limit will reset at 2026-07-30 22:45:10"}}
    {"error": "you (1a1a11a) have reached your weekly usage limit, upgrade ..."}

These are *expected* exhaustions that only clear when the window resets, so the
circuit breaker uses this module to fire a single "Provider circuit opened"
alert per outage and stay quiet until the reset time — or for ``MIN_ALERT_GAP``,
whichever is longer (see ``routing.endpoint_health``) — rather than re-paging
every half-open probe for hours.

The parser is intentionally conservative. It classifies only clear
"usage limit" phrasing — never a transient per-minute ``rate limit``, which
recovers on its own and must keep alerting normally — and prefers the
provider-declared reset timestamp, falling back to the named window
("weekly", "5 hour", ...) when no timestamp is given.

A provider that gives neither — MiniMax answers a bare "Token Plan usage limit
reached: Upgrade your Token Plan or purchase Credits for more usage." — used to
fall back to a one-hour guess and so re-paged about five times across its actual
(5-hour) window. What the upstream *claims* and how long we stay quiet are
therefore two separate values now: every suppression deadline is floored to
``MIN_ALERT_GAP``, while ``UsageLimit.reset_at`` still reports the provider's own
claim unfloored, or None when it made none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = ["MIN_ALERT_GAP", "UsageLimit", "detect_usage_limit"]

# A usage-limit error must clearly name a *usage/subscription* limit. "usage
# limit" / "subscription limit" are unambiguous. A bare reset phrase counts only
# when the text is not a transient "rate limit" — a rate limit also "resets", but
# within seconds, and must keep paging normally.
_USAGE_MARKERS = ("usage limit", "subscription limit")
_RESET_MARKERS = ("limit will reset", "limit resets")

# Collapse whitespace/underscore/hyphen runs so "usage-limit", "usage_limit" and
# "rate-limit" read like their space-separated spellings before marker matching.
_SEPARATORS_RE = re.compile(r"[\s_-]+")
# Matches every common transient rate-limit spelling: "rate limit", "rate-limit",
# "rate_limit" (via separator collapse), and "ratelimit" (optional space).
_RATE_LIMIT_RE = re.compile(r"rate ?limit")


def _is_usage_limit(low: str) -> bool:
    """Return whether ``low`` (a lower-cased error) names a subscription usage limit."""
    normalized = _SEPARATORS_RE.sub(" ", low)
    if any(marker in normalized for marker in _USAGE_MARKERS):
        return True
    if any(marker in normalized for marker in _RESET_MARKERS):
        # A transient rate limit also "resets" — in seconds — and must keep
        # paging; exclude it in any spelling.
        return _RATE_LIMIT_RE.search(normalized) is None
    return False


# Floor on every suppression deadline, and so the minimum spacing between an
# endpoint's plan-usage pages. A spent subscription window is expected and stays
# spent: one page tells an operator the model is degraded, and repeating it
# before the window turns over adds nothing they can act on. Four hours is the
# operator-set cadence — long enough that a provider naming no window cannot page
# hourly for the rest of its window, short enough that a limit which really does
# reset sooner is still reported several times a day.
MIN_ALERT_GAP = timedelta(hours=4)

# Ceiling on the suppression deadline, so a mis-parsed or clock-skewed timestamp
# cannot mute an endpoint for an unbounded stretch. Comfortably covers a weekly
# window; a monthly limit is capped to it (it re-alerts at most a few times per
# month rather than staying muted on a single parse).
_MAX_SUPPRESS = timedelta(days=8)

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
    """A detected subscription usage-limit error, when it resets, and how long to stay quiet.

    Attributes:
        suppress_until: Timezone-aware UTC deadline until which repeat
            circuit-open alerts for the endpoint are suppressed. Always at least
            ``MIN_ALERT_GAP`` out and never beyond ``_MAX_SUPPRESS``, so callers
            can use it directly.
        window: Human label for the limit period (``"5 hour"``, ``"weekly"``,
            ``"explicit"``, or ``"unspecified"``) for logs and alert context.
        reset_at: When the provider said — or its named window implies — the limit
            turns over, or None when the error named neither. Reported to
            operators as the upstream's own claim, so it is deliberately *not*
            floored to ``MIN_ALERT_GAP``: a card that reads "resets in 30 min"
            while we stay quiet for four hours is telling the truth about the
            provider, and ``suppress_until`` says the rest.
    """

    suppress_until: datetime
    window: str
    reset_at: datetime | None = None


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


def _suppression_deadline(reset_at: datetime | None, now: datetime) -> datetime:
    """Return when repeat alerts may resume: ``reset_at``, floored and capped.

    Floored to ``now + MIN_ALERT_GAP`` so a limit that resets in minutes — or one
    that names no reset at all — still cannot page more often than the operator
    cadence, and capped to ``now + _MAX_SUPPRESS`` so a mis-parsed or
    clock-skewed timestamp cannot mute an endpoint indefinitely.
    """
    floor = now + MIN_ALERT_GAP
    if reset_at is None or reset_at < floor:
        return floor
    ceiling = now + _MAX_SUPPRESS
    return ceiling if reset_at > ceiling else reset_at


def detect_usage_limit(detail: str | None, *, now: datetime) -> UsageLimit | None:
    """Classify ``detail`` as a subscription usage-limit error with a reset time.

    Returns ``None`` when the text is not a usage-limit error, so the caller
    alerts normally. Otherwise returns how long to stay quiet
    (``suppress_until``, floored and capped, UTC), a window label, and the
    provider's own reset claim (``reset_at``, or None when it made none),
    preferring a declared timestamp over the named window.

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
        # Neither a timestamp nor a window: the limit is real but we cannot say
        # when it turns over, so claim nothing and let the floor decide.
        window = "unspecified"

    return UsageLimit(
        suppress_until=_suppression_deadline(reset_at, now),
        window=window,
        reset_at=reset_at,
    )
