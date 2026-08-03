"""The daily cost quota, in one place, for every door into inference.

There are two now. A user's API key comes through ``verify_api_key``; a cloud
agent's inference grant comes through ``model_auth``. Both spend the same
account's money against the same limit, so both must answer identically when it
runs out — a client that could tell which door it came through would be reading
a difference that is not supposed to exist.

That is the whole reason this module exists rather than the gate being inlined
twice. Two copies of a 429 body drift: one grows a header, the other keeps an
older ``retry_after``, and the divergence is invisible until someone diffs two
incident reports.

**Where the two halves live** is the awkward part, and it is a property of the
schema rather than a choice made here:

| | Stored on | Keyed by |
|---|---|---|
| the limit, ``quota_daily_cost_usd`` | ``api_keys`` | **API key** |
| the spend | ``user_daily_cost`` | **user** |

A grant has no API key, so the limit has to be found by user instead. The schema
guarantees at most one active key per user, which makes that lookup a single row
rather than a policy question about which key's limit wins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

#: What an old row with no configured quota means. Matches ``verify_api_key``
#: exactly; diverging would make the two doors disagree about the same account.
DEFAULT_DAILY_QUOTA_USD = 1000.0

#: Charged optimistically before the call, since the real cost is not known
#: until it returns. Same value the direct path uses.
ESTIMATED_REQUEST_COST_USD = 0.01


class QuotaExceeded(Exception):
    """The account's daily cost quota is spent.

    Carries the numbers rather than a rendered message so each caller can raise
    it in its own framework's idiom while the body stays identical.
    """

    def __init__(self, *, quota_usd: float, spent_usd: float) -> None:
        super().__init__("Daily cost quota exceeded")
        self.quota_usd = quota_usd
        self.spent_usd = spent_usd


def next_utc_midnight(now: datetime | None = None) -> datetime:
    """Return the next UTC midnight — when the daily counter resets."""
    current = now or datetime.now(timezone.utc)
    return (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """Return whole seconds until the daily counter resets."""
    current = now or datetime.now(timezone.utc)
    return max(1, int((next_utc_midnight(current) - current).total_seconds()))


def resolve_quota(raw: Any) -> float:
    """Return the effective daily limit for a raw column value.

    ``NULL`` keeps meaning :data:`DEFAULT_DAILY_QUOTA_USD`, as it does on the
    direct path — old rows predate the column.
    """
    return DEFAULT_DAILY_QUOTA_USD if raw is None else float(raw)


def check(*, quota_usd: float, spent_usd: float) -> None:
    """Raise if this request would take the account past its daily limit.

    Raises:
        QuotaExceeded: When the estimated charge does not fit under the limit.
    """
    if spent_usd + ESTIMATED_REQUEST_COST_USD > quota_usd:
        raise QuotaExceeded(quota_usd=quota_usd, spent_usd=spent_usd)


def exceeded_payload(
    *, quota_usd: float, spent_usd: float, contact_email: str = ""
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the 429 body and headers both doors return.

    Args:
        quota_usd: The account's daily limit.
        spent_usd: What it has already spent today.
        contact_email: Where to ask for more, if the deployment publishes one.

    Returns:
        The response body and the headers to send with it.
    """
    reset_at = next_utc_midnight()
    retry_after = seconds_until_utc_midnight()
    remaining = max(0, quota_usd - spent_usd)
    body = {
        "error": "Daily cost quota exceeded",
        "quota_usd": quota_usd,
        "spent_usd": spent_usd,
        "remaining_usd": remaining,
        "reset_at": reset_at.isoformat(),
        "contact_email": contact_email,
        "message": (
            f"Need more quota? Email {contact_email} and explain your use case."
            if contact_email
            else "Daily quota exhausted. Contact the operator of this deployment."
        ),
        "retry_after": retry_after,
    }
    headers = {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit-Cost": str(quota_usd),
        "X-RateLimit-Remaining-Cost": str(remaining),
        "X-RateLimit-Reset": str(int(reset_at.timestamp())),
    }
    return body, headers
