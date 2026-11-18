"""CNY money crosses HTTP and quota boundaries without becoming USD."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving import accounting, pricing as pricing_module, spend_ledger
from serving.accounting import AccountingPolicy, get_accounting_policy
from serving.adapters.base import ModelConfig
from serving.audit import AuditActor
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
from serving.utils import context as req_ctx, password as password_utils
from serving.utils.login_rate_limit import reset_login_rate_limit_state


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
    store.get_default_project.return_value = {"id": "proj_d_native-user", "name": "Default"}
    log_admin_action = AsyncMock()
    monkeypatch.setattr(api_keys, "log_admin_action", log_admin_action)
    app = FastAPI()
    app.include_router(api_keys.router)
    app.dependency_overrides[get_operational_store] = lambda: store

    # ``get_admin_audit_actor`` reads the immutable actor the real
    # ``verify_admin_access`` records on the request. Overriding auth with a
    # synthetic admin therefore has to record one the same way, otherwise the
    # unmodified audit dependency fails closed with 500. Authentication itself
    # is still the route's dependency; only its backend is swapped. A key for
    # another account needs the signed-in administrator's password, checked
    # against this synthetic account.
    def _synthetic_admin(request: Request) -> str:
        request.state.admin_user_id = "admin-test"
        request.state.audit_actor = AuditActor.admin("admin-test", "127.0.0.1")
        return "admin-test"

    app.dependency_overrides[verify_admin_access] = _synthetic_admin
    store.get_user_by_id.return_value = {
        "id": "admin-test",
        "email": "admin@example.test",
        "password_hash": "synthetic-hash",
    }
    monkeypatch.setattr(
        password_utils, "verify_password", lambda plain, _hash: plain == "synthetic-admin-pw"
    )
    reset_login_rate_limit_state()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin/api-keys",
            json={
                "user_id": "native-user",
                "quota_daily_cost": "10.123456789012",
                "currency": "CNY",
                "password": "synthetic-admin-pw",
            },
        )
        stale = await client.post(
            "/admin/api-keys",
            json={
                "user_id": "native-user",
                "quota_daily_cost": "10",
                "currency": "USD",
                "password": "synthetic-admin-pw",
            },
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
    # The audit dependency resolved the recorded actor and passed it through;
    # overriding auth must not silently skip attribution.
    log_admin_action.assert_awaited_once()
    assert log_admin_action.await_args.kwargs["actor"] == AuditActor.admin(
        "admin-test", "127.0.0.1"
    )


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
    app.state.services = AppServices(router=FixedRouter(), operational_store=store, log_store=None)

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
    app.state.services = AppServices(router=FixedRouter(), operational_store=store, log_store=None)
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
    app.state.services = AppServices(router=FixedRouter(), operational_store=None, log_store=None)
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
async def test_native_request_cost_reaches_ledger_and_logs_its_usd_equivalent():
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
    assert store.increment_user_cost_amount.await_count == 1
    native = store.increment_user_cost_amount.call_args.args[1]
    assert native == Decimal("0.91")
    # ``upstream_cost_usd`` is what this request was charged, in USD, as it
    # always has been on a USD ledger -- not the provider's own report.
    assert result.upstream_cost_usd == pytest.approx(float(get_accounting_policy().to_usd(native)))
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
    # Each request's own charge, as recorded in the spend ledger; the ledger
    # may coalesce both into one counter write.
    recorded: list[float] = []
    real_record = spend_ledger.SpendLedger.record

    def spy(self, user_id, cost, **kwargs):
        recorded.append(cost)
        return real_record(self, user_id, cost, **kwargs)

    monkeypatch.setattr(spend_ledger.SpendLedger, "record", spy)

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
    assert await spend_ledger.ledger_for(store).flush()
    assert recorded == [2, 3]
    writes = store.increment_user_cost_amount.call_args_list
    # Written natively and exactly: both requests, nothing converted to USD.
    assert sum(call.args[1] for call in writes) == Decimal("5")
    assert sum(call.kwargs["requests"] for call in writes) == 2
    store.increment_user_cost.assert_not_awaited()

    with (
        req_ctx.push(pricing_snapshot=snapshot("usd", 1, "USD")),
        pytest.raises(PricingUnavailableError),
    ):
        lookup.for_routing(routing)


@pytest.mark.asyncio
async def test_batch_console_context_reads_the_native_counter_and_keeps_usd_truthful(
    monkeypatch, cny_policy
):
    """The batch create page's quota reads the primary native ledger.

    It goes through the same stored-key authentication the batch worker and the
    console submit use, so on a CNY deployment it reads the native counter (never
    the USD one) and reports the exact native amounts alongside truthful USD.
    """
    from fastapi import Response

    from serving.servers.routers import batch_console_create

    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "native-user",
        "email": "native@example.test",
        "email_verified": True,
        "quota_daily_cost_usd": 999,
        "quota_daily_cost_amount": Decimal("12.345678901234"),
    }
    store.get_user_cost_today.return_value = 900
    store.get_user_cost_today_amount.return_value = Decimal("1.000000000001")
    batch_store = AsyncMock()
    batch_store.active_batch_credential.return_value = {
        "key_id": 7,
        "key_prefix": "hyi-cny",
        "key_hash": "stored-hash",
    }
    batch_store.count_active_batches.return_value = 0
    file_store = AsyncMock()
    file_store.usage.return_value = {"files": 0, "bytes": 0}
    for module in (auth, batch_console_create):
        monkeypatch.setattr(module, "is_user_auth_enabled", lambda: True)
    monkeypatch.setattr(
        batch_console_create, "_surface", lambda s, f: (s, f, SimpleNamespace(**_BATCH_LIMITS))
    )
    monkeypatch.setattr(batch_console_create, "may_use_batch", lambda role, config: True)
    monkeypatch.setattr(batch_console_create, "session_model_context", AsyncMock(return_value={}))
    monkeypatch.setattr(batch_console_create, "_catalog", AsyncMock(return_value=([], None)))

    body = await batch_console_create.create_context(
        Response(),
        current_user={"user_id": "native-user", "role": "pro"},
        store=batch_store,
        file_store=file_store,
        op_store=store,
        router_exec=None,
        embedding_adapters={},
        visibility_resolver=None,
    )

    quota = body["quota"]
    assert quota["currency"] == "CNY"
    assert Decimal(quota["daily"]) == Decimal("12.345678901234")
    assert Decimal(quota["spent_today"]) == Decimal("1.000000000001")
    assert quota["daily_usd"] == pytest.approx(float(cny_policy.to_usd("12.345678901234")))
    assert quota["spent_today_usd"] == pytest.approx(float(cny_policy.to_usd("1.000000000001")))
    store.get_auth_context_by_key_hash.assert_awaited_once_with("stored-hash")
    store.get_user_cost_today_amount.assert_awaited_once_with("native-user")
    store.get_user_cost_today.assert_not_awaited()
    # Reading the page is not a request: the key's last use is not touched.
    store.update_key_last_used.assert_not_awaited()


_BATCH_LIMITS = {
    "min_role": "pro",
    "max_body_bytes": 1,
    "max_line_bytes": 1,
    "max_requests": 1,
    "file_uploads_per_hour": 1,
    "files_max_total_bytes": 1,
    "max_active_per_user": 1,
    "file_retention_days": 1,
    "retention_days": 1,
    "file_window_seconds": 86400,
}


# --- Key budgets and the account's monthly quota (core bc7445f2) ----------------


def _native_identity(**overrides):
    """An auth-lookup row whose USD columns would mislead: only native amounts are compared."""
    row = {
        "id": 7,
        "user_id": "native-user",
        "role": "pro",
        "email": "native@example.test",
        "email_verified": True,
        "key_prefix": "hyi-native",
        "key_name": "ci",
        "quota_daily_cost_usd": 999,
        "quota_daily_cost_amount": Decimal("1000"),
        "quota_monthly_cost_usd": None,
        "quota_monthly_cost_amount": None,
        "budget_daily_usd": None,
        "budget_daily_amount": None,
        "budget_monthly_usd": None,
        "budget_monthly_amount": None,
    }
    row.update(overrides)
    return row


async def _infer(monkeypatch, store, token="hyi-cny-test"):
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "hash_api_key", lambda _: "test-hash")
    monkeypatch.setattr(auth, "log_rejection", AsyncMock())
    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(router=FixedRouter(), operational_store=store, log_store=None)

    @app.post("/v1/chat/completions")
    async def infer(user: dict = Depends(auth.verify_api_key)):
        return {"spent_today": str(user["spent_today"]), "api_key_id": user["api_key_id"]}

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        return await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "spent", "refused"),
    [
        # Estimate: 0.01 USD at 6.718 = 0.06718 CNY on top of what is spent.
        ("day", "4.93282", False),
        ("day", "4.932820000001", True),
        ("month", "49.93282", False),
        ("month", "49.95", True),
    ],
)
async def test_a_keys_native_budget_is_compared_in_the_ledger_currency(
    monkeypatch, window, spent, refused
):
    budget = {"day": Decimal("5"), "month": Decimal("50")}[window]
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = _native_identity(
        # A USD column far above the native budget: only the native one may decide.
        **{
            f"budget_{'daily' if window == 'day' else 'monthly'}_usd": 999,
            f"budget_{'daily' if window == 'day' else 'monthly'}_amount": budget,
        }
    )
    store.get_user_cost_today_amount.return_value = Decimal("0")
    store.get_key_spend_amount.return_value = (
        (Decimal(spent), Decimal(spent)) if window == "day" else (Decimal("0"), Decimal(spent))
    )

    response = await _infer(monkeypatch, store)

    store.get_key_spend_amount.assert_awaited_once_with(7)
    store.get_key_spend.assert_not_awaited()
    if not refused:
        assert response.status_code == 200, response.text
        assert response.json()["api_key_id"] == 7
        return
    assert response.status_code == 429, response.text
    body = response.json()
    assert (body["scope"], body["window"], body["code"]) == ("key", window, "budget_exceeded")
    assert body["currency"] == "CNY"
    assert Decimal(str(body["quota"])) == budget
    assert Decimal(str(body["spent"])) == Decimal(spent)
    assert body["quota_usd"] == pytest.approx(float(budget / Decimal("6.718")))
    assert "¥" + f"{budget:.2f}" in body["message"]
    assert response.headers["X-Accounting-Currency"] == "CNY"
    assert response.headers["X-RateLimit-Cost-Currency"] == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["key", "grant"])
async def test_the_accounts_native_monthly_quota_is_enforced_at_both_doors(monkeypatch, credential):
    key = _native_identity(quota_monthly_cost_usd=999, quota_monthly_cost_amount=Decimal("100"))
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = key
    store.get_quota_context_for_user.return_value = [key]
    store.get_user_by_id.return_value = {"id": "native-user", "role": "pro", "status": "active"}
    store.get_user_cost_today_amount.return_value = Decimal("0")
    store.get_user_cost_month_amount.return_value = Decimal("99.95")
    store.get_agent_grant.return_value = {
        "grant_id": "agr_native",
        "user_id": "native-user",
        "external_job_id": "job_native",
        "external_attempt_id": "1",
        "allowed_models": ["test-model"],
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "revoked_at": None,
    }
    token = mint_grant_token("agr_native") if credential == "grant" else "hyi-cny-test"

    response = await _infer(monkeypatch, store, token)

    assert response.status_code == 429, response.text
    body = response.json()
    assert (body["scope"], body["window"]) == ("account", "month")
    assert body["currency"] == "CNY"
    assert Decimal(str(body["quota"])) == Decimal("100")
    assert Decimal(str(body["spent"])) == Decimal("99.95")
    store.get_user_cost_month_amount.assert_awaited_once_with("native-user")
    store.get_user_cost_period.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_auth_lookups_native_spend_is_used_without_another_query(monkeypatch):
    """The merged lookup's native columns spare the gate every extra read."""
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = _native_identity(
        cost_amount=Decimal("0.5"),
        budget_daily_usd=1,
        budget_daily_amount=Decimal("1"),
        key_spent_today_amount=Decimal("0.5"),
        key_spent_month_amount=Decimal("0.5"),
        quota_monthly_cost_usd=1,
        quota_monthly_cost_amount=Decimal("10"),
        account_spent_month_amount=Decimal("0.5"),
    )

    response = await _infer(monkeypatch, store)

    assert response.status_code == 200, response.text
    assert Decimal(response.json()["spent_today"]) == Decimal("0.5")
    store.get_key_spend_amount.assert_not_awaited()
    store.get_user_cost_month_amount.assert_not_awaited()
    store.get_user_cost_today_amount.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stored_keys_native_budget_stops_queued_work(monkeypatch):
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = _native_identity(
        budget_monthly_usd=999, budget_monthly_amount=Decimal("2")
    )
    store.get_user_cost_today_amount.return_value = Decimal("0")
    store.get_key_spend_amount.return_value = (Decimal("0"), Decimal("1.95"))
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)

    with pytest.raises(auth.StoredKeyQuotaExceeded) as exc:
        await auth.authenticate_stored_key("test-hash", store)

    assert (exc.value.scope, exc.value.window) == ("key", "month")
    assert exc.value.quota_usd == pytest.approx(2 / 6.718)
    # Validating a batch spends nothing and is not gated.
    context = await auth.authenticate_stored_key("test-hash", store, check_quota=False)
    assert context["quota_daily_cost"] == Decimal("1000")


