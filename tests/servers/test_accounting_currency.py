"""CNY money crosses HTTP and quota boundaries without becoming USD."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving import accounting, pricing as pricing_module
from serving.accounting import AccountingPolicy
from serving.adapters.base import ModelConfig
from serving.config.settings import get_settings
from serving.grants import mint_grant_token
from serving.pricing import (
    PricingSchedule,
    PricingSnapshot,
    PricingUnavailableError,
    freeze_schedule,
)
from serving.servers import auth
from serving.servers.deps import AppServices, get_operational_store, verify_admin_access
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers.admin import api_keys
from serving.servers.routers.anthropic_messages import anthropic_user_balance
from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.routing_info import Pricing, RoutingInfo
from serving.utils import context as req_ctx


@pytest.fixture(autouse=True)
def cny_policy(monkeypatch: pytest.MonkeyPatch):
    policy = AccountingPolicy(
        currency="CNY",
        usd_to_currency_rate=Decimal("6.7180"),
        rate_date="2026-10-23",
        rate_source="https://example.test/fixed-rate",
    )
    monkeypatch.setattr(accounting, "_policy", policy)
    monkeypatch.setenv("API_KEY_SECRET", "test-cny-accounting-secret")
    get_settings.cache_clear()
    yield policy
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_admin_creates_native_quota_and_returns_both_truthful_units(monkeypatch, cny_policy):
    store = AsyncMock()
    store.check_active_key_exists.return_value = False
    store.create_key.return_value = {"created_at": datetime(2026, 10, 23, tzinfo=UTC)}
    monkeypatch.setattr(api_keys, "log_admin_action", AsyncMock())
    app = FastAPI()
    app.include_router(api_keys.router)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[verify_admin_access] = lambda: "admin-test"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/api-keys",
            json={
                "user_id": "native-user",
                "quota_daily_cost": "10.123456789012",
                "currency": "CNY",
            },
        )
        stale = await client.post(
            "/admin/api-keys",
            json={"user_id": "native-user", "quota_daily_cost": "10", "currency": "USD"},
        )
    assert response.status_code == 201, response.text
    assert stale.status_code == 422
    body = response.json()
    amount = Decimal("10.123456789012")
    assert Decimal(body["quota_daily_cost"]) == amount
    assert Decimal(body["quota_daily_cost_usd"]) == cny_policy.to_usd(amount)
    assert body["currency"] == "CNY"
    assert store.create_key.await_count == 1
    assert store.create_key.call_args.kwargs["quota_daily_cost_amount"] == amount


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["key", "grant"])
async def test_both_inference_doors_enforce_primary_native_quota_over_http(monkeypatch, credential):
    store = AsyncMock()
    key = {
        "id": 7,
        "user_id": "native-user",
        "role": "pro",
        "email": "native@example.test",
        "email_verified": True,
        "quota_daily_cost_usd": 999,
        "quota_daily_cost_amount": Decimal("1"),
    }
    store.get_auth_context_by_key_hash.return_value = key
    store.get_quota_context_for_user.return_value = [key]
    store.get_user_by_id.return_value = {"id": "native-user", "role": "pro", "status": "active"}
    store.get_user_cost_today.return_value = 0
    store.get_user_cost_today_amount.return_value = Decimal("0.99")
    store.get_agent_grant.return_value = {
        "grant_id": "agr_native",
        "user_id": "native-user",
        "external_job_id": "job_native",
        "external_attempt_id": "1",
        "allowed_models": ["test-model"],
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "revoked_at": None,
    }
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "hash_api_key", lambda _: "test-hash")
    monkeypatch.setattr(auth, "log_rejection", AsyncMock())
    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(
        router=RouteExecutor(), operational_store=store, log_store=None
    )

    @app.post("/v1/chat/completions")
    async def infer(user: dict = Depends(auth.verify_api_key)):
        return user

    token = mint_grant_token("agr_native") if credential == "grant" else "hyi-cny-test"
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["currency"] == "CNY"
    assert float(body["quota"]) == pytest.approx(1)
    assert float(body["spent"]) == pytest.approx(0.99)
    assert float(body["quota_usd"]) == pytest.approx(1 / 6.718)
    store.get_user_cost_today_amount.assert_awaited_once_with("native-user")
    store.get_user_cost_today.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("spent", ["0", "0.999999999999", "1", "1.25"])
async def test_balance_reads_exact_native_counter_even_when_exhausted(
    monkeypatch, cny_policy, spent
):
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "native-user",
        "email": "native@example.test",
        "email_verified": True,
        "quota_daily_cost_usd": 999,
        "quota_daily_cost_amount": Decimal("1"),
    }
    store.get_user_cost_today.return_value = 900
    store.get_user_cost_today_amount.return_value = Decimal(spent)
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "hash_api_key", lambda _: "test-hash")
    app = FastAPI()
    app.add_api_route("/anthropic/user/balance", anthropic_user_balance)
    app.state.services = AppServices(
        router=RouteExecutor(), operational_store=store, log_store=None
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/anthropic/user/balance", headers={"Authorization": "Bearer hyi-cny-test"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    remainder = max(Decimal(0), Decimal("1") - Decimal(spent))
    assert body["currency"] == "CNY"
    assert Decimal(body["daily_limit"]) == Decimal("1")
    assert Decimal(body["spent_today"]) == Decimal(spent)
    assert Decimal(body["balance"]) == remainder
    assert body["is_available"] is (remainder > 0)
    assert body["daily_limit_usd"] == round(float(cny_policy.to_usd(1)), 4)
    assert body["spent_today_usd"] == round(float(cny_policy.to_usd(spent)), 4)
    assert body["balance_usd"] == round(float(cny_policy.to_usd(remainder)), 4)
    store.get_user_cost_today_amount.assert_awaited_once_with("native-user")
    store.get_user_cost_today.assert_not_awaited()
    store.update_key_last_used.assert_not_awaited()


@pytest.mark.asyncio
async def test_balance_without_auth_reports_native_currency_and_unknown_amounts(monkeypatch):
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: False)
    app = FastAPI()
    app.add_api_route("/anthropic/user/balance", anthropic_user_balance)
    app.state.services = AppServices(router=RouteExecutor(), operational_store=None, log_store=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/anthropic/user/balance")
    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "CNY"
    assert body["is_available"] is True
    for name in (
        "balance",
        "daily_limit",
        "spent_today",
        "balance_usd",
        "daily_limit_usd",
        "spent_today_usd",
    ):
        assert body[name] is None


@pytest.mark.asyncio
async def test_native_request_cost_reaches_ledger_and_preserves_actual_provider_usd():
    store = AsyncMock()
    tracker = CostTracker(op_store=store, pricing=PricingLookup(router=SimpleNamespace(routes={})))
    routing = RoutingInfo(
        request_id="cny-request",
        model="flash",
        pricing=Pricing(
            prompt_price=1,
            completion_price=4,
            cache_read_price=0.02,
            cache_write_price=0,
        ),
        upstream_cost_usd=0.12,
    )
    result = await tracker.schedule_increment(
        user_id="native-user",
        routing=routing,
        prompt_tokens=1_000_000,
        cache_read_tokens=500_000,
        completion_tokens=100_000,
    )
    await asyncio.gather(*tracker._background_tasks)
    assert result.upstream_cost_usd == 0.12
    assert store.increment_user_cost_amount.await_count == 1
    native = store.increment_user_cost_amount.call_args.args[1]
    assert float(native) == pytest.approx(0.91)
    store.increment_user_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_cost_uses_frozen_native_snapshot_and_does_not_cache_across_requests(
    monkeypatch,
):
    def snapshot(revision, amount, currency="CNY"):
        schedule = PricingSchedule.from_raw(
            {
                "effective_at": "2026-01-01T00:00:00Z",
                "default": {"prompt": str(amount), "completion": "4"},
                "windows": [
                    {"start": "01:00", "end": "04:00", "pricing": {"prompt": "2"}},
                ],
            }
        )
        return PricingSnapshot(
            revision=revision,
            fetched_at=datetime(2026, 10, 23, tzinfo=UTC),
            calendar_years=frozenset({2026}),
            tariffs={"deepseek-flash": freeze_schedule(schedule)},
            model_versions={"deepseek-flash": "test-version"},
            currency=currency,
        )

    old = snapshot("old", 1)
    current = snapshot("new", 3)
    monkeypatch.setattr(pricing_module, "_snapshot_provider", lambda: current)
    config = ModelConfig(
        id="flash",
        name="Flash",
        provider="deepseek",
        base_url="https://example.test",
        endpoint_id="deepseek-official",
        pricing_source={"provider": "deepseek", "model": "deepseek-flash"},
    )
    route = SimpleNamespace(adapters=[(SimpleNamespace(config=config), None)])
    lookup = PricingLookup(router=SimpleNamespace(routes={"flash": route}))
    store = AsyncMock()
    tracker = CostTracker(op_store=store, pricing=lookup)
    routing = RoutingInfo(request_id="cny-source", model="flash", endpoint_id="deepseek-official")

    with req_ctx.push(pricing_snapshot=old, pricing_time=datetime(2026, 10, 23, 2, tzinfo=UTC)):
        first = await tracker.schedule_increment(
            user_id="native-user",
            routing=routing,
            prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        assert lookup.raw_dict_for_routing(routing)["prompt"] == "2"
        assert first.pricing.prompt_price == 2
    with req_ctx.push(pricing_snapshot=current, pricing_time=datetime(2026, 10, 23, 5, tzinfo=UTC)):
        second = await tracker.schedule_increment(
            user_id="native-user",
            routing=routing,
            prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        assert lookup.raw_dict_for_routing(routing)["prompt"] == "3"
        assert second.pricing.prompt_price == 3
    await asyncio.gather(*tracker._background_tasks)
    charges = [call.args[1] for call in store.increment_user_cost_amount.call_args_list]
    assert charges == [2, 3]
    store.increment_user_cost.assert_not_awaited()

    with (
        req_ctx.push(pricing_snapshot=snapshot("usd", 1, "USD")),
        pytest.raises(PricingUnavailableError),
    ):
        lookup.for_routing(routing)
