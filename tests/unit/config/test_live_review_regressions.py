"""Regressions from an independent review of the live configuration protocol.

Each reproduced a defect in an earlier draft: a component's exception text in
the log, a late candidate never discarded, a detector send or recovery that
outlived its configuration, an acknowledgement of an older report, and a
delayed read undoing a newer contract.
"""

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from serving.config import app_config, instances, live
from serving.observability import alert_runtime, alerts
from serving.observability.alert_runtime import AlertRuntimeConfig
from tests.fixtures.app_config_store import FakeAppConfigStore
from tests.unit.config.test_live import KEYS, Component, snapshot

# A fixture the tests below request by name; imported for pytest, not called.
from tests.unit.observability.test_alert_runtime import (
    SLACK_A,
    SLACK_B,
    boot,
    config_env,  # noqa: F401
    publish,
)


@pytest.mark.usefixtures("config_env")
def test_reset_clears_a_publication_without_loading_database(monkeypatch):
    from serving.observability.alert_config import _expand_env

    # Delivery tests can publish directly, without ever loading a config store.
    publish(AlertRuntimeConfig())
    # Published without raw values: every key is unset, served the caller's default.
    assert app_config.config_value("FAILED_REQUEST_ALERT_THRESHOLD") is None
    assert app_config.config_value("FAILED_REQUEST_ALERT_THRESHOLD", "9") == "9"

    app_config.reset_state()

    assert _expand_env("${FAILED_REQUEST_ALERT_THRESHOLD}") == "200"
    assert _expand_env("${FAILED_REQUEST_ALERT_THRESHOLD:-9}") == "9"
    monkeypatch.setenv("FAILED_REQUEST_ALERT_THRESHOLD", "40")
    assert _expand_env("${FAILED_REQUEST_ALERT_THRESHOLD}") == "40"


@pytest.mark.asyncio
async def test_unexpected_projection_exception_does_not_log_its_secret(caplog):
    secret = "SYNTHETIC-SECRET-IN-PROJECTION"

    class Broken(Component):
        def project(self, snap):
            raise ValueError(secret)

    group = live.LiveGroup("test.group", schema_versions=(1,), keys=KEYS, component=Broken())
    group.install_baseline()
    group.submit(snapshot(1))
    group.start()
    await asyncio.sleep(0.02)
    await group.stop(time.monotonic() + 0.1)
    assert group.acquire().generation == 0
    assert group.status.error_code == "internal_error"
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_late_cancel_resistant_candidate_is_eventually_discarded():
    release = asyncio.Event()
    created = asyncio.Event()
    discarded = asyncio.Event()

    class Late(Component):
        async def prepare(self, desired, active):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            created.set()
            return "candidate"

        async def discard(self, candidate):
            discarded.set()

    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=Late(),
        prepare_timeout=0.01,
        discard_timeout=0.01,
        max_retained=1,
    )
    group.install_baseline()
    group.submit(snapshot(1))
    await group.run_once()
    release.set()
    await asyncio.wait_for(created.wait(), timeout=0.1)
    await asyncio.sleep(0.02)
    assert discarded.is_set(), "A candidate completed after cancellation and was never discarded"


@pytest.mark.asyncio
@pytest.mark.usefixtures("config_env")
async def test_detector_send_is_fenced_again_after_async_delivery_preparation(monkeypatch):
    publish(AlertRuntimeConfig(slack_webhook_url=SLACK_A))
    expected = alert_runtime.acquire()

    async def changed_while_checking_mute(key):
        publish(AlertRuntimeConfig(slack_webhook_url=SLACK_B))
        return False

    post = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "_type_muted", changed_while_checking_mute)
    monkeypatch.setattr(alerts, "_post_to_slack", post)
    sent = await alerts.alert_slack(
        alerts.AlertSeverity.ERROR,
        "synthetic review",
        {},
        dedupe_key="review-fence",
        fence=lambda: alert_runtime.is_current(expected),
    )
    assert not sent
    post.assert_not_awaited()
    assert "review-fence" not in alerts._LAST_FIRED