@pytest.mark.asyncio
async def test_batch_management_records_the_native_refusal_for_create(monkeypatch):
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = _native_identity(
        budget_daily_usd=999, budget_daily_amount=Decimal("1")
    )
    store.get_user_cost_today_amount.return_value = Decimal("0")
    store.get_key_spend_amount.return_value = (Decimal("0.99"), Decimal("0.99"))
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "hash_api_key", lambda _: "test-hash")
    monkeypatch.setattr(auth, "log_rejection", AsyncMock())
    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(router=FixedRouter(), operational_store=store, log_store=None)

    @app.post("/v1/batches")
    async def create(request: Request, user: dict = Depends(auth.verify_batch_management_identity)):
        assert user["quota_exceeded"] is True
        auth.enforce_daily_quota(request, user)
        return {}

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post("/v1/batches", headers={"Authorization": "Bearer hyi-x"})

    assert response.status_code == 429, response.text
    body = response.json()
    assert (body["scope"], body["window"], body["currency"]) == ("key", "day", "CNY")
    assert Decimal(str(body["quota"])) == Decimal("1")
    assert Decimal(str(body["spent"])) == Decimal("0.99")


def test_a_context_without_its_refusal_reports_native_daily_amounts():
    refusal = auth._daily_refusal_from_context(
        {
            "quota_daily_cost": Decimal("10"),
            "spent_today": Decimal("9.99"),
            "quota_daily_cost_usd": 1.0,
            "spent_today_usd": 1.0,
        }
    )
    assert (refusal.limit_amount, refusal.spent_amount) == (Decimal("10"), Decimal("9.99"))
    assert refusal.limit_usd == pytest.approx(10 / 6.718)


