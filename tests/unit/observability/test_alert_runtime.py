"""The ``alerts.runtime`` group: projection, activation, and every reader on its path."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.config import app_config, live
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP, static_entries
from serving.config.settings import get_settings
from serving.observability import alert_runtime, alerts
from serving.observability.alert_runtime import AlertRuntimeConfig, AlertRuntimeTarget
from tests.fixtures.app_config_store import FakeAppConfigStore, row

SLACK_A = "https://hooks.slack.test/services/T0/B0/fake-slack-a"
SLACK_B = "https://hooks.slack.test/services/T0/B0/fake-slack-b"
WECOM = "https://qyapi.weixin.test/cgi-bin/webhook/send?key=fake-wecom"


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    for key in ("ENVIRONMENT", "DISTRIBUTION_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    alerts.reset_dedupe_state()
    alerts.reset_transition_state()
    yield tmp_path
    alerts.reset_dedupe_state()
    alerts.reset_transition_state()
    alert_runtime.set_detector_state("unavailable")


async def boot(store: FakeAppConfigStore) -> None:
    """Boot as the gateway does: load, attach, publish the baseline."""
    await app_config.load(store)
    app_config.attach(store)
    live.COORDINATOR.install_baselines()


async def settle() -> None:
    """Run the group's applier on whatever ``app_config`` last handed it."""
    await alert_runtime.GROUP.run_once()


def entry(key: str) -> dict:
    return next(item for item in app_config.describe()["entries"] if item["key"] == key)


def publish(config: AlertRuntimeConfig) -> None:
    alert_runtime.GROUP.publish(config, AlertRuntimeTarget(config), None)


# --- the configuration value ----------------------------------------------------


def test_the_webhooks_never_appear_in_a_repr() -> None:
    config = AlertRuntimeConfig(slack_webhook_url=SLACK_A, wecom_alerts_webhook_url="")
    assert SLACK_A not in repr(config)
    assert config.channel == "slack"


@pytest.mark.parametrize(
    ("webhooks", "channel", "detector"),
    [
        ({}, "none", False),
        ({"slack_webhook_url": "  "}, "none", False),
        ({"slack_alerts_webhook_url": SLACK_A}, "slack", False),
        ({"slack_webhook_url": SLACK_A, "slack_alerts_webhook_url": SLACK_A}, "slack", True),
        ({"wecom_alerts_webhook_url": WECOM}, "wecom", True),
        (
            {"slack_alerts_webhook_url": SLACK_A, "wecom_alerts_webhook_url": WECOM},
            "conflict",
            True,
        ),
    ],
)
def test_channel_and_detector_predicates(webhooks, channel, detector) -> None:
    config = AlertRuntimeConfig(**webhooks)
    assert config.channel == channel
    # The detector's own switch: SLACK_WEBHOOK_URL or WeCom, never the alerting URL alone.
    assert config.detector_enabled is detector


@pytest.mark.asyncio
async def test_the_baseline_is_what_the_process_booted_with(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SLACK_A)
    monkeypatch.setenv("FAILED_REQUEST_ALERT_THRESHOLD", "7")
    get_settings.cache_clear()
    await boot(FakeAppConfigStore())
    config = alert_runtime.current()
    # Settings' fallback copies the legacy Slack URL into the alerting one.
    assert config.slack_alerts_webhook_url == SLACK_A
    assert config.failed_request_alert_threshold == 7
    assert alert_runtime.acquire().generation == 0


# --- dormant until activated -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_dormant_group_keeps_the_boot_value_pending_a_restart(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    await settle()
    assert alert_runtime.GROUP.status.state == "dormant"
    assert alert_runtime.current().slack_webhook_url == SLACK_A
    assert app_config.config_value("SLACK_WEBHOOK_URL") == SLACK_A
    assert app_config.configured_value("SLACK_WEBHOOK_URL") == SLACK_B
    described = entry("SLACK_WEBHOOK_URL")
    assert described["apply_mode"] == "restart"
    assert described["pending_restart"] is True
    # Names only, so activation can be previewed without revealing the secret.
    assert alert_runtime.GROUP.status.pending_keys == ("SLACK_WEBHOOK_URL",)


