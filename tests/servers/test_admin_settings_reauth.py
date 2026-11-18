"""A security-relevant runtime setting changes only after the administrator confirms it.

``PATCH /admin/settings/{key}`` used to take ``verify_admin_access`` alone, so a
stolen console session or ``ADMIN_TOKEN`` could open signup, turn off email
verification or inference keys, log full payloads or raise every quota. A
guarded setting now asks for what ``PATCH /admin/config`` asks for a security
setting (``confirm_administrator``): a recent authenticator code, the
account's password, or a fresh single sign-on -- and the admin token cannot
give any of them. Analytics switches change as they always did.

The administrator's password is checked by the real re-authentication with a
stand-in hash, its attempts counted against the real sign-in limits. Nothing
here sends mail or reaches an identity provider.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from serving.audit import AuditActor
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    runtime_setting_security_relevant,
)
from serving.config.settings import settings
from serving.exceptions import TwoFactorRequiredError
from serving.schemas_auth import SsoReauthStartRequest
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import reauthentication
from serving.utils import password as password_utils
from serving.utils.login_rate_limit import reset_login_rate_limit_state

pytestmark = pytest.mark.asyncio

ADMIN_TOKEN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-admin-password"

#: What the registry guards. A setting added later must be placed on one side
#: or the other here, deliberately.
GUARDED = {
    "user_auth_enabled",
    "signup_enabled",
    "signup_require_email_verification",
    "signup_admin_notify_enabled",
    "log_full_payload",
    "log_rejected_requests",
    "user_concurrency_free",
    "user_concurrency_pro",
    "user_concurrency_internal",
    "user_concurrency_admin",
    "user_daily_quota_free",
    "user_daily_quota_pro",
    "user_daily_quota_internal",
    "user_daily_quota_admin",
}
UNGUARDED = {
    "analytics_token_usage_enabled",
    "analytics_users_and_requests_enabled",
    "analytics_performance_metrics_enabled",
    "analytics_growth_rate_enabled",
    "log_synthetic_probes",
    "force_chat_completions_streaming",
    "reasoning_small_call_reroute_enabled",
    "auto_mode_enabled",
    # RouteWise settings have their own endpoint; this one answers 404 for them.
    "routewise_budget_alpha",
    "routewise_latency_slo_sec",
    "routewise_probe_enabled",
    "routewise_probe_interval_sec",
}


def test_every_runtime_setting_is_classified():
    assert set(RUNTIME_SETTINGS_REGISTRY) == GUARDED | UNGUARDED
    assert {key for key in RUNTIME_SETTINGS_REGISTRY if runtime_setting_security_relevant(key)} == (
        GUARDED
    )


def test_single_sign_on_can_confirm_an_administrators_action():
    assert SsoReauthStartRequest(purpose="admin_action").purpose == "admin_action"


@pytest.fixture
def op_store() -> MagicMock:
    store = MagicMock()
    rows: dict[str, dict[str, Any]] = {}

    async def get_setting(key):
        return rows.get(key)

    async def set_setting(key, value, value_type, updated_by):
        rows[key] = {"key": key, "value": value, "value_type": value_type}

    async def set_settings(batch, updated_by):
        for key, value, value_type in batch:
            rows[key] = {"key": key, "value": value, "value_type": value_type}

    store.rows = rows
    store.get_setting = AsyncMock(side_effect=get_setting)
    store.set_setting = AsyncMock(side_effect=set_setting)
    store.set_settings = AsyncMock(side_effect=set_settings)
    store.list_settings = AsyncMock(return_value=[])
    store.list_signup_allowed_domains = AsyncMock(return_value=[])
    store.list_user_identities = AsyncMock(return_value=[])
    store.consume_sso_reauth_proof = AsyncMock(return_value=False)
    return store


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.settings.log_admin_action", log_action)
    return log_action


@pytest.fixture
async def client(monkeypatch, op_store, audit):
    app = FastAPI()
    install_exception_handlers(app)
    app.state.services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=MagicMock(),
        runtime_settings=RuntimeSettings(op_store),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    for name, value in (
        ("login_rate_limit_per_15min", 5),
        ("login_account_rate_limit_per_15min", 50),
        ("login_rate_limit_per_hour_per_ip", 20),
    ):
        monkeypatch.setattr(settings, name, value)
    reset_login_rate_limit_state()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    reset_login_rate_limit_state()


@pytest.fixture
def console_admin(client, op_store, monkeypatch):
    """An administrator signed in to the console, session ``sid-1``, with a password."""
    app = client._transport.app

    async def console_session(request: Request) -> str:
        request.state.admin_user_id = "u-admin"
        request.state.admin_session_id = "sid-1"
        request.state.audit_actor = AuditActor.admin("u-admin", "127.0.0.1")
        return "admin@example.com"

    app.dependency_overrides[verify_admin_access] = console_session
    account: dict[str, Any] = {
        "id": "u-admin",
        "email": "admin@example.com",
        "password_hash": "stored-hash",
        "phone": None,
        "phone_verified": False,
    }
    op_store.get_user_by_id = AsyncMock(return_value=account)
    monkeypatch.setattr(password_utils, "verify_password", lambda plain, _hash: plain == PASSWORD)
    # No authenticator on the account unless a test says otherwise.
    monkeypatch.setattr(reauthentication, "confirmed_by_two_factor", AsyncMock(return_value=False))
    return account


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("signup_require_email_verification", False),
        ("user_auth_enabled", False),
        ("log_full_payload", True),
        ("user_daily_quota_free", 100000.0),
        ("user_concurrency_admin", 0),
    ],
)
async def test_the_admin_token_cannot_change_a_guarded_setting(client, op_store, audit, key, value):
    response = await client.patch(
        f"/admin/settings/{key}", headers=ADMIN_TOKEN, json={"value": value}
    )

    assert response.status_code == 403
    assert "admin token cannot confirm" in response.json()["detail"]
    op_store.set_setting.assert_not_awaited()
    audit.assert_not_awaited()


async def test_the_admin_token_still_changes_an_analytics_switch(client, op_store):
    response = await client.patch(
        "/admin/settings/analytics_growth_rate_enabled", headers=ADMIN_TOKEN, json={"value": True}
    )

    assert response.status_code == 200
    assert response.json()["security_relevant"] is False
    op_store.set_setting.assert_awaited_once()


async def test_the_listing_says_which_settings_need_confirmation(client):
    response = await client.get("/admin/settings", headers=ADMIN_TOKEN)

    assert response.status_code == 200
    flags = {item["key"]: item["security_relevant"] for item in response.json()["settings"]}
    assert flags["signup_enabled"] is True
    assert flags["user_daily_quota_pro"] is True
    assert flags["analytics_token_usage_enabled"] is False


async def test_a_password_account_confirms_with_its_password(
    client, op_store, audit, console_admin
):
    missing = await client.patch(
        "/admin/settings/signup_require_email_verification", json={"value": False}
    )
    wrong = await client.patch(
        "/admin/settings/signup_require_email_verification",
        json={"value": False, "password": "guess"},
    )
    assert (missing.status_code, wrong.status_code) == (400, 400)
    op_store.set_setting.assert_not_awaited()

    response = await client.patch(
        "/admin/settings/signup_require_email_verification",
        json={"value": False, "password": PASSWORD},
    )

    assert response.status_code == 200, response.text
    op_store.set_setting.assert_awaited_once_with(
        "signup_require_email_verification", "False", "bool", "admin@example.com"
    )
    assert PASSWORD not in repr(audit.await_args_list)


async def test_wrong_passwords_count_against_the_sign_in_limits(client, console_admin):
    for _ in range(5):
        response = await client.patch(
            "/admin/settings/log_full_payload", json={"value": True, "password": "guess"}
        )
        assert response.status_code == 400

    limited = await client.patch(
        "/admin/settings/log_full_payload", json={"value": True, "password": PASSWORD}
    )
    assert limited.status_code == 429


async def test_a_refused_value_spends_no_password_attempt(client, op_store, console_admin):
    for _ in range(10):
        response = await client.patch(
            "/admin/settings/user_concurrency_free", json={"value": 0, "password": "guess"}
        )
        assert response.status_code == 400
        assert "below min" in response.json()["detail"]

    ok = await client.patch(
        "/admin/settings/user_concurrency_free", json={"value": 4, "password": PASSWORD}
    )
    assert ok.status_code == 200


async def test_an_authenticator_code_confirms_and_the_password_is_not_asked(
    client, op_store, console_admin, monkeypatch
):
    monkeypatch.setattr(reauthentication, "confirmed_by_two_factor", AsyncMock(return_value=True))

    response = await client.patch("/admin/settings/signup_enabled", json={"value": False})

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once()


async def test_a_session_that_owes_a_code_is_asked_for_one(
    client, op_store, console_admin, monkeypatch
):
    async def owes_code(request, store, *, action):
        raise TwoFactorRequiredError("stale", action)

    monkeypatch.setattr(reauthentication, "confirmed_by_two_factor", owes_code)

    response = await client.patch(
        "/admin/settings/user_auth_enabled", json={"value": False, "password": PASSWORD}
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "TWO_FACTOR_REQUIRED"
    op_store.set_setting.assert_not_awaited()


async def test_a_single_sign_on_account_confirms_with_a_fresh_sign_in_of_this_session(
    client, op_store, console_admin
):
    console_admin["password_hash"] = None
    op_store.list_user_identities = AsyncMock(return_value=[{"id": "ident-1"}])

    refused = await client.patch("/admin/settings/signup_enabled", json={"value": False})
    assert refused.status_code == 400
    assert "organization's sign-in" in refused.json()["detail"]
    op_store.consume_sso_reauth_proof.assert_awaited_with(
        user_id="u-admin", sid="sid-1", purpose="admin_action", max_age_seconds=300
    )
    op_store.set_setting.assert_not_awaited()

    op_store.consume_sso_reauth_proof = AsyncMock(return_value=True)
    response = await client.patch("/admin/settings/signup_enabled", json={"value": False})

    assert response.status_code == 200, response.text
    op_store.set_setting.assert_awaited_once()


async def test_an_account_with_neither_password_nor_identity_is_told_to_set_a_password(
    client, op_store, console_admin
):
    console_admin["password_hash"] = None

    response = await client.patch("/admin/settings/signup_enabled", json={"value": False})

    assert response.status_code == 400
    assert "this account has none" in response.json()["detail"]


async def test_several_settings_are_confirmed_once(client, op_store, audit, console_admin):
    response = await client.patch(
        "/admin/settings",
        json={
            "values": {
                "user_daily_quota_free": 5.0,
                "user_daily_quota_pro": 50.0,
                "user_daily_quota_internal": 500.0,
                "user_daily_quota_admin": 900.0,
                "analytics_growth_rate_enabled": True,
            },
            "password": PASSWORD,
        },
    )

    assert response.status_code == 200, response.text
    assert [item["key"] for item in response.json()["settings"]] == [
        "user_daily_quota_free",
        "user_daily_quota_pro",
        "user_daily_quota_internal",
        "user_daily_quota_admin",
        "analytics_growth_rate_enabled",
    ]
    # One write of all five, then one audit row each.
    op_store.set_settings.assert_awaited_once()
    assert len(op_store.set_settings.await_args.args[0]) == 5
    assert audit.await_count == 5
    # One confirmation: four more saves like it fit in the sign-in window.
    for _ in range(4):
        again = await client.patch(
            "/admin/settings",
            json={"values": {"user_daily_quota_free": 6.0}, "password": PASSWORD},
        )
        assert again.status_code == 200


async def test_a_batch_with_a_refused_value_writes_nothing(client, op_store, console_admin):
    response = await client.patch(
        "/admin/settings",
        json={
            "values": {"user_daily_quota_free": 5.0, "user_concurrency_free": 0},
            "password": PASSWORD,
        },
    )

    assert response.status_code == 400
    op_store.set_settings.assert_not_awaited()


async def test_the_admin_token_batch_with_a_guarded_setting_writes_nothing(client, op_store):
    response = await client.patch(
        "/admin/settings",
        headers=ADMIN_TOKEN,
        json={"values": {"analytics_growth_rate_enabled": True, "signup_enabled": False}},
    )

    assert response.status_code == 403
    op_store.set_settings.assert_not_awaited()


async def test_the_admin_token_batch_of_display_switches_is_written(client, op_store):
    response = await client.patch(
        "/admin/settings",
        headers=ADMIN_TOKEN,
        json={"values": {"analytics_growth_rate_enabled": True}},
    )

    assert response.status_code == 200
    op_store.set_settings.assert_awaited_once()


@pytest.fixture
def cny_ledger(monkeypatch):
    from decimal import Decimal

    from serving import accounting

    monkeypatch.setattr(
        accounting,
        "_policy",
        accounting.AccountingPolicy(
            currency="CNY",
            usd_to_currency_rate=Decimal("8"),
            rate_date="2026-10-23",
            rate_source="https://example.test/fixed-rate",
        ),
    )


async def test_a_batch_stores_quotas_given_in_the_ledger_currency_in_usd(
    client, op_store, console_admin, cny_ledger
):
    response = await client.patch(
        "/admin/settings",
        json={
            "values": {"user_daily_quota_free": 40, "analytics_growth_rate_enabled": True},
            "currency": "CNY",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 200, response.text
    assert op_store.rows["user_daily_quota_free"]["value"] == "5.0"
    assert op_store.rows["analytics_growth_rate_enabled"]["value"] == "True"
    quota = response.json()["settings"][0]
    assert (quota["currency"], quota["amount"]) == ("CNY", 40.0)


async def test_a_batch_in_a_stale_currency_writes_nothing_and_spends_no_password(
    client, op_store, console_admin, cny_ledger
):
    response = await client.patch(
        "/admin/settings",
        json={"values": {"user_daily_quota_free": 5}, "currency": "USD", "password": "wrong"},
    )

    assert response.status_code == 409
    op_store.set_settings.assert_not_awaited()


async def test_a_batch_the_store_fails_part_way_changes_nothing_it_reports(
    client, op_store, audit, console_admin
):
    """Review: written one by one, a failure part way left the first values in force."""
    app = client._transport.app
    rt = app.state.services.runtime_settings
    before = rt.get_cached("user_daily_quota_free")
    op_store.set_settings.side_effect = ConnectionError("database went away")

    with pytest.raises(ConnectionError):
        await client.patch(
            "/admin/settings",
            json={
                "values": {"user_daily_quota_free": 5.0, "user_daily_quota_pro": 50.0},
                "password": PASSWORD,
            },
        )

    # Nothing is cached or audited as changed, and nothing is half stored.
    assert rt.get_cached("user_daily_quota_free") == before
    audit.assert_not_awaited()
    assert op_store.rows == {}
    op_store.set_setting.assert_not_awaited()
