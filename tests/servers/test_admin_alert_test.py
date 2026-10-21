"""Admin test notifications use saved/draft secrets without creating incidents."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from serving.observability import alerts
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import alerts as admin_alerts
from serving.servers.routers.anthropic_messages import (
    anthropic_aware_validation_exception_handler,
)
from tests.fixtures.app_config_store import FakeAppConfigStore, row

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": "Bearer test-admin"}
SLACK_URL = "https://hooks.slack.com/services/Tunit/Bunit/synthetic-secret"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-secret"


@pytest.fixture
async def admin_client(monkeypatch, tmp_path):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-at-least-32-characters")
    get_settings.cache_clear()
    monkeypatch.setattr(admin_alerts, "_test_send_not_before", 0.0)
    audit = AsyncMock()
    monkeypatch.setattr(admin_alerts, "log_admin_action", audit)
    post_slack = AsyncMock(return_value=True)
    post_wecom = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "_post_to_slack", post_slack)
    monkeypatch.setattr(alerts, "_post_to_wecom", post_wecom)
    alerts.reset_dedupe_state()

    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=MagicMock(),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    app.add_exception_handler(RequestValidationError, anthropic_aware_validation_exception_handler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, audit, post_slack, post_wecom
    alerts.reset_dedupe_state()
    get_settings.cache_clear()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
async def test_only_an_admin_can_send_a_test(admin_client, headers):
    client, audit, post_slack, post_wecom = admin_client

    response = await client.post(
        "/admin/alerts/test",
        headers=headers,
        json={"channel": "wecom", "webhook_url": WECOM_URL},
    )

    assert response.status_code in (401, 403)
    audit.assert_not_awaited()
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()


@pytest.mark.parametrize(
    ("channel", "key", "url"),
    [
        ("slack", "SLACK_ALERTS_WEBHOOK_URL", SLACK_URL),
        ("slack", "SLACK_WEBHOOK_URL", SLACK_URL),
        ("wecom", "WECOM_ALERTS_WEBHOOK_URL", WECOM_URL),
    ],
)
async def test_omitted_url_uses_selected_saved_secret(admin_client, channel, key, url):
    client, audit, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row(key, url, secret=True)])
    await app_config.load(store)
    app_config.attach(store)

    response = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": channel})

    assert response.status_code == 200
    result = response.json()
    assert result == {"channel": channel, "sent": True, "test_id": result["test_id"]}
    assert result["test_id"].startswith("alert-test-")
    post, other = (post_slack, post_wecom) if channel == "slack" else (post_wecom, post_slack)
    post.assert_awaited_once()
    assert post.await_args.args[0] == url
    assert post.await_args.args[1].startswith("[TEST]\n")
    assert result["test_id"] in post.await_args.args[1]
    assert post.await_args.kwargs == {"dedupe_key": result["test_id"], "record_failure": False}
    other.assert_not_awaited()
    assert audit.await_args.args[2] == "alerts.test"
    assert audit.await_args.args[4] == result
    assert audit.await_args.kwargs == {"success": True}
    assert "synthetic-secret" not in response.text + str(audit.await_args)


@pytest.mark.parametrize(
    ("old_channel", "old_key", "old_url", "new_channel", "new_key", "new_url"),
    [
        (
            "slack",
            "SLACK_ALERTS_WEBHOOK_URL",
            SLACK_URL,
            "wecom",
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL,
        ),
        (
            "wecom",
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL,
            "slack",
            "SLACK_ALERTS_WEBHOOK_URL",
            SLACK_URL,
        ),
    ],
)
async def test_saved_pending_channel_is_tested_before_restart(
    admin_client, old_channel, old_key, old_url, new_channel, new_key, new_url
):
    client, _, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row(old_key, old_url, secret=True)])
    await app_config.load(store)
    app_config.attach(store)
    await app_config.update({old_key: "", new_key: new_url}, updated_by="admin")
    assert [sink.name for sink in alerts.configured_sinks()] == [old_channel]
    assert app_config.config_value(old_key) == old_url

    response = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": new_channel})

    assert response.status_code == 200
    post = post_slack if new_channel == "slack" else post_wecom
    assert post.await_args.args[0] == new_url
    assert [sink.name for sink in alerts.configured_sinks()] == [old_channel]
    assert {old_key, new_key}.issubset(app_config.describe()["pending_restart"])


@pytest.mark.parametrize(
    ("old_key", "old_url", "new_channel", "new_key", "new_url"),
    [
        ("SLACK_ALERTS_WEBHOOK_URL", SLACK_URL, "wecom", "WECOM_ALERTS_WEBHOOK_URL", WECOM_URL),
        (
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL,
            "wecom",
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL.replace("synthetic-secret", "replacement-secret"),
        ),
    ],
)
async def test_omitted_url_refreshes_a_save_from_another_worker(
    admin_client, old_key, old_url, new_channel, new_key, new_url
):
    client, _, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row(old_key, old_url, secret=True)])
    await app_config.load(store)
    app_config.attach(store)
    # Another worker saves directly to the shared store. This worker has not
    # reached its periodic refresh, so its configured value is still stale.
    await store.write([row(old_key, "", secret=True), row(new_key, new_url, secret=True)])
    assert app_config.configured_value(new_key) != new_url

    response = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": new_channel})

    assert response.status_code == 200
    assert post_wecom.await_args.args[0] == new_url
    post_slack.assert_not_awaited()
    assert app_config.configured_value(new_key) == new_url
    assert app_config.config_value(old_key) == old_url


async def test_refresh_failure_does_not_send_to_a_stale_saved_webhook(
    admin_client, monkeypatch, caplog
):
    client, audit, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row("WECOM_ALERTS_WEBHOOK_URL", WECOM_URL, secret=True)])
    await app_config.load(store)
    app_config.attach(store)
    monkeypatch.setattr(
        store, "fetch_all", AsyncMock(side_effect=RuntimeError(f"Cannot read {WECOM_URL}"))
    )

    response = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": "wecom"})

    assert response.status_code == 503
    assert "synthetic-secret" not in response.text + str(
        [record.__dict__ for record in caplog.records]
    )
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()
    audit.assert_not_awaited()
    assert app_config.config_value("WECOM_ALERTS_WEBHOOK_URL") == WECOM_URL


async def test_unsaved_draft_bypasses_suppression_without_changing_config_or_incidents(
    admin_client, monkeypatch
):
    client, _, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row("SLACK_ALERTS_WEBHOOK_URL", SLACK_URL, secret=True)])
    await app_config.load(store)
    app_config.attach(store)
    before_rows = deepcopy(store.rows)
    alerts._LAST_FIRED["existing"] = 123.0
    alerts._ANNOUNCED.add("existing")
    alerts._FAILED_DELIVERIES["retry"] = 1
    alerts._MUTED_UNANNOUNCED.add("muted")
    before_incidents = (
        dict(alerts._LAST_FIRED),
        set(alerts._ANNOUNCED),
        dict(alerts._FAILED_DELIVERIES),
        set(alerts._MUTED_UNANNOUNCED),
        dict(alerts._IN_FLIGHT),
    )
    formal_send = AsyncMock(side_effect=AssertionError("Must not enter incident delivery"))
    monkeypatch.setattr(alerts, "alert_slack", formal_send)
    refresh = AsyncMock(side_effect=AssertionError("Draft tests do not require a config read"))
    monkeypatch.setattr(app_config, "refresh", refresh)
    monkeypatch.setattr(
        "serving.observability.alert_snooze.get_snooze_until", AsyncMock(return_value=float("inf"))
    )
    monkeypatch.setattr(
        "serving.observability.alert_mutes.is_alert_type_muted", AsyncMock(return_value=True)
    )

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": "wecom", "webhook_url": WECOM_URL}
    )

    assert response.status_code == 200
    assert post_wecom.await_args.args[0] == WECOM_URL
    post_slack.assert_not_awaited()
    formal_send.assert_not_awaited()
    refresh.assert_not_awaited()
    assert store.rows == before_rows
    assert (
        dict(alerts._LAST_FIRED),
        set(alerts._ANNOUNCED),
        dict(alerts._FAILED_DELIVERIES),
        set(alerts._MUTED_UNANNOUNCED),
        dict(alerts._IN_FLIGHT),
    ) == before_incidents


@pytest.mark.parametrize("explicit_url", [None, ""])
async def test_selected_missing_or_empty_webhook_does_not_use_other_channel(
    admin_client, monkeypatch, explicit_url
):
    client, audit, post_slack, post_wecom = admin_client
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK_URL)
    payload = {"channel": "wecom"}
    if explicit_url is not None:
        payload["webhook_url"] = explicit_url

    response = await client.post("/admin/alerts/test", headers=AUTH, json=payload)

    assert response.status_code == 400
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    ("channel", "url"),
    [
        ("slack", WECOM_URL),
        ("wecom", SLACK_URL),
        ("slack", SLACK_URL.replace("https:", "http:")),
        ("wecom", WECOM_URL.replace("qyapi.weixin.qq.com", "127.0.0.1")),
        ("wecom", WECOM_URL.replace("qyapi.weixin.qq.com", "qyapi.weixin.qq.com.evil.invalid")),
        ("slack", SLACK_URL.replace("hooks.slack.com", "hooks.slack.com:443")),
        ("slack", SLACK_URL.replace("hooks.slack.com", "user@hooks.slack.com")),
        ("slack", SLACK_URL + "#fragment"),
        ("slack", SLACK_URL.replace("/services/", "/other/")),
        ("slack", SLACK_URL.replace("Tunit", "Tunit%2F..")),
        ("wecom", WECOM_URL + "&key=second"),
        ("wecom", WECOM_URL.replace("synthetic-secret", "")),
        ("wecom", WECOM_URL.replace("key=", "other=")),
        ("wecom", WECOM_URL.replace("send?", "send/../other?")),
        ("wecom", WECOM_URL + "\n"),
    ],
)
async def test_unsafe_destinations_are_rejected_without_echoing_credentials(
    admin_client, caplog, channel, url
):
    client, audit, post_slack, post_wecom = admin_client
    caplog.set_level(logging.INFO)

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": channel, "webhook_url": url}
    )

    assert response.status_code == 400
    assert "synthetic-secret" not in response.text + str(
        [record.__dict__ for record in caplog.records]
    )
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    ("channel", "key", "url"),
    [
        ("slack", "SLACK_ALERTS_WEBHOOK_URL", SLACK_URL + "/"),
        ("slack", "SLACK_ALERTS_WEBHOOK_URL", SLACK_URL + "?source=admin"),
        ("wecom", "WECOM_ALERTS_WEBHOOK_URL", WECOM_URL.replace("send?", "send/?")),
        ("wecom", "WECOM_ALERTS_WEBHOOK_URL", WECOM_URL + "&source=admin"),
        (
            "wecom",
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL.replace("?key=", "?source=admin&key="),
        ),
    ],
)
@pytest.mark.parametrize("saved", [False, True])
async def test_official_webhooks_allow_trailing_slash_and_extra_query_parameters(
    admin_client, channel, key, url, saved
):
    client, _, post_slack, post_wecom = admin_client
    payload = {"channel": channel}
    if saved:
        store = FakeAppConfigStore([row(key, url, secret=True)])
        await app_config.load(store)
        app_config.attach(store)
    else:
        payload["webhook_url"] = url

    response = await client.post("/admin/alerts/test", headers=AUTH, json=payload)

    assert response.status_code == 200
    post = post_slack if channel == "slack" else post_wecom
    assert post.await_args.args[0] == url


@pytest.mark.parametrize(
    ("channel", "key", "url"),
    [
        ("slack", "SLACK_ALERTS_WEBHOOK_URL", SLACK_URL.replace("hooks.slack.com", "127.0.0.1")),
        (
            "wecom",
            "WECOM_ALERTS_WEBHOOK_URL",
            WECOM_URL.replace("qyapi.weixin.qq.com", "qyapi.weixin.qq.com.evil.invalid"),
        ),
    ],
)
async def test_saved_webhooks_still_reject_untrusted_destinations(admin_client, channel, key, url):
    client, audit, post_slack, post_wecom = admin_client
    store = FakeAppConfigStore([row(key, url, secret=True)])
    await app_config.load(store)
    app_config.attach(store)

    response = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": channel})

    assert response.status_code == 400
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()
    audit.assert_not_awaited()


async def test_slack_gov_webhook_is_supported(admin_client):
    client, _, post_slack, _ = admin_client
    url = SLACK_URL.replace("hooks.slack.com", "hooks.slack-gov.com")

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": "slack", "webhook_url": url}
    )

    assert response.status_code == 200
    assert post_slack.await_args.args[0] == url


@pytest.mark.parametrize(
    "payload",
    [
        {"webhook_url": WECOM_URL},
        {"channel": "other", "webhook_url": WECOM_URL},
        {"channel": "wecom", "webhook_url": {"value": WECOM_URL}},
        {"channel": "wecom", "webhook_url": WECOM_URL + "x" * 4096},
        [WECOM_URL],
    ],
)
async def test_invalid_request_validation_never_logs_or_returns_secret(
    admin_client, caplog, payload
):
    client, audit, post_slack, post_wecom = admin_client
    caplog.set_level(logging.WARNING)

    response = await client.post("/admin/alerts/test", headers=AUTH, json=payload)

    assert response.status_code == 422
    assert "synthetic-secret" not in response.text + str(
        [record.__dict__ for record in caplog.records]
    )
    post_slack.assert_not_awaited()
    post_wecom.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("raise_exception", [False, True])
async def test_delivery_failure_is_safe_and_audited(admin_client, caplog, raise_exception):
    client, audit, _, post_wecom = admin_client
    if raise_exception:
        post_wecom.side_effect = RuntimeError(f"Request to {WECOM_URL} failed")
    else:
        post_wecom.return_value = False
    caplog.set_level(logging.WARNING)

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": "wecom", "webhook_url": WECOM_URL}
    )

    assert response.status_code == 502
    assert audit.await_args.args[4]["sent"] is False
    assert audit.await_args.kwargs == {"success": False}
    assert "synthetic-secret" not in (
        response.text + str(audit.await_args) + str([record.__dict__ for record in caplog.records])
    )


@pytest.mark.parametrize(("channel", "url"), [("slack", SLACK_URL), ("wecom", WECOM_URL)])
async def test_failed_tests_disable_live_delivery_failure_accounting(
    admin_client, monkeypatch, channel, url
):
    client, _, post_slack, post_wecom = admin_client
    counters = {"503": 7}
    monkeypatch.setattr(alerts, "_DELIVERY_FAILURES_TOTAL", counters)

    async def reject(_url, _message, *, dedupe_key, record_failure=True):
        if record_failure:
            counters["503"] += 1
        return False

    post = post_slack if channel == "slack" else post_wecom
    post.side_effect = reject

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": channel, "webhook_url": url}
    )

    assert response.status_code == 502
    assert post.await_args.kwargs["record_failure"] is False
    assert counters == {"503": 7}


async def test_audit_failure_does_not_misreport_a_delivered_test(admin_client, caplog):
    client, audit, _, post_wecom = admin_client
    audit.side_effect = RuntimeError("audit unavailable")

    response = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": "wecom", "webhook_url": WECOM_URL}
    )

    assert response.status_code == 200
    assert response.json()["sent"] is True
    post_wecom.assert_awaited_once()
    assert "alert test audit failed" in caplog.text


async def test_repeated_tests_are_throttled_then_resume(admin_client, monkeypatch):
    client, audit, _, post_wecom = admin_client
    monkeypatch.setattr(admin_alerts, "time", MagicMock(monotonic=lambda: 100.0))
    payload = {"channel": "wecom", "webhook_url": WECOM_URL}

    first = await client.post("/admin/alerts/test", headers=AUTH, json=payload)
    again = await client.post("/admin/alerts/test", headers=AUTH, json=payload)

    assert first.status_code == 200
    assert again.status_code == 429
    assert again.headers["Retry-After"] == "10"
    post_wecom.assert_awaited_once()
    audit.assert_awaited_once()
    monkeypatch.setattr(admin_alerts, "time", MagicMock(monotonic=lambda: 111.0))
    later = await client.post("/admin/alerts/test", headers=AUTH, json=payload)
    assert later.status_code == 200
    assert later.json()["test_id"] != first.json()["test_id"]
    assert post_wecom.await_count == 2


async def test_throttled_saved_test_never_refreshes_configuration(admin_client, monkeypatch):
    client, _, _, post_wecom = admin_client
    refresh = AsyncMock(side_effect=AssertionError("Throttled requests must not read config"))
    monkeypatch.setattr(app_config, "refresh", refresh)
    first = await client.post(
        "/admin/alerts/test", headers=AUTH, json={"channel": "wecom", "webhook_url": WECOM_URL}
    )

    again = await client.post("/admin/alerts/test", headers=AUTH, json={"channel": "wecom"})

    assert first.status_code == 200
    assert again.status_code == 429
    refresh.assert_not_awaited()
    post_wecom.assert_awaited_once()


async def test_limit_is_reserved_while_a_saved_test_awaits_configuration(admin_client, monkeypatch):
    client, _, _, post_wecom = admin_client
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_refresh():
        started.set()
        await release.wait()

    refresh = AsyncMock(side_effect=blocking_refresh)
    monkeypatch.setattr(app_config, "refresh", refresh)
    monkeypatch.setenv("WECOM_ALERTS_WEBHOOK_URL", WECOM_URL)
    first_task = asyncio.create_task(
        client.post("/admin/alerts/test", headers=AUTH, json={"channel": "wecom"})
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        concurrent = await client.post(
            "/admin/alerts/test", headers=AUTH, json={"channel": "wecom"}
        )
        assert concurrent.status_code == 429
        refresh.assert_awaited_once()
        post_wecom.assert_not_awaited()
    finally:
        release.set()
        first = await asyncio.wait_for(first_task, timeout=2)

    assert first.status_code == 200
    post_wecom.assert_awaited_once()