def test_native_key_budget_input_is_exact_and_its_usd_equivalent_fits_the_column():
    from serving.schemas_auth import APIKeyUpdateRequest

    payload = APIKeyUpdateRequest.model_validate({"budget_daily": "50", "currency": "CNY"})
    assert payload.budget_daily == Decimal("50")
    # 50 / 6.718 = 7.44269..., stored at the column's four places.
    assert payload.budget_daily_usd == Decimal("7.4427")
    assert {"budget_daily", "budget_daily_usd"} <= payload.model_fields_set
    cleared = APIKeyUpdateRequest.model_validate({"budget_monthly": None, "currency": "CNY"})
    assert cleared.budget_monthly_usd is None and "budget_monthly_usd" in cleared.model_fields_set
    legacy = APIKeyUpdateRequest.model_validate({"budget_daily_usd": "5"})
    assert legacy.budget_daily_usd == Decimal("5") and "budget_daily" not in legacy.model_fields_set
    for invalid in (
        {"budget_daily": "50", "currency": "USD"},
        {"budget_daily": "50"},
        {"budget_daily": "50", "budget_daily_usd": "7", "currency": "CNY"},
        {"budget_daily": "-1", "currency": "CNY"},
        {"budget_daily": "1.00001", "currency": "CNY"},
        # Bounded by its USD equivalent, like a USD budget.
        {"budget_daily": "7000000", "currency": "CNY"},
    ):
        with pytest.raises(ValueError):
            APIKeyUpdateRequest.model_validate(invalid)


