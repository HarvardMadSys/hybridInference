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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi.encoders import jsonable_encoder

from serving.accounting import get_accounting_policy
from serving.accounting_api import monetary_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    policy = get_accounting_policy()
    check_amount(quota_amount=policy.from_usd(quota_usd), spent_amount=policy.from_usd(spent_usd))


def check_amount(*, quota_amount: Any, spent_amount: Any) -> None:
    """Apply the quota in the ledger currency; retain true USD error aliases."""
    policy = get_accounting_policy()
    if spent_amount + policy.from_usd(ESTIMATED_REQUEST_COST_USD) > quota_amount:
        raise QuotaExceeded(
            quota_usd=float(policy.to_usd(quota_amount)),
            spent_usd=float(policy.to_usd(spent_amount)),
        )


async def daily_amounts(store: Any, user_id: str, key: Mapping[str, Any]) -> tuple[Any, Any]:
    """Read the primary native counter on CNY deployments."""
    policy = get_accounting_policy()
    quota_amount = key.get("quota_daily_cost_amount")
    if quota_amount is None:
        quota_amount = policy.from_usd(resolve_quota(key.get("quota_daily_cost_usd")))
    else:
        quota_amount = Decimal(str(quota_amount))
    if policy.currency == "USD":
        spent = policy.from_usd(await store.get_user_cost_today(user_id) or 0)
    else:
        spent = await store.get_user_cost_today_amount(user_id)
    return quota_amount, Decimal(str(spent))


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
    headers["X-Accounting-Currency"] = get_accounting_policy().currency
    headers["X-RateLimit-Cost-Currency"] = "USD"
    return jsonable_encoder(monetary_payload(body)), headers
