"""A live group's published values keep where each one came from.

``config_value`` serves a group's publication in place of the boot rows. A key
nothing sets -- no row, no environment value -- must still resolve as it did
before the group applied: to the caller's default, which is how an alerts.yaml
``${VAR:-default}`` keeps its inline default. Flattening such a key into its
registered default changed that default the moment the group activated, and a
later explicit value equal to the default changed nothing at all.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from serving.config import app_config
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP
from serving.config.settings import get_settings
from serving.observability import alert_runtime
from serving.observability.alert_config import _expand_env
from tests.fixtures.app_config_store import FakeAppConfigStore, row

# A fixture the tests below request by name; imported for pytest, not called.
from tests.unit.observability.test_alert_runtime import (
    SLACK_A,
    SLACK_B,
    boot,
    config_env,  # noqa: F401
    entry,
    settle,
)

KEY = "FAILED_REQUEST_ALERT_THRESHOLD"
REFERENCE = "${FAILED_REQUEST_ALERT_THRESHOLD:-9}"

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("config_env")]


def yaml_value() -> str:
    """What an alerts.yaml reference to the threshold resolves to now."""
    return _expand_env(REFERENCE)


async def activate(store: FakeAppConfigStore, *, active: bool = True) -> None:
    store.set_contract(ALERTS_RUNTIME_GROUP, active=active)
    await app_config.refresh()
    await settle()


async def restart(store: FakeAppConfigStore) -> None:
    """Boot again on *store*, as a new process."""
    app_config.reset_state()
    get_settings.cache_clear()
    await boot(store)
    await app_config.refresh()
    await settle()


async def test_an_unset_key_keeps_the_inline_default_once_the_group_applies() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    assert yaml_value() == "9"
    assert app_config.config_value(KEY) is None

    await activate(store)

    assert alert_runtime.GROUP.managed
    assert alert_runtime.GROUP.status.state == "applied"
    assert yaml_value() == "9"
    assert app_config.config_value(KEY) is None
    assert app_config.config_value(KEY, "17") == "17"
    # The typed snapshot still has the registered default.
    assert get_settings().failed_request_alert_threshold == 200
    assert alert_runtime.current().failed_request_alert_threshold == 200


async def test_restart_adoption_keeps_the_inline_default() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    assert yaml_value() == "9"
    await app_config.refresh()
    await settle()
    # Adopted, not rebuilt: the booted state is now the managed one.
    assert alert_runtime.GROUP.managed
    assert alert_runtime.acquire().generation == 0
    assert yaml_value() == "9"

    await restart(store)

    assert alert_runtime.GROUP.managed
    assert yaml_value() == "9"


async def test_an_explicit_value_equal_to_the_default_applies_live() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await activate(store)
    before = alert_runtime.acquire()
    assert yaml_value() == "9"

    await app_config.update({KEY: 200}, updated_by="admin")
    await settle()

    # The typed configuration is the same, so nothing is rebuilt...
    after = alert_runtime.acquire()
    assert after.generation == before.generation
    assert after.view is before.view
    # ...but the value is now set, and the inline default no longer applies.
    assert app_config.config_value(KEY) == "200"
    assert yaml_value() == "200"
    assert after.version == store.version
    assert entry(KEY)["pending_restart"] is False

    # A reset returns the key to unset, and the inline default with it.
    await app_config.delete(KEY, updated_by="admin")
    await settle()
    assert app_config.config_value(KEY) is None
    assert yaml_value() == "9"
    assert alert_runtime.acquire().generation == before.generation
    assert get_settings().failed_request_alert_threshold == 200


async def test_a_stored_value_reset_after_boot_returns_to_the_inline_default() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True), row(KEY, "200")])
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await boot(store)
    await app_config.refresh()
    await settle()
    assert yaml_value() == "200"

    await app_config.delete(KEY, updated_by="admin")
    await settle()

    assert yaml_value() == "9"
    assert app_config.config_value(KEY) is None
    # The next boot agrees: the reset row is not a value.
    await restart(store)
    assert yaml_value() == "9"


async def test_an_environment_value_equal_to_the_default_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv(KEY, "200")
    get_settings.cache_clear()
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    assert yaml_value() == "200"
    await activate(store)
    assert yaml_value() == "200"
    assert app_config.config_value(KEY) == "200"


async def test_an_environment_value_not_imported_is_explicit_after_activation(
    monkeypatch,
) -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    # Set after the boot import, so it stays an environment value.
    monkeypatch.setenv(KEY, "200")
    alert_runtime.GROUP.component.reset()
    assert yaml_value() == "200"
    await activate(store)
    assert app_config.config_value(KEY) == "200"
    assert yaml_value() == "200"


async def test_explicit_empty_strings_are_values(monkeypatch) -> None:
    # An empty environment value is not imported, and stays a value.
    monkeypatch.setenv("WECOM_ALERTS_WEBHOOK_URL", "")
    get_settings.cache_clear()
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    reference = "${WECOM_ALERTS_WEBHOOK_URL:-inline}"
    assert app_config.config_value("WECOM_ALERTS_WEBHOOK_URL", "fallback") == ""
    assert _expand_env(reference) == ""
    assert _expand_env("${SLACK_ALERTS_WEBHOOK_URL:-inline}") == "inline"

    await activate(store)

    assert app_config.config_value("WECOM_ALERTS_WEBHOOK_URL", "fallback") == ""
    assert _expand_env(reference) == ""
    assert _expand_env("${SLACK_ALERTS_WEBHOOK_URL:-inline}") == "inline"

    # A stored empty string is a value too: it clears the channel on purpose.
    await app_config.update({"SLACK_WEBHOOK_URL": ""}, updated_by="admin")
    await settle()
    assert alert_runtime.GROUP.status.state == "paused"
    assert app_config.config_value("SLACK_WEBHOOK_URL", "fallback") == ""
    assert _expand_env("${SLACK_WEBHOOK_URL:-inline}") == ""


async def test_dormant_frozen_keeps_what_it_published_until_the_restart() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await activate(store)
    await activate(store, active=False)
    assert alert_runtime.GROUP.status.state == "dormant-frozen"
    assert yaml_value() == "9"
    assert entry(KEY)["pending_restart"] is False

    await app_config.update({KEY: 200}, updated_by="admin")
    await settle()

    # Frozen: the published, unset key still serves the inline default...
    assert yaml_value() == "9"
    assert app_config.configured_value(KEY) == "200"
    # ...and the restart that will change it is reported, though the typed
    # value is the same.
    assert entry(KEY)["pending_restart"] is True
    assert KEY in app_config.get_config_health().pending_restart
    assert alert_runtime.GROUP.status.pending_keys == (KEY,)

    await restart(store)
    assert alert_runtime.GROUP.status.state == "dormant"
    assert yaml_value() == "200"
    assert entry(KEY)["pending_restart"] is False


async def test_a_dormant_group_reports_a_key_set_to_its_default_pending() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await app_config.update({KEY: 200}, updated_by="admin")
    await settle()
    assert alert_runtime.GROUP.status.state == "dormant"
    assert yaml_value() == "9"
    assert entry(KEY)["pending_restart"] is True
    assert alert_runtime.GROUP.status.pending_keys == (KEY,)

    # Activation applies it without a restart.
    await activate(store)
    assert yaml_value() == "200"
    assert entry(KEY)["pending_restart"] is False


async def test_a_spelling_of_the_same_explicit_value_is_not_pending() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True), row(KEY, "200")])
    await boot(store)
    store.rows[KEY] = row(KEY, " 200")
    store.revision += 1
    await app_config.refresh()
    await settle()
    assert entry(KEY)["pending_restart"] is False


async def test_a_candidate_overtaken_by_a_provenance_change_publishes_the_newer_target() -> None:
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await activate(store)
    component = alert_runtime.GROUP.component
    prepare = component.prepare
    wrote = []

    async def prepare_then_write(desired, active):
        if not wrote:
            wrote.append(True)
            # Saved while the webhook change is being prepared: the typed
            # configuration is the same, the threshold is now set.
            await app_config.update({KEY: 200}, updated_by="admin")
        return await prepare(desired, active)

    with patch.object(component, "prepare", new=prepare_then_write):
        await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
        await settle()

    assert alert_runtime.current().slack_webhook_url == SLACK_B
    assert alert_runtime.acquire().version == store.version
    assert app_config.config_value(KEY) == "200"
    assert yaml_value() == "200"