@pytest.mark.asyncio
@pytest.mark.usefixtures("config_env")
async def test_recovery_keeps_its_generation_fence(monkeypatch):
    publish(AlertRuntimeConfig(slack_webhook_url=SLACK_A))
    expected = alert_runtime.acquire()

    def fence():
        return alert_runtime.is_current(expected)

    alerts._STATE_TRANSITIONS.observe("review-recovery", breached=True, now=100)
    in_flight = asyncio.Event()
    alerts._IN_FLIGHT["review-recovery"] = in_flight
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "_post_to_slack", post)
    recovery = asyncio.create_task(
        alerts.alert_on_transition(
            key="review-recovery",
            breached=False,
            severity=alerts.AlertSeverity.INFO,
            title="review",
            context=dict,
            cooldown_sec=0,
            kind="state",
            now=101,
            fence=fence,
        )
    )
    await asyncio.sleep(0.01)
    publish(AlertRuntimeConfig(slack_webhook_url=SLACK_B))
    alerts._IN_FLIGHT.pop("review-recovery")
    in_flight.set()
    assert not await recovery
    post.assert_not_awaited()
    assert "review-recovery" not in alerts._PENDING_RESOLUTIONS


@pytest.mark.asyncio
async def test_old_report_ack_does_not_confirm_a_newer_local_status():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Connection:
        async def fetchval(self, statement, *args):
            entered.set()
            await release.wait()
            return args[5]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Connection()

    reporter = instances.InstanceReporter(Pool(), "review-instance", datetime.now(UTC), {})
    reporter.set_group("test.group", {"state": "applied", "reconciled_revision": 1})
    report = asyncio.create_task(reporter.report_once())
    await entered.wait()
    reporter.set_group("test.group", {"state": "applied", "reconciled_revision": 2})
    release.set()
    assert await report
    assert not reporter.acknowledged


@pytest.mark.asyncio
@pytest.mark.usefixtures("config_env")
async def test_delayed_read_cannot_undo_a_newer_contract_snapshot(monkeypatch):
    store = FakeAppConfigStore()
    await boot(store)
    older = await store.fetch_snapshot()
    store.set_contract("alerts.runtime", active=True)
    newer = await store.fetch_snapshot()
    entered = asyncio.Event()
    release = asyncio.Event()
    reads = 0

    async def fetch_snapshot():
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
            return older
        return newer

    monkeypatch.setattr(store, "fetch_snapshot", fetch_snapshot)
    delayed = asyncio.create_task(app_config.refresh())
    await entered.wait()
    await app_config.refresh()
    assert app_config.source_version() == newer.version
    release.set()
    await delayed
    assert app_config.source_version() == newer.version
    assert app_config.last_snapshot().contracts.groups["alerts.runtime"].active


class _SlowRelease(Component):
    """Discard and retire wait on a gate and record any overlap on one state."""

    def __init__(self) -> None:
        super().__init__()
        self.release_gate = asyncio.Event()
        self.releasing: set[str] = set()
        self.overlaps: list[str] = []
        self.release_started = asyncio.Event()

    async def _release(self, state, record):
        if state in self.releasing:
            self.overlaps.append(state)
        self.releasing.add(state)
        self.release_started.set()
        try:
            await self.release_gate.wait()
        finally:
            self.releasing.discard(state)
        record.append(state)

    async def discard(self, prepared):
        await self._release(prepared, self.discarded)

    async def retire(self, previous):
        await self._release(previous, self.retired)


@pytest.mark.asyncio
async def test_a_late_candidate_and_the_applier_never_release_one_state_together():
    finish = asyncio.Event()

    class Late(_SlowRelease):
        async def prepare(self, desired, active):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish.wait()
            return "late"

    component = Late()
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        prepare_timeout=0.01,
        discard_timeout=0.01,
        retire_timeout=1,
        max_retained=4,
    )
    group.install_baseline()
    group.submit(snapshot(1, a="2"))
    await group.run_once()  # Times out; the prepare is abandoned and keeps running.
    assert group.retained == 1
    finish.set()
    await asyncio.wait_for(component.release_started.wait(), timeout=1)
    # The applier comes round while the late candidate is being discarded.
    applier = asyncio.create_task(group.run_once())
    await asyncio.sleep(0.02)
    component.release_gate.set()
    await asyncio.wait_for(applier, timeout=1)
    await asyncio.sleep(0.02)
    assert component.overlaps == []
    assert component.discarded.count("late") == 1
    assert group.retained == 0


