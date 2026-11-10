"""Balance probes report the same remaining money the quota gate enforces."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.quota import first_refusal
from serving.servers import billing_compat
from serving.servers.billing_compat import (
    BillingSnapshot,
    LimitWindow,
    UsageTotals,
    binding_quota,
    credit_grants_payload,
    dashboard_usage_payload,
    sub2api_billing_payload,
    subscription_payload,
    usage_payload,
)
from serving.servers.deps import get_log_store, get_operational_store
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import billing

NOW = datetime(2026, 11, 9, 1, 30, tzinfo=UTC)
TODAY = UsageTotals(
    requests=4,
    prompt_tokens=100,
    completion_tokens=20,
    total_tokens=120,
    cost_usd=3.5,
)
TOTAL = UsageTotals(
    requests=10, prompt_tokens=200, completion_tokens=40, total_tokens=240, cost_usd=9
)


def _snapshot(**overrides) -> BillingSnapshot:
    values = {
        "windows": (LimitWindow("account", "day", 10.0, 3.5),),
        "account_daily_limit_usd": 10.0,
        "account_daily_spent_usd": 3.5,
        "today": TODAY,
        "total": TOTAL,
        "now": NOW,
        "unlimited": False,
    }
    values.update(overrides)
    return BillingSnapshot(**values)


def _app(snapshot: BillingSnapshot | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(billing.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_log_store] = lambda: None
    if snapshot is not None:
        app.dependency_overrides[billing.get_billing_snapshot] = lambda: snapshot
        app.dependency_overrides[billing.get_quota_snapshot] = lambda: snapshot
    return app


def test_daily_remaining_matches_new_api_balance():
    snapshot = _snapshot()
    usage = usage_payload(snapshot)
    subscription = subscription_payload(snapshot)
    dashboard = dashboard_usage_payload(snapshot)
    grants = credit_grants_payload(snapshot)

    assert usage["mode"] == "quota_limited"
    assert usage["isValid"] is True
    assert usage["planName"] == "Daily quota"
    assert usage["remaining"] == 6.5
    assert usage["quota"] == {"limit": 10.0, "used": 3.5, "remaining": 6.5, "unit": "USD"}
    assert usage["rate_limits"][0]["reset_at"] == "2026-11-10T00:00:00.000000000Z"
    assert usage["usage"]["today"]["cost"] == 3.5
    assert usage["usage"]["today"]["input_tokens"] == 100

    assert subscription["access_until"] == 0
    assert subscription["hard_limit_usd"] == 10.0
    balance = subscription["hard_limit_usd"] - dashboard["total_usage"] / 100
    assert balance == usage["remaining"]
    assert grants["total_available"] == usage["remaining"]
    assert grants["grants"]["data"][0]["id"] == "daily-quota"


def test_monthly_cap_binds_while_the_daily_window_stays_visible():
    snapshot = _snapshot(
        windows=(
            LimitWindow("account", "day", 10.0, 3.5),
            LimitWindow("account", "month", 5.0, 4.25),
        )
    )
    usage = usage_payload(snapshot)

    assert usage["planName"] == "Monthly quota"
    assert usage["remaining"] == 0.75
    assert usage["quota"]["limit"] == 5.0
    assert usage["quota"]["used"] == 4.25
    daily = usage["rate_limits"][0]
    assert daily["limit"] == 10.0
    assert daily["used"] == 3.5
    assert daily["remaining"] == 6.5
    assert subscription_payload(snapshot)["access_until"] == 0
    grants = credit_grants_payload(snapshot)
    assert grants["grants"]["data"][0]["id"] == "monthly-quota"
    assert grants["total_available"] == 0.75


def test_spend_past_the_daily_quota_reports_zero_remaining():
    today = UsageTotals(cost_usd=12.0)
    snapshot = _snapshot(
        windows=(LimitWindow("account", "day", 10.0, 12.0),),
        account_daily_limit_usd=10.0,
        account_daily_spent_usd=12.0,
        today=today,
    )
    usage = usage_payload(snapshot)
    subscription = subscription_payload(snapshot)
    dashboard = dashboard_usage_payload(snapshot)

    assert usage["remaining"] == 0.0
    assert usage["quota"]["used"] == 10.0
    assert usage["rate_limits"][0]["used"] == 12.0
    assert usage["rate_limits"][0]["remaining"] == 0.0
    assert usage["usage"]["today"]["cost"] == 12.0
    assert subscription["hard_limit_usd"] - dashboard["total_usage"] / 100 == 0.0


def test_a_zero_monthly_quota_is_a_cap():
    snapshot = _snapshot(
        windows=(
            LimitWindow("account", "day", 10.0, 0.0),
            LimitWindow("account", "month", 0.0, 0.0),
        ),
        account_daily_spent_usd=0.0,
        today=UsageTotals(),
    )
    usage = usage_payload(snapshot)

    assert usage["planName"] == "Monthly quota"
    assert usage["remaining"] == 0.0
    assert usage["quota"]["limit"] == 0.0


def test_a_missing_monthly_quota_is_not_a_cap():
    binding = binding_quota(_snapshot())
    assert binding.window == "day"
    assert binding.remaining_usd == 6.5


def test_key_budget_is_tighter_than_the_account_quota():
    snapshot = _snapshot(
        windows=(
            LimitWindow("key", "day", 1.0, 0.4),
            LimitWindow("account", "day", 10.0, 3.5),
        )
    )
    usage = usage_payload(snapshot)

    assert usage["planName"] == "API key daily budget"
    assert usage["remaining"] == 0.6
    assert usage["quota"]["limit"] == 1.0
    assert usage["rate_limits"][0]["remaining"] == 6.5
    assert subscription_payload(snapshot)["hard_limit_usd"] == 1.0


def test_project_budget_is_tighter_than_the_account_quota():
    snapshot = _snapshot(
        windows=(
            LimitWindow("project", "month", 2.0, 1.5),
            LimitWindow("account", "day", 10.0, 3.5),
        )
    )
    usage = usage_payload(snapshot)

    assert usage["planName"] == "Project monthly budget"
    assert usage["remaining"] == 0.5
    assert credit_grants_payload(snapshot)["grants"]["data"][0]["id"] == "project-monthly-budget"


def test_an_equal_remaining_keeps_the_narrower_window():
    snapshot = _snapshot(
        windows=(
            LimitWindow("key", "day", 2.0, 1.0),
            LimitWindow("account", "day", 4.0, 3.0),
        ),
        account_daily_limit_usd=4.0,
        account_daily_spent_usd=3.0,
    )
    binding = binding_quota(snapshot)
    assert (binding.scope, binding.window, binding.remaining_usd) == ("key", "day", 1.0)


def test_reset_at_midnight_is_the_following_midnight():
    snapshot = _snapshot(now=datetime(2026, 11, 9, tzinfo=UTC))
    assert usage_payload(snapshot)["rate_limits"][0]["reset_at"] == "2026-11-10T00:00:00.000000000Z"


def test_sub2api_probe_has_the_fields_its_parser_requires():
    body = sub2api_billing_payload(NOW)
    assert body == {
        "object": "sub2api.key_billing",
        "schema_version": 1,
        "billing_scope": "token",
        "group_rate_multiplier": 1.0,
        "resolved_rate_multiplier": 1.0,
        "peak_rate_enabled": False,
        "effective_rate_multiplier": 1.0,
        "observed_at": "2026-11-09T01:30:00.000000000Z",
    }


def test_unlimited_snapshot_uses_the_client_sentinels():
    snapshot = _snapshot(unlimited=True, windows=())
    usage = usage_payload(snapshot)
    assert usage["mode"] == "unrestricted"
    assert usage["remaining"] == -1
    # new-api reads -1 as unlimited only beside a subscription with no limits.
    assert usage["subscription"] == {
        "daily_limit_usd": None,
        "weekly_limit_usd": None,
        "monthly_limit_usd": None,
    }
    assert "quota" not in usage
    assert subscription_payload(snapshot)["hard_limit_usd"] == 100_000_000
    assert dashboard_usage_payload(snapshot)["total_usage"] == 0


def test_the_five_routes_share_one_remaining_and_are_not_cached():
    client = TestClient(_app(_snapshot()))
    paths = [
        "/v1/usage",
        "/v1/dashboard/billing/subscription",
        "/v1/dashboard/billing/usage",
        "/v1/dashboard/billing/credit_grants",
        "/v1/sub2api/billing",
    ]
    bodies = []
    for path in paths:
        response = client.get(path, params={"start_date": "2020-01-01", "end_date": "2020-01-02"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        bodies.append(response.json())

    usage, subscription, dashboard, grants, probe = bodies
    remaining = usage["remaining"]
    assert remaining == 6.5
    assert subscription["hard_limit_usd"] - dashboard["total_usage"] / 100 == remaining
    assert grants["total_available"] == remaining
    assert probe["object"] == "sub2api.key_billing"
    assert probe["peak_rate_enabled"] is False


def test_a_missing_key_is_401(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    client = TestClient(_app())
    response = client.get("/v1/usage")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == 401


def test_auth_disabled_reports_unlimited(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    client = TestClient(_app())
    usage = client.get("/v1/usage")
    subscription = client.get("/v1/dashboard/billing/subscription")
    assert usage.status_code == 200
    assert usage.json()["remaining"] == -1
    assert usage.json()["subscription"]["daily_limit_usd"] is None
    # Nothing was read, so no totals are reported as zeros.
    assert "usage" not in usage.json()
    assert subscription.json()["hard_limit_usd"] == 100_000_000
    assert subscription.json()["access_until"] == 0


@pytest.mark.asyncio
async def test_balance_read_uses_counters_and_does_not_touch_last_used(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    row = {
        "id": 9,
        "user_id": "heavy-user",
        "quota_daily_cost_usd": 10.0,
        "quota_monthly_cost_usd": 5.0,
        "account_spent_month_usd": 4.25,
        "budget_daily_usd": 1.0,
        "key_spent_today_usd": 0.4,
        "key_spent_month_usd": 0.4,
        "project_budget_daily_usd": None,
        "project_budget_monthly_usd": None,
    }

    async def fake_auth(request, authorization, x_api_key, op_store, *, want_spend=False):
        return row, "hash", 12.5

    monkeypatch.setattr("serving.servers.auth._authenticate_by_api_key", fake_auth)
    store = MagicMock()
    store.update_key_last_used = AsyncMock()
    store.get_key_spend = AsyncMock()
    store.get_user_cost_period = AsyncMock()

    from serving.servers.auth import read_balance_limits

    limits = await read_balance_limits(MagicMock(), "Bearer hyi-test", None, store)

    assert limits["user_id"] == "heavy-user"
    assert limits["account_quota_usd"] == 10.0
    assert limits["account_spent_usd"] == 12.5
    assert limits["account_quota_monthly_usd"] == 5.0
    assert limits["account_spent_month_usd"] == 4.25
    assert limits["key_budget_daily_usd"] == 1.0
    assert limits["key_spent_today_usd"] == 0.4
    store.update_key_last_used.assert_not_awaited()
    store.get_key_spend.assert_not_awaited()
    store.get_user_cost_period.assert_not_awaited()


def test_routed_snapshot_prefers_the_key_budget_and_keeps_today_cost(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    row = {
        "id": 3,
        "user_id": "u1",
        "quota_daily_cost_usd": 10.0,
        "quota_monthly_cost_usd": None,
        "budget_daily_usd": 1.0,
        "key_spent_today_usd": 0.25,
        "key_spent_month_usd": 0.25,
    }

    async def fake_auth(request, authorization, x_api_key, op_store, *, want_spend=False):
        return row, "hash", 3.5

    monkeypatch.setattr("serving.servers.auth._authenticate_by_api_key", fake_auth)
    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {
        "today": {
            "requests": 2,
            "prompt_tokens": 8,
            "completion_tokens": 3,
            "cache_read_tokens": 1,
            "cache_write_tokens": 4,
            "cost_usd": 99.0,
        },
        "alltime": {"requests": 5, "prompt_tokens": 10, "completion_tokens": 6, "cost_usd": 20.0},
    }
    app = _app()
    app.dependency_overrides[get_log_store] = lambda: log_store
    client = TestClient(app)

    body = client.get("/v1/usage", headers={"X-API-Key": "hyi-test"}).json()

    assert body["remaining"] == 0.75
    assert body["planName"] == "API key daily budget"
    assert body["usage"]["today"]["cost"] == 3.5
    assert body["usage"]["today"]["requests"] == 2
    assert body["usage"]["today"]["cache_read_tokens"] == 1
    assert body["usage"]["today"]["cache_creation_tokens"] == 4
    assert body["usage"]["total"]["cost"] == 20.0
    assert body["rate_limits"][0]["used"] == 3.5


def test_a_rollup_failure_still_returns_the_quota_and_leaves_the_totals_out(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")

    async def fake_auth(request, authorization, x_api_key, op_store, *, want_spend=False):
        return {"id": 1, "user_id": "u1", "quota_daily_cost_usd": 10.0}, "hash", 1.0

    monkeypatch.setattr("serving.servers.auth._authenticate_by_api_key", fake_auth)
    log_store = AsyncMock()
    log_store.get_user_usage_detail.side_effect = RuntimeError("rollup down")
    app = _app()
    app.dependency_overrides[get_log_store] = lambda: log_store

    body = TestClient(app).get("/v1/usage", headers={"Authorization": "Bearer hyi-test"}).json()

    assert body["remaining"] == 9.0
    assert body["rate_limits"][0]["used"] == 1.0
    assert "usage" not in body


def test_next_midnight_helper_matches_the_payload():
    """The reset timestamp is the quota module's midnight, including exactly at midnight."""
    from serving.quota import next_utc_midnight

    moment = datetime(2026, 11, 9, tzinfo=UTC)
    formatted = billing_compat._rfc3339_nano(next_utc_midnight(moment))
    assert formatted == "2026-11-10T00:00:00.000000000Z"


