"""The account's cost quotas, in one place, for every door into inference.

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
| the limits, ``quota_daily_cost_usd`` and ``quota_monthly_cost_usd`` | each row of ``api_keys`` | **the account** |
| the spend | ``user_daily_cost`` | **user** |

An account may hold several API keys. The limit is the account's, copied onto
every one of its unrevoked keys by the store (``OperationalStore.create_key``),
so the key the direct path authenticates with and the account a grant is found
by read the same number: which key's limit wins is never a question.

The daily limit has a default (:data:`DEFAULT_DAILY_QUOTA_USD`); the monthly
one does not: NULL is no monthly limit, which is what every account had in
effect before it was enforced. Month spend is the sum of the month's
``user_daily_cost`` rows, the same days the admin console reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi.encoders import jsonable_encoder

from serving.accounting import USD, Money, get_accounting_policy, money_decimal
from serving.accounting_api import monetary_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

#: What an old row with no configured quota means. Matches ``verify_api_key``
#: exactly; diverging would make the two doors disagree about the same account.
DEFAULT_DAILY_QUOTA_USD = 1000.0

#: Charged optimistically before the call, since the real cost is not known
#: until it returns. Same value the direct path uses. It is also the least an
#: admitted request reserves while it runs (:mod:`serving.spend_ledger`), so
#: requests in flight count against the limits they may yet exceed.
ESTIMATED_REQUEST_COST_USD = 0.01

#: The most one request reserves while it runs, whatever its estimate
#: (:func:`estimate_request_cost`): more than the largest request of a typical
#: model costs, and little enough that one mispriced or oversized estimate
#: holds back only a few dollars of the caller's budget.
MAX_RESERVED_REQUEST_COST_USD = 5.0


def estimate_request_cost(
    *,
    prompt_tokens: int,
    output_tokens: int,
    prompt_price: float,
    completion_price: float,
) -> Money:
    """What a request may cost, for reserving it while it runs.

    Its prompt and the output it allows, at its model's prices (per million
    tokens, in the ledger currency: :func:`serving.pricing.effective_pricing`),
    within [:func:`estimated_request_cost`, :func:`max_reserved_request_cost`]
    -- :data:`ESTIMATED_REQUEST_COST_USD` and
    :data:`MAX_RESERVED_REQUEST_COST_USD` on a USD ledger; the most, when a
    price is not a number.
    """
    policy = get_accounting_policy()
    ceiling = max_reserved_request_cost()
    prices = [policy.number(price) for price in (prompt_price, completion_price)]
    # NaN compares unequal to itself, quietly, as a float and as a decimal.
    if any(price != price for price in prices):
        return ceiling
    prompt, completion = (max(price, policy.zero) for price in prices)
    if not all(_finite(price) for price in (prompt, completion)):
        return ceiling
    estimate = (max(prompt_tokens, 0) * prompt + max(output_tokens, 0) * completion) / 1_000_000
    if not _finite(estimate):
        return ceiling
    return min(max(estimate, estimated_request_cost()), ceiling)


def _finite(value: Money) -> bool:
    return math.isfinite(value) if isinstance(value, float) else value.is_finite()


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


def estimated_request_cost() -> Money:
    """:data:`ESTIMATED_REQUEST_COST_USD` in the ledger currency, as the spend ledger counts it.

    The gates and the spend ledger (:mod:`serving.spend_ledger`) count in the
    deployment's accounting currency; on a USD ledger this is the constant.
    """
    return get_accounting_policy().in_ledger(ESTIMATED_REQUEST_COST_USD)


def max_reserved_request_cost() -> Money:
    """:data:`MAX_RESERVED_REQUEST_COST_USD` in the ledger currency."""
    return get_accounting_policy().in_ledger(MAX_RESERVED_REQUEST_COST_USD)


async def daily_amounts(
    store: Any, user_id: str, key: Mapping[str, Any], *, spent_usd: float | None = None
) -> tuple[Decimal, Decimal]:
    """The account's daily limit and today's spend in the ledger currency.

    The limit is the identity's native ``quota_daily_cost_amount`` (the
    organization ceiling, else the key's copy of the account quota, as the
    auth lookups resolve it), falling back to its USD equivalent at the fixed
    rate. A non-USD ledger reads the native counter: the one the auth join
    carries (``cost_amount``), else the store's.
    """
    policy = get_accounting_policy()
    quota_amount = native_limit(
        key, "quota_daily_cost_amount", "quota_daily_cost_usd", default_usd=DEFAULT_DAILY_QUOTA_USD
    )
    if not policy.native:
        spent = policy.from_usd(
            spent_usd if spent_usd is not None else await store.get_user_cost_today(user_id) or 0
        )
    elif key.get("cost_amount") is not None:
        # The live auth join carries the native counter with the key identity.
        spent = key["cost_amount"]
    else:
        spent = await store.get_user_cost_today_amount(user_id)
    # The daily quota always has a limit: NULL in both columns is the default.
    return cast("Decimal", quota_amount), Decimal(str(spent))


def exceeded_payload(
    *,
    quota_usd: float,
    spent_usd: float,
    contact_email: str = "",
    quota_amount: Decimal | None = None,
    spent_amount: Decimal | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the 429 body and headers both doors return.

    Args:
        quota_usd: The account's daily limit.
        spent_usd: What it has already spent today.
        contact_email: Where to ask for more, if the deployment publishes one.
        quota_amount: The exact native limit, when the gate compared natively.
        spent_amount: The exact native spend, when the gate compared natively.

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
    return _native_payload(body, headers, quota_amount, spent_amount)


Scope = Literal["key", "project", "account"]
Window = Literal["day", "month"]


@dataclass(frozen=True, slots=True)
class BudgetRefusal:
    """Which limit a request would exceed, and by how much it is already spent.

    ``scope`` names whose limit it is: the presented API key's own budget, or
    the account's quota. ``key_label`` is how a message names the key (its
    name, else its masked prefix); None for the account.

    ``limit_usd`` / ``spent_usd`` keep their USD meaning. A refusal decided in
    the native ledger (:func:`first_refusal_amount`) also carries the exact
    native ``limit_amount`` / ``spent_amount`` it compared, so the 429 reports
    them without a lossy round trip through the USD equivalents.
    """

    scope: Scope
    window: Window
    limit_usd: float
    spent_usd: float
    key_prefix: str | None = None
    key_label: str | None = None
    scope_name: str | None = None
    limit_amount: Decimal | None = None
    spent_amount: Decimal | None = None

    def reset_at(self, now: datetime | None = None) -> datetime:
        """When the window's counter starts again."""
        return next_utc_midnight(now) if self.window == "day" else next_utc_month_start(now)


