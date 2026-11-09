"""Response builders for the usage endpoints balance-check clients poll.

FreeInference bills cost quotas per account, and optional budgets per project
and per API key. Callers such as new-api, one-api, sub2api, and cc-switch each
expect a different JSON shape, but they all display one number: how much of
the tightest applicable limit is left.

The numbers come from the same counters the request gate reads
(``user_daily_cost``, ``api_key_daily_cost``, ``spend_counters``). A poll does
not include in-flight reservations: those live on the worker that admitted the
request, and a balance read has to agree across workers.

new-api computes ``balance = hard_limit_usd - total_usage / 100`` and ignores
the ``start_date`` / ``end_date`` query it sends. ``total_usage`` is therefore
spend against the binding quota, in cents, clamped so the balance is never
negative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

from serving.quota import next_utc_midnight, next_utc_month_start

#: new-api treats a hard limit this large, with no usage, as unlimited.
_UNLIMITED_USD = 100_000_000.0

_PLAN_NAME = {
    ("account", "day"): "Daily quota",
    ("account", "month"): "Monthly quota",
    ("key", "day"): "API key daily budget",
    ("key", "month"): "API key monthly budget",
    ("project", "day"): "Project daily budget",
    ("project", "month"): "Project monthly budget",
}

_GRANT_ID = {
    ("account", "day"): "daily-quota",
    ("account", "month"): "monthly-quota",
    ("key", "day"): "key-daily-budget",
    ("key", "month"): "key-monthly-budget",
    ("project", "day"): "project-daily-budget",
    ("project", "month"): "project-monthly-budget",
}


@dataclass(frozen=True)
class UsageTotals:
    """Request and token counters for one window. Cost is USD."""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class LimitWindow:
    """One limit the request gate can refuse on, with the counter spend under it."""

    scope: str
    window: str
    limit_usd: float
    spent_usd: float


@dataclass(frozen=True)
class BillingSnapshot:
    """Quota and spend for the API key that authenticated the poll."""

    windows: tuple[LimitWindow, ...]
    account_daily_limit_usd: float
    account_daily_spent_usd: float
    today: UsageTotals
    total: UsageTotals
    now: datetime
    # Auth-disabled local runs have no quota. Clients treat a negative
    # remaining (sub2api) or a huge hard limit (new-api) as unlimited.
    unlimited: bool = False


@dataclass(frozen=True)
class BindingQuota:
    """The limit that currently leaves the least money."""

    limit_usd: float
    used_usd: float
    remaining_usd: float
    scope: str
    window: str


def _money(value: float) -> float:
    return round(float(value), 6)


def _remaining(limit: float, spent: float) -> float:
    return _money(max(0.0, limit - spent))


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def binding_quota(snapshot: BillingSnapshot) -> BindingQuota:
    """Pick the limit with the least remaining money.

    Windows are considered in the request gate's order (key, project, then
    account; day before month). An equal remaining keeps the earlier window,
    which is the one the gate names first.
    """
    if snapshot.unlimited:
        return BindingQuota(
            limit_usd=_UNLIMITED_USD,
            used_usd=0.0,
            remaining_usd=_UNLIMITED_USD,
            scope="unlimited",
            window="unlimited",
        )

    chosen: LimitWindow | None = None
    chosen_remaining: float | None = None
    for window in snapshot.windows:
        remaining = _remaining(window.limit_usd, window.spent_usd)
        if chosen is None or chosen_remaining is None or remaining < chosen_remaining:
            chosen = window
            chosen_remaining = remaining
    if chosen is None or chosen_remaining is None:
        return BindingQuota(0.0, 0.0, 0.0, "account", "day")
    return BindingQuota(
        limit_usd=_money(chosen.limit_usd),
        used_usd=_money(min(chosen.spent_usd, chosen.limit_usd)),
        remaining_usd=chosen_remaining,
        scope=chosen.scope,
        window=chosen.window,
    )


def _rfc3339_nano(moment: datetime) -> str:
    """Format a timestamp the way Go's time.RFC3339Nano parser accepts."""
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def _day_start(now: datetime) -> datetime:
    current = _utc(now)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    current = _utc(now)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _usage_block(totals: UsageTotals) -> dict[str, Any]:
    return {
        "requests": int(totals.requests),
        "input_tokens": int(totals.prompt_tokens),
        "output_tokens": int(totals.completion_tokens),
        "cache_creation_tokens": int(totals.cache_creation_tokens),
        "cache_read_tokens": int(totals.cache_read_tokens),
        "total_tokens": int(totals.total_tokens),
        "cost": _money(totals.cost_usd),
        "actual_cost": _money(totals.cost_usd),
    }