@pytest.mark.asyncio
async def test_the_key_settings_route_stores_and_audits_the_native_budget(monkeypatch):
    from serving.servers.deps import get_current_user, get_db_logger
    from serving.servers.routers import user_routes

    store = AsyncMock()
    store.update_key_settings.return_value = (
        {"name": None, "budget_daily_usd": None, "budget_daily_amount": None},
        {
            "name": None,
            "budget_daily_usd": Decimal("7.4427"),
            "budget_daily_amount": Decimal("50"),
            "budget_monthly_usd": None,
            "budget_monthly_amount": None,
            "allowed_models": None,
        },
    )
    log_admin_action = AsyncMock()
    monkeypatch.setattr(user_routes, "log_admin_action", log_admin_action)
    app = FastAPI()
    app.include_router(user_routes.router)
    app.state.services = AppServices(router=FixedRouter(), operational_store=store, log_store=None)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_db_logger] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "native-user", "role": "pro"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/user/api-keys/hyi-native", json={"budget_daily": 50, "currency": "CNY"}
        )
        stale = await client.patch(
            "/user/api-keys/hyi-native", json={"budget_daily": 50, "currency": "USD"}
        )

    assert response.status_code == 200, response.text
    assert stale.status_code == 422
    store.update_key_settings.assert_awaited_once_with(
        "native-user",
        "hyi-native",
        {"budget_daily_usd": Decimal("7.4427"), "budget_daily_amount": Decimal("50")},
    )
    body = response.json()
    assert body["currency"] == "CNY"
    assert body["budget_daily"] == 50 and body["budget_daily_usd"] == 7.4427
    changed = log_admin_action.await_args.kwargs["details"]["changed"]
    assert changed["budget_daily"] == {"from": None, "to": Decimal("50")}
    assert changed["budget_daily_usd"] == {"from": None, "to": 7.4427}


