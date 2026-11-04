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

| | Stored on | Belongs to |
|---|---|---|
| the limit, ``quota_daily_cost_usd`` | each row of ``api_keys`` | **the account** |
| the spend | ``user_daily_cost`` | **user** |

An account may hold several API keys. The limit is the account's, copied onto
every one of its unrevoked keys by the store (``OperationalStore.create_key``),
so the key the direct path authenticates with and the account a grant is found
by read the same number: which key's limit wins is never a question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

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
    current = now or datetime.now(UTC)
    return (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def next_utc_month_start(now: datetime | None = None) -> datetime:
    """Return the first instant of next UTC month — when a monthly counter resets."""
    current = now or datetime.now(UTC)
    year, month = (
        (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    )
    return datetime(year, month, 1, tzinfo=UTC)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """Return whole seconds until the daily counter resets."""
    current = now or datetime.now(UTC)
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


Scope = Literal["key", "account"]
Window = Literal["day", "month"]


@dataclass(frozen=True, slots=True)
class BudgetRefusal:
    """Which limit a request would exceed, and by how much it is already spent.

    ``scope`` names whose limit it is: the presented API key's own budget, or
    the account's quota. ``key_label`` is how a message names the key (its
    name, else its masked prefix); None for the account.
    """

    scope: Scope
    window: Window
    limit_usd: float
    spent_usd: float
    key_prefix: str | None = None
    key_label: str | None = None

    def reset_at(self, now: datetime | None = None) -> datetime:
        """When the window's counter starts again."""
        return next_utc_midnight(now) if self.window == "day" else next_utc_month_start(now)


def _over(limit_usd: float | None, spent_usd: float) -> bool:
    return limit_usd is not None and spent_usd + ESTIMATED_REQUEST_COST_USD > limit_usd


def first_refusal(
    *,
    account_quota_usd: float,
    account_spent_usd: float,
    key_budget_daily_usd: float | None = None,
    key_budget_monthly_usd: float | None = None,
    key_spent_today_usd: float = 0.0,
    key_spent_month_usd: float = 0.0,
    key_prefix: str | None = None,
    key_label: str | None = None,
) -> BudgetRefusal | None:
    """Return the narrowest limit this request does not fit under, or None.

    Checked in order key/day, key/month, account/day, so a refusal names the
    narrowest scope that ran out. Each uses the same optimistic estimate, so a
    key and its account behave alike near their limits. A key budget of None
    is no limit at the key.
    """
    if _over(key_budget_daily_usd, key_spent_today_usd):
        return BudgetRefusal(
            "key", "day", float(key_budget_daily_usd), key_spent_today_usd, key_prefix, key_label
        )
    if _over(key_budget_monthly_usd, key_spent_month_usd):
        return BudgetRefusal(
            "key",
            "month",
            float(key_budget_monthly_usd),
            key_spent_month_usd,
            key_prefix,
            key_label,
        )
    if _over(account_quota_usd, account_spent_usd):
        return BudgetRefusal("account", "day", account_quota_usd, account_spent_usd)
    return None


def refusal_payload(
    refusal: BudgetRefusal, *, contact_email: str = ""
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the 429 body and headers for *refusal*.

    The account's refusal is :func:`exceeded_payload` unchanged plus ``code``,
    ``scope`` and ``window``, so clients reading today's fields keep working.
    A key's refusal has the same fields, names the key, and resets with its
    window.
    """
    if refusal.scope == "account":
        body, headers = exceeded_payload(
            quota_usd=refusal.limit_usd, spent_usd=refusal.spent_usd, contact_email=contact_email
        )
        return {**body, "code": "budget_exceeded", "scope": "account", "window": "day"}, headers
    now = datetime.now(UTC)
    reset_at = refusal.reset_at(now)
    retry_after = max(1, int((reset_at - now).total_seconds()))
    remaining = max(0, refusal.limit_usd - refusal.spent_usd)
    period = "daily" if refusal.window == "day" else "monthly"
    body = {
        "error": f"API key {period} budget exceeded",
        "code": "budget_exceeded",
        "scope": "key",
        "window": refusal.window,
        "key_prefix": refusal.key_prefix,
        "quota_usd": refusal.limit_usd,
        "spent_usd": refusal.spent_usd,
        "remaining_usd": remaining,
        "reset_at": reset_at.isoformat(),
        "contact_email": contact_email,
        "message": (
            f"API key {refusal.key_label or refusal.key_prefix} reached its {period} budget "
            f"of ${refusal.limit_usd:.2f}. Raise the key's budget in the console, or use "
            "another key."
        ),
        "retry_after": retry_after,
    }
    headers = {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit-Cost": str(refusal.limit_usd),
        "X-RateLimit-Remaining-Cost": str(remaining),
        "X-RateLimit-Reset": str(int(reset_at.timestamp())),
    }
    return body, headers