def usage_payload(snapshot: BillingSnapshot) -> dict[str, Any]:
    """Build the GET /v1/usage body sub2api and cc-switch parse.

    ``remaining`` and ``quota`` are the tightest limit. ``rate_limits`` always
    describes the account's daily quota, including spend past that quota, so a
    monthly or key cap does not hide today's account usage.
    """
    if snapshot.unlimited:
        return {
            "mode": "unrestricted",
            "isValid": True,
            "status": "active",
            "planName": "Unlimited",
            "remaining": -1,
            "unit": "USD",
            "usage": {
                "today": _usage_block(snapshot.today),
                "total": _usage_block(snapshot.total),
            },
        }

    binding = binding_quota(snapshot)
    daily_remaining = _remaining(snapshot.account_daily_limit_usd, snapshot.account_daily_spent_usd)
    body: dict[str, Any] = {
        "mode": "quota_limited",
        "isValid": True,
        "status": "active",
        "planName": _PLAN_NAME.get((binding.scope, binding.window), "Daily quota"),
        "unit": "USD",
        "remaining": binding.remaining_usd,
        "quota": {
            "limit": binding.limit_usd,
            "used": binding.used_usd,
            "remaining": binding.remaining_usd,
            "unit": "USD",
        },
        "rate_limits": [
            {
                "window": "1d",
                "limit": _money(snapshot.account_daily_limit_usd),
                "used": _money(snapshot.account_daily_spent_usd),
                "remaining": daily_remaining,
                "reset_at": _rfc3339_nano(next_utc_midnight(snapshot.now)),
            }
        ],
        "usage": {
            "today": _usage_block(snapshot.today),
            "total": _usage_block(snapshot.total),
        },
    }
    return body


def subscription_payload(snapshot: BillingSnapshot) -> dict[str, Any]:
    """Build the GET /v1/dashboard/billing/subscription body new-api parses.

    ``access_until`` stays 0. new-api treats a past access time as an expired
    key and can disable the channel. A quota resets; it does not expire the key.
    """
    binding = binding_quota(snapshot)
    return {
        "object": "billing_subscription",
        "has_payment_method": True,
        "soft_limit_usd": binding.limit_usd,
        "hard_limit_usd": binding.limit_usd,
        "system_hard_limit_usd": binding.limit_usd,
        "access_until": 0,
    }


def dashboard_usage_payload(snapshot: BillingSnapshot) -> dict[str, Any]:
    """Build the GET /v1/dashboard/billing/usage body.

    ``total_usage`` is USD cents of the binding quota's used amount, clamped
    to the limit. new-api ignores the requested date window, and so does this.
    """
    binding = binding_quota(snapshot)
    return {
        "object": "list",
        "total_usage": _money(binding.used_usd * 100),
    }


def credit_grants_payload(snapshot: BillingSnapshot) -> dict[str, Any]:
    """Build the GET /v1/dashboard/billing/credit_grants body.

    ``total_available`` is the remaining quota. Clients that read only this
    field, including older one-api balance checks, display it directly.
    ``expires_at`` says when the window resets. It is not copied onto
    ``access_until``.
    """
    binding = binding_quota(snapshot)
    if binding.window == "month":
        effective_at = _month_start(snapshot.now)
        expires_at = next_utc_month_start(snapshot.now)
    else:
        effective_at = _day_start(snapshot.now)
        expires_at = next_utc_midnight(snapshot.now)
    if binding.scope == "unlimited":
        grant_id = "unlimited"
    else:
        grant_id = _GRANT_ID.get((binding.scope, binding.window), "daily-quota")
    return {
        "object": "credit_summary",
        "total_granted": binding.limit_usd,
        "total_used": binding.used_usd,
        "total_available": binding.remaining_usd,
        "grants": {
            "object": "list",
            "data": [
                {
                    "object": "credit_grant",
                    "id": grant_id,
                    "grant_amount": binding.limit_usd,
                    "used_amount": binding.used_usd,
                    "effective_at": int(effective_at.timestamp()),
                    "expires_at": int(expires_at.timestamp()),
                }
            ],
        },
    }


def sub2api_billing_payload(now: datetime) -> dict[str, Any]:
    """Build the GET /v1/sub2api/billing body.

    sub2api's probe requires ``object == sub2api.key_billing`` and a token
    rate multiplier. Requests are billed at the listed model price, so every
    multiplier is 1 and peak pricing is off.
    """
    return {
        "object": "sub2api.key_billing",
        "schema_version": 1,
        "billing_scope": "token",
        "group_rate_multiplier": 1.0,
        "resolved_rate_multiplier": 1.0,
        "peak_rate_enabled": False,
        "effective_rate_multiplier": 1.0,
        "observed_at": _rfc3339_nano(now),
    }
