"""Balance probes for clients that check an upstream's quota.

new-api, one-api, sub2api, and cc-switch each request a different path, on a
timer or when someone asks. The four quota answers describe the same remaining
money: the tightest of the account quota, the key's budget, and the project's
budget. ``/v1/sub2api/billing`` answers sub2api's separate rate-multiplier
probe. Checking the balance does not spend it, so an exhausted quota is a
successful read with remaining 0, and the key's ``last_used_at`` is left alone.
Only ``/v1/usage`` reports request and token totals, so only it reads the
usage rollup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from serving.servers.auth import read_balance_limits
from serving.servers.billing_compat import (
    BillingSnapshot,
    LimitWindow,
    UsageTotals,
    credit_grants_payload,
    dashboard_usage_payload,
    sub2api_billing_payload,
    subscription_payload,
    usage_payload,
)
from serving.servers.deps import get_log_store, get_operational_store

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


def _json(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(content=body, headers=_NO_STORE)


def _totals(row: dict[str, Any] | None, *, cost: float) -> UsageTotals:
    """Token counters from a usage rollup row, with the cost supplied by the caller."""
    data = row or {}
    prompt = int(data.get("prompt_tokens") or 0)
    completion = int(data.get("completion_tokens") or 0)
    reported_total = data.get("total_tokens")
    total_tokens = int(reported_total) if reported_total is not None else prompt + completion
    return UsageTotals(
        requests=int(data.get("requests") or 0),
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_creation_tokens=int(data.get("cache_write_tokens") or 0),
        cache_read_tokens=int(data.get("cache_read_tokens") or 0),
        total_tokens=total_tokens,
        cost_usd=cost,
    )


async def _usage_totals(
    log_store: Any, user_id: str, *, today_cost: float
) -> tuple[UsageTotals, UsageTotals] | None:
    """Today's and all-time request counters. Today's cost is the quota counter.

    The rollup is what the console shows for tokens. None when it cannot be
    read: the answer then leaves the totals out and still carries the quota
    numbers, which are what a balance poll is for.
    """
    if log_store is None:
        return None
    try:
        detail = await log_store.get_user_usage_detail(user_id, windows=("today", "alltime"))
    except Exception:
        logger.warning("balance usage rollup failed for user_id=%s", user_id, exc_info=True)
        return None
    alltime = detail.get("alltime") or {}
    return (
        _totals(detail.get("today"), cost=today_cost),
        _totals(alltime, cost=float(alltime.get("cost_usd") or 0.0)),
    )


def _window(
    limits: dict[str, Any], scope: str, window: str, limit_key: str, spent_key: str
) -> LimitWindow | None:
    limit = limits.get(limit_key)
    if limit is None:
        return None
    return LimitWindow(scope, window, float(limit), float(limits.get(spent_key) or 0.0))


def _snapshot(
    limits: dict[str, Any],
    usage: tuple[UsageTotals, UsageTotals] | None,
    now: datetime,
) -> BillingSnapshot:
    specs = (
        ("key", "day", "key_budget_daily_usd", "key_spent_today_usd"),
        ("key", "month", "key_budget_monthly_usd", "key_spent_month_usd"),
        ("project", "day", "project_budget_daily_usd", "project_spent_today_usd"),
        ("project", "month", "project_budget_monthly_usd", "project_spent_month_usd"),
        ("account", "day", "account_quota_usd", "account_spent_usd"),
        ("account", "month", "account_quota_monthly_usd", "account_spent_month_usd"),
    )
    windows = tuple(
        window for window in (_window(limits, *spec) for spec in specs) if window is not None
    )
    today, total = usage or (UsageTotals(), UsageTotals())
    return BillingSnapshot(
        windows=windows,
        account_daily_limit_usd=float(limits["account_quota_usd"]),
        account_daily_spent_usd=float(limits["account_spent_usd"]),
        today=today,
        total=total,
        now=now,
        usage_available=usage is not None,
    )


async def _load_snapshot(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    op_store: Any,
    log_store: Any,
) -> BillingSnapshot:
    """Authenticate the poll and load the counters its answer is built from.

    While user auth is disabled there is no quota, and the answers say so.
    Without a *log_store* the usage rollup is not read.
    """
    now = datetime.now(UTC)
    limits = await read_balance_limits(request, authorization, x_api_key, op_store)
    if limits is None:
        return BillingSnapshot(
            windows=(),
            account_daily_limit_usd=0.0,
            account_daily_spent_usd=0.0,
            today=UsageTotals(),
            total=UsageTotals(),
            now=now,
            unlimited=True,
            usage_available=False,
        )
    usage = await _usage_totals(
        log_store, limits["user_id"], today_cost=float(limits["account_spent_usd"])
    )
    return _snapshot(limits, usage, now)


async def get_billing_snapshot(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> BillingSnapshot:
    """The quota counters and the usage rollup, for ``/v1/usage``."""
    return await _load_snapshot(request, authorization, x_api_key, op_store, log_store)


async def get_quota_snapshot(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
) -> BillingSnapshot:
    """The quota counters alone, for the answers that report only money left."""
    return await _load_snapshot(request, authorization, x_api_key, op_store, None)


@router.get("/v1/usage")
async def get_usage(snapshot: BillingSnapshot = Depends(get_billing_snapshot)) -> JSONResponse:
    """Return remaining quota in the shape sub2api and cc-switch parse."""
    return _json(usage_payload(snapshot))


@router.get("/v1/dashboard/billing/subscription")
async def get_subscription(
    snapshot: BillingSnapshot = Depends(get_quota_snapshot),
) -> JSONResponse:
    """Return the new-api subscription body for the binding quota."""
    return _json(subscription_payload(snapshot))


@router.get("/v1/dashboard/billing/usage")
async def get_dashboard_usage(
    snapshot: BillingSnapshot = Depends(get_quota_snapshot),
) -> JSONResponse:
    """Return binding-quota spend in cents. The date query is ignored."""
    return _json(dashboard_usage_payload(snapshot))


@router.get("/v1/dashboard/billing/credit_grants")
async def get_credit_grants(
    snapshot: BillingSnapshot = Depends(get_quota_snapshot),
) -> JSONResponse:
    """Return remaining quota as an OpenAI credit-grant summary."""
    return _json(credit_grants_payload(snapshot))


@router.get("/v1/sub2api/billing")
async def get_sub2api_billing(
    snapshot: BillingSnapshot = Depends(get_quota_snapshot),
) -> JSONResponse:
    """Return the sub2api rate-multiplier probe. Pricing is the listed model price."""
    return _json(sub2api_billing_payload(snapshot.now))
