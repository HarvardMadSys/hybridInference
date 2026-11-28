"""The admin test email: the gateway's fixed message, through the saved settings.

It is refused, before anything is sent, when the saved settings cannot send
or there is nowhere to send it; a refusal names what to fix and does not hold
up the next test. A send the transport refuses comes back with its reason.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings, settings
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import email_test
from serving.utils.delivery import EmailDelivery

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": "Bearer test-admin"}
ACCOUNT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def cloudflare(monkeypatch):
    """Saved settings that can send: Cloudflare Email Sending, fully configured."""
    monkeypatch.setattr(settings, "email_provider", "cloudflare")
    monkeypatch.setattr(settings, "cloudflare_account_id", ACCOUNT_ID)
    monkeypatch.setattr(settings, "cloudflare_email_api_token", "synthetic-token")


@pytest.fixture
async def admin_client(monkeypatch):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-at-least-32-characters")
    get_settings.cache_clear()
    for name, value in {
        "email_provider": "smtp",
        "smtp_host": "",
        "smtp_user": "",
        "smtp_password": "",
        "cloudflare_account_id": "",
        "cloudflare_email_api_token": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(email_test, "_test_send_not_before", {})
    refresh = AsyncMock()
    monkeypatch.setattr(app_config, "refresh", refresh)
    deliver = Mock(return_value=EmailDelivery(True))
    monkeypatch.setattr(email_test, "deliver_email", deliver)
    audit = AsyncMock()
    monkeypatch.setattr(email_test, "log_admin_action", audit)

    op_store = MagicMock()
    op_store.get_user_by_id = AsyncMock(return_value=None)
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, deliver, audit, refresh, op_store
    get_settings.cache_clear()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
async def test_only_an_admin_can_send_a_test(admin_client, cloudflare, headers):
    client, deliver, audit, _refresh, _op_store = admin_client

    response = await client.post("/admin/email/test", headers=headers, json={"to": "a@example.org"})

    assert response.status_code in (401, 403)
    deliver.assert_not_called()
    audit.assert_not_awaited()


async def test_the_fixed_message_goes_to_the_address_given(admin_client, cloudflare):
    client, deliver, audit, refresh, _op_store = admin_client

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "ops@example.org"})

    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "to": "ops@example.org", "provider": "cloudflare"}
    refresh.assert_awaited_once()  # the saved settings, not this worker's older copy
    [call] = deliver.call_args_list
    to, subject, html_body, text_body = call.args
    assert to == "ops@example.org"
    assert subject.startswith("Test email from ")
    assert "Cloudflare Email Sending" in text_body
    assert "Cloudflare Email Sending" in html_body
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "email.test"
    assert audit.await_args.kwargs["details"] == {
        "provider": "cloudflare",
        "to": "ops@example.org",
        "sent": True,
    }
    assert audit.await_args.kwargs["success"] is True


async def test_without_an_address_the_test_goes_to_the_admin_account(
    admin_client, cloudflare, monkeypatch
):
    client, deliver, _audit, _refresh, op_store = admin_client
    monkeypatch.setattr(email_test, "get_admin_user_id", lambda request: "u-admin")
    op_store.get_user_by_id.return_value = {"id": "u-admin", "email": "admin@example.org"}

    response = await client.post("/admin/email/test", headers=AUTH, json={})

    assert response.status_code == 200, response.text
    assert response.json()["to"] == "admin@example.org"
    op_store.get_user_by_id.assert_awaited_once_with("u-admin")
    assert deliver.call_args.args[0] == "admin@example.org"


async def test_an_account_without_an_address_is_asked_for_one(admin_client, cloudflare):
    client, deliver, audit, _refresh, _op_store = admin_client

    # The admin token has no account, so it has no address of its own.
    response = await client.post("/admin/email/test", headers=AUTH, json={})

    assert response.status_code == 400
    assert "Enter an address to send the test to" in response.json()["detail"]
    deliver.assert_not_called()
    audit.assert_not_awaited()
    # Nothing was sent, so nothing holds up the next attempt.
    retry = await client.post("/admin/email/test", headers=AUTH, json={"to": "ops@example.org"})
    assert retry.status_code == 200, retry.text


async def test_settings_that_cannot_send_are_named_before_anything_is_sent(admin_client):
    client, deliver, _audit, _refresh, _op_store = admin_client

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "SMTP needs SMTP_HOST, SMTP_USER and SMTP_PASSWORD. Save them, then send the test."
    )
    deliver.assert_not_called()
    assert email_test._test_send_not_before == {}


async def test_a_refused_send_reports_the_transports_reason(admin_client, cloudflare):
    client, deliver, audit, _refresh, _op_store = admin_client
    deliver.return_value = EmailDelivery(False, "Cloudflare did not accept the API token.")

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})

    assert response.status_code == 502
    assert response.json()["detail"] == "Cloudflare did not accept the API token."
    assert audit.await_args.kwargs["success"] is False
    assert audit.await_args.kwargs["details"]["sent"] is False


async def test_a_second_test_right_after_one_that_was_sent_waits(admin_client, cloudflare):
    client, deliver, _audit, _refresh, _op_store = admin_client

    first = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})
    second = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    assert deliver.call_count == 1


async def test_unreadable_saved_settings_send_nothing(admin_client, cloudflare):
    client, deliver, _audit, refresh, _op_store = admin_client
    refresh.side_effect = RuntimeError("database down")

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})

    assert response.status_code == 503
    assert "database down" not in response.text
    deliver.assert_not_called()


async def test_an_address_that_is_not_one_is_rejected(admin_client, cloudflare):
    client, deliver, _audit, _refresh, _op_store = admin_client

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "not an address"})

    assert response.status_code == 422
    deliver.assert_not_called()


async def test_an_audit_failure_does_not_hide_a_sent_test(admin_client, cloudflare):
    client, deliver, audit, _refresh, _op_store = admin_client
    audit.side_effect = RuntimeError("audit down")

    response = await client.post("/admin/email/test", headers=AUTH, json={"to": "a@example.org"})

    assert response.status_code == 200, response.text
    deliver.assert_called_once()