_GATE_ARGUMENTS = (
    "account_quota_usd",
    "account_spent_usd",
    "account_quota_monthly_usd",
    "account_spent_month_usd",
    "key_budget_daily_usd",
    "key_budget_monthly_usd",
    "key_spent_today_usd",
    "key_spent_month_usd",
    "project_budget_daily_usd",
    "project_budget_monthly_usd",
    "project_spent_today_usd",
    "project_spent_month_usd",
)


@pytest.mark.parametrize(
    ("limit_key", "spent_key"),
    [
        ("account_quota_usd", "account_spent_usd"),
        ("account_quota_monthly_usd", "account_spent_month_usd"),
        ("key_budget_daily_usd", "key_spent_today_usd"),
        ("key_budget_monthly_usd", "key_spent_month_usd"),
        ("project_budget_daily_usd", "project_spent_today_usd"),
        ("project_budget_monthly_usd", "project_spent_month_usd"),
    ],
)
@pytest.mark.parametrize("spent", [0.0, 9.0, 9.98, 9.985, 9.99, 9.991, 9.995, 10.0, 12.5])
def test_nothing_is_left_exactly_when_the_gate_refuses(limit_key, spent_key, spent):
    """Every answer reads 0 when, and only when, the request gate would refuse.

    The gate refuses once its per-request estimate no longer fits under a
    limit, a cent before the limit itself. The limits come from the counters
    the way the route reads them, and the verdict from the gate's own check.
    """
    limits = {
        "user_id": "u1",
        "account_quota_usd": 100.0,
        "account_spent_usd": 0.0,
        limit_key: 10.0,
        spent_key: spent,
    }
    snapshot = billing._snapshot(limits, None, NOW)
    refused = first_refusal(**{key: limits[key] for key in _GATE_ARGUMENTS if key in limits})

    remaining = binding_quota(snapshot).remaining_usd
    assert (remaining == 0) is (refused is not None)
    new_api_balance = (
        subscription_payload(snapshot)["hard_limit_usd"]
        - dashboard_usage_payload(snapshot)["total_usage"] / 100
    )
    assert new_api_balance == pytest.approx(remaining, abs=1e-9)
    assert usage_payload(snapshot)["remaining"] == remaining
    assert credit_grants_payload(snapshot)["total_available"] == remaining
    if refused is None:
        assert remaining == pytest.approx(10.0 - spent, abs=1e-6)


def test_the_money_only_answers_do_not_read_the_usage_rollup(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")

    async def fake_auth(request, authorization, x_api_key, op_store, *, want_spend=False):
        return {"id": 1, "user_id": "u1", "quota_daily_cost_usd": 10.0}, "hash", 2.0

    monkeypatch.setattr("serving.servers.auth._authenticate_by_api_key", fake_auth)
    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {"today": {}, "alltime": {}}
    app = _app()
    app.dependency_overrides[get_log_store] = lambda: log_store
    client = TestClient(app)
    headers = {"Authorization": "Bearer hyi-test"}

    for path in (
        "/v1/dashboard/billing/subscription",
        "/v1/dashboard/billing/usage",
        "/v1/dashboard/billing/credit_grants",
        "/v1/sub2api/billing",
    ):
        assert client.get(path, headers=headers).status_code == 200
    log_store.get_user_usage_detail.assert_not_awaited()

    assert client.get("/v1/usage", headers=headers).status_code == 200
    log_store.get_user_usage_detail.assert_awaited_once()