def test_usd_key_responses_keep_their_upstream_shape(monkeypatch):
    from serving.schemas_auth import APIKeyListItem, APIKeyUpdateResponse

    monkeypatch.setattr(accounting, "_policy", AccountingPolicy())
    update = APIKeyUpdateResponse(key_prefix="p", budget_daily_usd=5.0).model_dump()
    assert update == {
        "key_prefix": "p",
        "name": None,
        "budget_daily_usd": 5.0,
        "budget_monthly_usd": None,
        "allowed_models": None,
    }
    item = APIKeyListItem(
        key_prefix="p",
        key_masked="p…",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="active",
    ).model_dump()
    assert "currency" not in item and "budget_daily" not in item and "spent_today" not in item


@pytest.mark.asyncio
async def test_service_impersonation_keeps_the_users_exact_native_quota(monkeypatch):
    store = AsyncMock()
    store.get_user_by_id.return_value = {"id": "native-target", "status": "active", "role": "pro"}
    store.get_quota_context_for_user.return_value = [
        {
            "quota_daily_cost_usd": Decimal("0.1838"),
            "quota_monthly_cost_usd": Decimal("0.3492"),
            "quota_daily_cost_amount": Decimal("1.234567890123"),
            "quota_monthly_cost_amount": Decimal("2.345678901234"),
        }
    ]
    monkeypatch.setattr(auth, "_impersonation_service_key_hashes", lambda: frozenset({"synthetic"}))
    effective = await auth._resolve_effective_identity({}, "synthetic", "native-target", store)
    assert effective["quota_daily_cost_amount"] == Decimal("1.234567890123")
    assert effective["quota_monthly_cost_amount"] == Decimal("2.345678901234")