def first_refusal(
    *,
    account_quota_usd: Money,
    account_spent_usd: Money,
    account_quota_monthly_usd: Money | None = None,
    account_spent_month_usd: Money = 0.0,
    key_budget_daily_usd: Money | None = None,
    key_budget_monthly_usd: Money | None = None,
    key_spent_today_usd: Money = 0.0,
    key_spent_month_usd: Money = 0.0,
    project_budget_daily_usd: Money | None = None,
    project_budget_monthly_usd: Money | None = None,
    project_spent_today_usd: Money = 0.0,
    project_spent_month_usd: Money = 0.0,
    project_name: str | None = None,
    key_prefix: str | None = None,
    key_label: str | None = None,
) -> BudgetRefusal | None:
    """Return the narrowest limit this request does not fit under, or None.

    Checked in order key, project, account (day then month at each level), so a
    refusal names the narrowest limit that ran out. Each uses the same
    optimistic estimate (:func:`estimated_request_cost`), so the levels behave
    alike near their limits. A NULL budget or monthly quota is no limit there.

    The limits and spends are ledger amounts, in the ledger's arithmetic (the
    ``*_usd`` names predate other ledgers): floats compared exactly as they
    always were on a USD ledger, decimals on a native one. A native refusal
    keeps the amounts it compared and reports their USD equivalents.
    """
    policy = get_accounting_policy()
    estimate = estimated_request_cost()

    def over(limit: Money | None, spent: Money) -> bool:
        return limit is not None and spent + estimate > limit

    def refuse(
        scope: Scope, window: Window, limit: Money, spent: Money, **named: Any
    ) -> BudgetRefusal:
        native = (limit, spent) if policy.native else (None, None)
        return BudgetRefusal(
            scope,
            window,
            float(policy.as_usd(limit)),
            float(policy.as_usd(spent)) if policy.native else spent,
            **named,
            limit_amount=native[0],
            spent_amount=native[1],
        )

    key = {"key_prefix": key_prefix, "key_label": key_label}
    project = {"scope_name": project_name}
    if over(key_budget_daily_usd, key_spent_today_usd):
        return refuse("key", "day", key_budget_daily_usd, key_spent_today_usd, **key)
    if over(key_budget_monthly_usd, key_spent_month_usd):
        return refuse("key", "month", key_budget_monthly_usd, key_spent_month_usd, **key)
    if over(project_budget_daily_usd, project_spent_today_usd):
        return refuse(
            "project", "day", project_budget_daily_usd, project_spent_today_usd, **project
        )
    if over(project_budget_monthly_usd, project_spent_month_usd):
        return refuse(
            "project", "month", project_budget_monthly_usd, project_spent_month_usd, **project
        )
    if over(account_quota_usd, account_spent_usd):
        return refuse("account", "day", account_quota_usd, account_spent_usd)
    if over(account_quota_monthly_usd, account_spent_month_usd):
        return refuse("account", "month", account_quota_monthly_usd, account_spent_month_usd)
    return None