@pytest.mark.asyncio
async def test_shutdown_during_a_discard_keeps_the_candidate_and_releases_it():
    component = _SlowRelease()
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        prepare_timeout=1,
        discard_timeout=5,
        retire_timeout=1,
    )
    group.install_baseline()
    gate = asyncio.Event()
    component.gate = gate
    group.submit(snapshot(1, a="2"))
    group.start()
    await asyncio.wait_for(component.started.wait(), timeout=1)
    # A newer target arrives while the candidate for "2" is prepared: it is
    # superseded, and its discard blocks.
    group.submit(snapshot(2, a="3"))
    gate.set()
    await asyncio.wait_for(component.release_started.wait(), timeout=1)
    assert component.releasing == {"view:2"}
    # Shutdown cancels the applier in that discard; the candidate must not be
    # lost, and stop releases it within its deadline.
    stopping = asyncio.create_task(group.stop(time.monotonic() + 2))
    await asyncio.sleep(0.05)
    component.release_gate.set()
    await asyncio.wait_for(stopping, timeout=3)
    assert "view:2" in component.discarded
    assert group.retained == 0
    assert group.acquire().generation == 0


@pytest.mark.asyncio
async def test_stop_waits_for_an_abandoned_prepare_and_discards_its_candidate():
    finish = asyncio.Event()

    class Late(Component):
        async def prepare(self, desired, active):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish.wait()
            return "late"

    component = Late()
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        prepare_timeout=0.01,
        discard_timeout=0.01,
    )
    group.install_baseline()
    group.submit(snapshot(1, a="2"))
    await group.run_once()
    stopping = asyncio.create_task(group.stop(time.monotonic() + 1))
    await asyncio.sleep(0.02)
    finish.set()
    await asyncio.wait_for(stopping, timeout=2)
    assert component.discarded == ["late"]
    assert group.retained == 0
    assert group._cleanup_task is None or group._cleanup_task.done()


@pytest.mark.asyncio
@pytest.mark.usefixtures("config_env")
async def test_a_failed_first_activation_is_owned_live_and_a_corrected_save_recovers(
    monkeypatch, caplog
):
    from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP
    from tests.fixtures.app_config_store import row
    from tests.unit.observability.test_alert_runtime import entry, settle

    fixed = SLACK_A.replace("fake-slack-a", "fake-slack-fixed")
    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    component = alert_runtime.GROUP.component
    original = type(component).prepare

    async def refuse_b(self, desired, active):
        if desired.config.slack_webhook_url == SLACK_B:
            raise RuntimeError(f"cannot build {SLACK_B}")
        return await original(self, desired, active)

    monkeypatch.setattr(type(component), "prepare", refuse_b)
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await app_config.refresh()
    await settle()
    # The first publication failed: the boot value stays in use...
    assert alert_runtime.GROUP.status.state == "failed"
    assert alert_runtime.current().slack_webhook_url == SLACK_A
    assert app_config.config_value("SLACK_WEBHOOK_URL") == SLACK_A
    # ...but the active contract owns the keys: no restart is pending or offered.
    described = entry("SLACK_WEBHOOK_URL")
    assert described["apply_mode"] == "managed"
    assert described["pending_restart"] is False
    assert "SLACK_WEBHOOK_URL" not in app_config.get_config_health().pending_restart
    assert {g["name"]: g["managed"] for g in app_config.describe()["groups"]}[ALERTS_RUNTIME_GROUP]
    # A corrected save applies without a restart.
    await app_config.update({"SLACK_WEBHOOK_URL": fixed}, updated_by="admin")
    await settle()
    assert alert_runtime.GROUP.status.state == "applied"
    assert alert_runtime.current().slack_webhook_url == fixed
    assert app_config.config_value("SLACK_WEBHOOK_URL") == fixed
    assert entry("SLACK_WEBHOOK_URL")["apply_mode"] == "managed"
    assert SLACK_B not in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("config_env")