@pytest.mark.asyncio
async def test_activation_applies_what_was_saved_while_dormant(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    await settle()
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await app_config.refresh()
    await settle()
    assert alert_runtime.current().slack_webhook_url == SLACK_B
    assert alert_runtime.GROUP.status.state == "applied"
    assert entry("SLACK_WEBHOOK_URL")["pending_restart"] is False
    assert entry("SLACK_WEBHOOK_URL")["apply_mode"] == "managed"


# --- the protocol's ordering -----------------------------------------------------


@pytest.mark.asyncio
async def test_settings_and_config_value_change_only_after_publication(config_env) -> None:
    store = FakeAppConfigStore(
        [row("SLACK_WEBHOOK_URL", SLACK_A, secret=True), row("FAILED_REQUEST_ALERT_THRESHOLD", "5")]
    )
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    assert alert_runtime.GROUP.status.state == "applied"
    before = alert_runtime.acquire()

    await app_config.update(
        {"SLACK_WEBHOOK_URL": SLACK_B, "FAILED_REQUEST_ALERT_THRESHOLD": 9}, updated_by="admin"
    )
    # Saved, handed to the group, not yet published: nothing reads the new value.
    assert app_config.configured_value("SLACK_WEBHOOK_URL") == SLACK_B
    assert app_config.config_value("SLACK_WEBHOOK_URL") == SLACK_A
    assert get_settings().slack_webhook_url == SLACK_A
    assert get_settings().failed_request_alert_threshold == 5
    assert alert_runtime.current() is before.view

    await settle()
    after = alert_runtime.acquire()
    assert after.generation == before.generation + 1
    # One value: the webhook and the threshold changed together.
    assert after.view.slack_webhook_url == SLACK_B
    assert after.view.failed_request_alert_threshold == 9
    assert app_config.config_value("SLACK_WEBHOOK_URL") == SLACK_B
    assert get_settings().slack_webhook_url == SLACK_B
    assert get_settings().failed_request_alert_threshold == 9
    status = alert_runtime.GROUP.status
    assert status.reconciled_revision == store.revision
    assert status.detail["channel"] == "slack"


@pytest.mark.asyncio
async def test_a_conflicting_pair_written_outside_the_api_is_refused_whole(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_ALERTS_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    # Plain SQL, which the API's own validation never saw.
    store.rows["WECOM_ALERTS_WEBHOOK_URL"] = row("WECOM_ALERTS_WEBHOOK_URL", WECOM, secret=True)
    store.revision += 1
    await app_config.refresh()
    await settle()
    status = alert_runtime.GROUP.status
    assert status.state == "invalid"
    assert status.error_code in {"rule_broken", "channels_conflict", "value_refused"}
    assert "WECOM_ALERTS_WEBHOOK_URL" in status.error_keys
    assert alert_runtime.current().channel == "slack"
    assert WECOM not in json.dumps(status.as_report())


@pytest.mark.asyncio
async def test_an_out_of_range_stored_value_is_invalid_for_the_whole_group(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    store.rows["FAILED_REQUEST_ALERT_RATE"] = row("FAILED_REQUEST_ALERT_RATE", "7.5")
    store.rows["SLACK_WEBHOOK_URL"] = row("SLACK_WEBHOOK_URL", SLACK_B, secret=True)
    store.revision += 1
    await app_config.refresh()
    await settle()
    status = alert_runtime.GROUP.status
    assert status.state == "invalid"
    assert status.error_keys == ("FAILED_REQUEST_ALERT_RATE",)
    # Not half-applied: the valid webhook in the same target waits too.
    assert alert_runtime.current().slack_webhook_url == SLACK_A


@pytest.mark.asyncio
async def test_a_stored_channel_wins_over_a_conflicting_environment_one(
    config_env, monkeypatch
) -> None:
    # Stored first, then the environment gained the other channel: the stored
    # value wins and the environment's is set aside, as at boot.
    monkeypatch.setenv("WECOM_ALERTS_WEBHOOK_URL", WECOM)
    get_settings.cache_clear()
    store = FakeAppConfigStore([row("SLACK_ALERTS_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    assert alert_runtime.GROUP.status.state == "applied"
    config = alert_runtime.current()
    assert config.channel == "slack"
    assert config.wecom_alerts_webhook_url == ""


@pytest.mark.asyncio
async def test_clearing_the_channel_pauses_delivery(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    await app_config.update({"SLACK_WEBHOOK_URL": ""}, updated_by="admin")
    await settle()
    assert alert_runtime.GROUP.status.state == "paused"
    assert alerts.configured_sink() is None
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        sent = await alerts.alert_slack(alerts.AlertSeverity.ERROR, "t", {}, dedupe_key="k")
    assert sent is False
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_reported_or_listed_carries_a_webhook(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    await settle()
    shown = json.dumps(
        {
            "local": live.COORDINATOR.local_status(),
            "describe": app_config.describe(),
            "status": alert_runtime.GROUP.status.as_report(),
        },
        default=str,
    )
    for secret in (SLACK_A, SLACK_B, "fake-slack"):
        assert secret not in shown


# --- the delivery path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_send_uses_the_channel_in_force_at_its_admission(config_env) -> None:
    publish(AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_A))
    posted: list[str] = []

    async def post(url, message, *, dedupe_key=""):
        posted.append(url)
        return True

    # The mute check awaits before admission; the channel changes meanwhile.
    async def muted(_key):
        publish(AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_B))
        return False

    with (
        patch.object(alerts, "_post_to_slack", new=post),
        patch.object(alerts, "_type_muted", new=muted),
    ):
        assert await alerts.alert_slack(alerts.AlertSeverity.ERROR, "t", {}, dedupe_key="k1")
        assert await alerts.alert_slack(alerts.AlertSeverity.ERROR, "t", {}, dedupe_key="k2")
    # The first send was admitted (and its channel read) before the change.
    assert posted == [SLACK_A, SLACK_B]


@pytest.mark.asyncio
async def test_a_fenced_send_is_dropped_without_arming_a_cooldown(config_env) -> None:
    publish(AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_A))
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        sent = await alerts.alert_slack(
            alerts.AlertSeverity.ERROR, "t", {}, dedupe_key="fenced", fence=lambda: False
        )
    assert sent is False
    post.assert_not_awaited()
    assert "fenced" not in alerts._LAST_FIRED


@pytest.mark.asyncio
async def test_a_retried_recovery_goes_to_the_channel_configured_then(config_env) -> None:
    publish(AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_A))
    posted: list[tuple[str, str]] = []
    outcome = {"ok": True}

    async def post(url, message, *, dedupe_key=""):
        posted.append((url, message.splitlines()[0]))
        return outcome["ok"]

    with patch.object(alerts, "_post_to_slack", new=post):
        await alerts.alert_on_transition(
            key="r",
            breached=True,
            severity=alerts.AlertSeverity.ERROR,
            title="T",
            context=dict,
            cooldown_sec=0,
            kind="state",
        )
        outcome["ok"] = False
        await alerts.alert_on_transition(
            key="r",
            breached=False,
            severity=alerts.AlertSeverity.ERROR,
            title="T",
            context=dict,
            cooldown_sec=0,
            kind="state",
        )
        # The channel moves; the queued recovery follows it, and no old
        # webhook is kept for it.
        publish(AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_B))
        outcome["ok"] = True
        await alerts.sweep_stale_breaches()
    assert [url for url, _ in posted] == [SLACK_A, SLACK_A, SLACK_B]
    assert "Recovered" in posted[-1][1]


# --- the failed-request detector ---------------------------------------------------


def _pool(count: int = 500):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=count)
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def _detector_config(**overrides) -> AlertRuntimeConfig:
    values = {
        "slack_webhook_url": SLACK_A,
        "slack_alerts_webhook_url": SLACK_A,
        "failed_request_alert_threshold": 10,
        "failed_request_alert_rate": 0.0,
        "failed_request_alert_cooldown_minutes": 5,
    }
    values.update(overrides)
    return AlertRuntimeConfig(**values)


@pytest.mark.asyncio
async def test_a_check_overtaken_by_a_change_during_its_query_has_no_effect(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(_detector_config())
    pool = _pool()

    async def slow_count(*_args):
        publish(_detector_config(failed_request_alert_threshold=10_000))
        return 500

    pool.fetchval = AsyncMock(side_effect=slow_count)
    alerter = FailedRequestAlerter(pool)
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        await alerter.run_check()
    post.assert_not_awaited()
    assert alerter.superseded_checks == 1
    assert alerter._last_alert_at is None
    assert not alerts._TRANSITIONS.is_firing("failed_request_rate_db")


@pytest.mark.asyncio
async def test_a_check_overtaken_during_its_breakdown_sends_nothing(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(_detector_config())
    pool = _pool()

    async def slow_breakdown(*_args):
        publish(_detector_config(slack_webhook_url=SLACK_B, slack_alerts_webhook_url=SLACK_B))
        return None

    pool.fetchrow = AsyncMock(side_effect=slow_breakdown)
    alerter = FailedRequestAlerter(pool)
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        await alerter.run_check()
    post.assert_not_awaited()
    assert alerter._last_alert_at is None
    assert not alerts._TRANSITIONS.is_firing("failed_request_rate_db")


@pytest.mark.asyncio
async def test_the_cooldown_and_incident_carry_over_a_configuration_change(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(_detector_config())
    alerter = FailedRequestAlerter(_pool())
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        await alerter.run_check()
        assert post.await_count == 1
        fired_at = alerter._last_alert_at
        # A new threshold and channel: the same detector instance, the same cooldown.
        publish(
            _detector_config(
                failed_request_alert_threshold=20,
                slack_webhook_url=SLACK_B,
                slack_alerts_webhook_url=SLACK_B,
            )
        )
        await alerter.run_check()
    assert post.await_count == 1
    assert alerter._last_alert_at == fired_at
    assert alerts._TRANSITIONS.is_firing("failed_request_rate_db")


@pytest.mark.asyncio
async def test_a_paused_detector_starts_when_its_webhook_is_published(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(AlertRuntimeConfig())
    pool = _pool()
    alerter = FailedRequestAlerter(pool)
    await alerter.run_check()
    pool.fetchval.assert_not_awaited()
    assert alerter.last_state == "paused"
    publish(_detector_config())
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        await alerter.run_check()
    assert alerter.last_state == "running"
    pool.fetchval.assert_awaited()
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_pausing_sends_no_recovery_for_an_open_incident(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(_detector_config())
    pool = _pool()
    alerter = FailedRequestAlerter(pool)
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)) as post:
        await alerter.run_check()
        publish(_detector_config(slack_webhook_url="", slack_alerts_webhook_url=""))
        pool.fetchval = AsyncMock(return_value=0)
        await alerter.run_check()
    assert post.await_count == 1
    pool.fetchval.assert_not_awaited()
    # The incident stays open, to close on the next real evaluation.
    assert alerts._TRANSITIONS.is_firing("failed_request_rate_db")


@pytest.mark.asyncio
async def test_registration_reports_the_detector_in_the_group_status(config_env) -> None:
    from serving.admin import failed_request_alerter

    publish(_detector_config())
    with patch.object(failed_request_alerter, "get_scheduler", return_value=MagicMock()):
        failed_request_alerter.register_alerter_job(_pool())
    assert alert_runtime.GROUP.status.detail["detector"] == "running"
    with patch.object(failed_request_alerter, "get_scheduler", return_value=None):
        failed_request_alerter.register_alerter_job(_pool())
    assert alert_runtime.GROUP.status.detail["detector"] == "unavailable"


@pytest.mark.asyncio
async def test_the_group_applies_through_its_own_task(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    live.COORDINATOR.start(None)
    try:
        await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
        for _ in range(200):
            if alert_runtime.current().slack_webhook_url == SLACK_B:
                break
            await asyncio.sleep(0.01)
        assert alert_runtime.current().slack_webhook_url == SLACK_B
    finally:
        await live.COORDINATOR.stop(timeout=2)


@pytest.mark.asyncio
async def test_an_unrelated_save_keeps_the_published_mirror(config_env) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    await settle()
    assert get_settings().slack_webhook_url == SLACK_B
    # Another setting's save re-runs the overlay; the group's fields are not
    # the overlay's to set, so it neither returns them to the boot values...
    await app_config.update({"SITE_NAME": "Elsewhere"}, updated_by="admin")
    assert get_settings().slack_webhook_url == SLACK_B
    assert app_config.config_value("SLACK_WEBHOOK_URL") == SLACK_B
    # ...nor does a deactivation (dormant-frozen) fall back to them.
    store.set_contract(ALERTS_RUNTIME_GROUP, active=False)
    await app_config.refresh()
    await settle()
    assert alert_runtime.GROUP.status.state == "dormant-frozen"
    await app_config.update({"SITE_NAME": "Again"}, updated_by="admin")
    assert get_settings().slack_webhook_url == SLACK_B
    assert alert_runtime.current().slack_webhook_url == SLACK_B


@pytest.mark.asyncio
async def test_a_healthy_check_overtaken_while_closing_keeps_the_cooldown(config_env) -> None:
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    publish(_detector_config())
    pool = _pool()
    alerter = FailedRequestAlerter(pool)
    with patch.object(alerts, "_post_to_slack", new=AsyncMock(return_value=True)):
        await alerter.run_check()
    fired_at = alerter._last_alert_at
    assert fired_at is not None
    pool.fetchval = AsyncMock(return_value=0)

    async def changed_during_close(*_args, **_kwargs):
        publish(_detector_config(failed_request_alert_threshold=1))
        return False

    with patch(
        "serving.admin.failed_request_alerter.alert_on_transition", new=changed_during_close
    ):
        await alerter.run_check()
    assert alerter._last_alert_at == fired_at