def native_limit(
    row: Mapping[str, Any], amount_key: str, usd_key: str, *, default_usd: float | None = None
) -> Decimal | None:
    """Read a stored limit in the ledger currency.

    The native column is primary. A row whose native column is still NULL (it
    predates the column) falls back to its USD equivalent at the fixed policy
    rate; NULL in both is no limit, or *default_usd* where the limit has one.
    """
    policy = get_accounting_policy()
    amount = row.get(amount_key)
    if amount is not None:
        return money_decimal(amount)
    legacy = row.get(usd_key)
    if legacy is None:
        return None if default_usd is None else policy.from_usd(default_usd)
    return policy.from_usd(legacy)


def _native_payload(
    body: dict[str, Any],
    headers: dict[str, str],
    limit_amount: Decimal | None,
    spent_amount: Decimal | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Add the ledger-currency aliases to a 429 whose ``*_usd`` fields stay USD.

    Exact native amounts win over a projection of the USD equivalents; the
    ``X-RateLimit-*-Cost`` headers keep their USD meaning and say so.
    """
    policy = get_accounting_policy()
    headers["X-Accounting-Currency"] = policy.currency
    headers["X-RateLimit-Cost-Currency"] = USD
    if not policy.native:
        # A USD ledger's 429 body keeps exactly the shape it always had.
        return body, headers
    if limit_amount is not None and spent_amount is not None:
        body["quota"] = limit_amount
        body["spent"] = spent_amount
        body["remaining"] = max(Decimal(0), limit_amount - spent_amount)
    return jsonable_encoder(monetary_payload(body)), headers


def _money_label(usd: float, amount: Decimal | None) -> str:
    """How a message states a limit: in the ledger currency when it was compared there."""
    if amount is None:
        return f"${usd:.2f}"
    return get_accounting_policy().label(amount)


def refusal_payload(
    refusal: BudgetRefusal, *, contact_email: str = ""
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the 429 body and headers for *refusal*.

    The account's daily refusal is :func:`exceeded_payload` unchanged plus
    ``code``, ``scope`` and ``window``, so clients reading today's fields keep
    working. The account's monthly refusal and a key's have the same fields
    and reset with their window; a key's also names the key.
    """
    if refusal.scope == "account" and refusal.window == "day":
        body, headers = exceeded_payload(
            quota_usd=refusal.limit_usd,
            spent_usd=refusal.spent_usd,
            contact_email=contact_email,
            quota_amount=refusal.limit_amount,
            spent_amount=refusal.spent_amount,
        )
        return {**body, "code": "budget_exceeded", "scope": "account", "window": "day"}, headers
    now = datetime.now(UTC)
    reset_at = refusal.reset_at(now)
    retry_after = max(1, int((reset_at - now).total_seconds()))
    remaining = max(0, refusal.limit_usd - refusal.spent_usd)
    period = "daily" if refusal.window == "day" else "monthly"
    if refusal.scope == "account":
        error = "Monthly cost quota exceeded"
        message = (
            f"Monthly quota exhausted. Need more? Email {contact_email} and explain your use case."
            if contact_email
            else "Monthly quota exhausted. Contact the operator of this deployment."
        )
        named: dict[str, Any] = {}
    elif refusal.scope == "project":
        error = f"Project {period} budget exceeded"
        message = (
            f"Project {refusal.scope_name!r} reached its {period} budget "
            f"of {_money_label(refusal.limit_usd, refusal.limit_amount)}. Raise the "
            "project's budget in the console."
        )
        named = {"project": refusal.scope_name}
    else:
        error = f"API key {period} budget exceeded"
        message = (
            f"API key {refusal.key_label or refusal.key_prefix} reached its {period} budget "
            f"of {_money_label(refusal.limit_usd, refusal.limit_amount)}. Raise the key's "
            "budget in the console, or use "
            "another key."
        )
        named = {"key_prefix": refusal.key_prefix}
    body = {
        "error": error,
        "code": "budget_exceeded",
        "scope": refusal.scope,
        "window": refusal.window,
        **named,
        "quota_usd": refusal.limit_usd,
        "spent_usd": refusal.spent_usd,
        "remaining_usd": remaining,
        "reset_at": reset_at.isoformat(),
        "contact_email": contact_email,
        "message": message,
        "retry_after": retry_after,
    }
    headers = {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit-Cost": str(refusal.limit_usd),
        "X-RateLimit-Remaining-Cost": str(remaining),
        "X-RateLimit-Reset": str(int(reset_at.timestamp())),
    }
    return _native_payload(body, headers, refusal.limit_amount, refusal.spent_amount)