async def test_deactivation_freezes_and_returns_owned_keys_to_restart():
    from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP
    from tests.fixtures.app_config_store import row
    from tests.unit.observability.test_alert_runtime import entry, settle

    store = FakeAppConfigStore([row("SLACK_WEBHOOK_URL", SLACK_A, secret=True)])
    await boot(store)
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    await app_config.refresh()
    await settle()
    store.set_contract(ALERTS_RUNTIME_GROUP, active=False)
    await app_config.update({"SLACK_WEBHOOK_URL": SLACK_B}, updated_by="admin")
    await settle()
    # Inactive contract: the frozen state is kept, the new target waits for a restart.
    assert alert_runtime.GROUP.status.state == "dormant-frozen"
    assert alert_runtime.current().slack_webhook_url == SLACK_A
    described = entry("SLACK_WEBHOOK_URL")
    assert described["apply_mode"] == "restart"
    assert described["pending_restart"] is True


@pytest.mark.asyncio
async def test_overlapping_cleanup_wakeups_never_release_one_resource_twice():
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0
    calls = 0

    class SlowRetire(Component):
        async def retire(self, previous):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    group = live.LiveGroup("test.group", schema_versions=(1,), keys=KEYS, component=SlowRetire())
    group._retiring.append(live._Retiring("resource"))
    first = asyncio.create_task(group._retire_due())
    await entered.wait()
    second = asyncio.create_task(group._retire_due())
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(first, second)
    assert peak == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelling_discard_keeps_ownership_of_the_candidate():
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowDiscard(Component):
        async def discard(self, candidate):
            entered.set()
            await release.wait()
            self.discarded.append(candidate)

    component = SlowDiscard()
    group = live.LiveGroup("test.group", schema_versions=(1,), keys=KEYS, component=component)
    task = asyncio.create_task(group._discard("candidate"))
    await entered.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    retained = group.retained
    release.set()
    await group._retire_due()
    assert retained >= 1
    assert component.discarded == ["candidate"]


@pytest.mark.asyncio
async def test_stop_keeps_its_deadline_with_a_cancellation_resistant_release():
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    class Stubborn(Component):
        async def retire(self, previous):
            nonlocal calls
            calls += 1
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    continue  # Ignores cancellation until it is done.
            self.retired.append(previous)

    component = Stubborn()
    group = live.LiveGroup(
        "test.group", schema_versions=(1,), keys=KEYS, component=component, retire_timeout=5
    )
    group._retiring.append(live._Retiring("in use"))
    group.start()
    group._wake.set()
    await asyncio.wait_for(entered.wait(), timeout=1)
    started = time.monotonic()
    await asyncio.wait_for(group.stop(time.monotonic() + 0.2), timeout=2)
    assert time.monotonic() - started < 0.6, "stop overran its deadline"
    # Not forced, not forgotten, not started twice.
    assert group.retained == 1
    assert calls == 1
    finish.set()
    await asyncio.sleep(0.01)
    await group._retire_due()
    assert component.retired == ["in use"]
    assert calls == 1
    assert group.retained == 0


@pytest.mark.asyncio
async def test_retry_delays_stay_capped_after_very_many_failures():
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=Component(),
        backoff_initial=1.0,
        backoff_max=60.0,
    )
    for attempts in (1, 2, 7, 1025, 10**6):
        delay = group._retry_delay(attempts)
        assert 0 < delay <= 60.0
    assert group._retry_delay(1) == 1.0
    group._failures = 5000
    group._backoff("target")  # Raised OverflowError before the cap.
    assert group._retry_at - time.monotonic() <= 60.0
    retiring = live._Retiring("state", attempts=5000)

    class Failing(Component):
        async def retire(self, previous):
            raise RuntimeError("still in use")

    group.component = Failing()
    assert not await group._try_retire(retiring, timeout=1)
    assert retiring.retry_at - time.monotonic() <= 60.0
