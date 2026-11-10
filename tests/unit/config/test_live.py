"""The live configuration applier: prepare, publish, discard, retire, and what it reports."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from serving.config import live, revisions
from serving.config.app_config_store import ConfigRow

KEYS = ("A", "B")


def snapshot(
    revision: int,
    *,
    a: str | None = "1",
    z: str | None = None,
    active: bool = True,
    epoch: str = "epoch-one",
    schema_version: int = 1,
    keys: tuple[str, ...] = KEYS,
    contract: bool = True,
) -> revisions.SourceSnapshot:
    rows: dict[str, ConfigRow] = {}
    if a is not None:
        rows["A"] = ConfigRow("A", a)
    if z is not None:
        rows["Z"] = ConfigRow("Z", z)
    groups = {}
    if contract:
        groups["test.group"] = revisions.GroupContract(
            "test.group", schema_version, keys, active=active
        )
    return revisions.SourceSnapshot(
        source="app_config",
        version=revisions.SourceVersion(epoch, revision),
        contracts=revisions.Contracts(groups),
        rows=rows,
    )


class Component:
    """Projects key A; ``bad`` is invalid, ``boom`` fails to prepare."""

    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.discarded: list[str] = []
        self.retired: list[str] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.retire_failures = 0
        self.prepare_hang = False

    def baseline(self) -> tuple[str, str]:
        return "boot", "view:boot"

    def project(self, snap: revisions.SourceSnapshot) -> str:
        row = snap.rows.get("A")
        value = row.value if row is not None else "boot"
        if value == "bad":
            raise live.InvalidConfig("value_invalid", ("A",))
        return value

    def same(self, desired: str, active: str) -> bool:
        return desired == active

    async def prepare(self, desired: str, active: str) -> str:
        self.prepared.append(desired)
        self.started.set()
        if self.prepare_hang:
            await asyncio.Event().wait()
        if self.gate is not None:
            await self.gate.wait()
        if desired == "boom":
            raise RuntimeError("secret-looking failure https://hooks.example/abc")
        return f"view:{desired}"

    async def discard(self, prepared: str) -> None:
        self.discarded.append(prepared)

    async def retire(self, previous: str) -> None:
        if self.retire_failures:
            self.retire_failures -= 1
            raise RuntimeError("still in use")
        self.retired.append(previous)

    def mirror(self, desired: str) -> dict[str, str]:
        return {"A": desired, "B": ""}

    def describe(self, view: str) -> dict[str, Any]:
        return {"view": view}

    def paused(self, view: str) -> bool:
        return view == "view:off"


@pytest.fixture
def component() -> Component:
    return Component()


@pytest.fixture
def group(component: Component) -> live.LiveGroup[str, str]:
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        prepare_timeout=0.5,
        retire_timeout=0.2,
        discard_timeout=0.2,
        backoff_initial=0.05,
        backoff_max=0.2,
    )
    group.install_baseline()
    return group


async def apply(group: live.LiveGroup[str, str], snap: revisions.SourceSnapshot) -> None:
    group.submit(snap)
    await group.run_once()


@pytest.mark.asyncio
async def test_a_group_without_an_active_contract_stays_on_its_baseline(group, component):
    await apply(group, snapshot(1, a="2", contract=False))
    assert group.status.state == "dormant"
    assert group.acquire().view == "view:boot"
    assert component.prepared == []
    # It reports which keys would change on activation: names only.
    assert group.status.pending_keys == ("A",)
    await apply(group, snapshot(2, a="2", active=False))
    assert group.status.state == "dormant"
    assert group.acquire().generation == 0


@pytest.mark.asyncio
async def test_activation_applies_the_saved_target_and_reports_it(group, component):
    published: list[live.Published[str, str]] = []
    group._on_publish = lambda _group, item: published.append(item)
    await apply(group, snapshot(4, a="2"))
    current = group.acquire()
    assert current.view == "view:2"
    assert current.generation == 1
    assert current.managed
    assert published == [current]
    status = group.status
    assert status.state == "applied"
    assert (status.epoch, status.checked_revision, status.reconciled_revision) == (
        "epoch-one",
        4,
        4,
    )
    # The baseline it replaced is retired.
    assert component.retired == ["view:boot"]


@pytest.mark.asyncio
async def test_an_unchanged_target_is_adopted_without_a_rebuild(group, component):
    await apply(group, snapshot(1, a=None))  # projects "boot", the baseline's own target
    assert component.prepared == []
    assert group.managed
    assert group.acquire().generation == 0
    assert group.status.state == "applied"


@pytest.mark.asyncio
async def test_an_unrelated_change_rebuilds_nothing(group, component):
    await apply(group, snapshot(1, a="2"))
    await apply(group, snapshot(2, a="2", z="unrelated"))
    await apply(group, snapshot(3, a="2", z="again"))
    assert component.prepared == ["2"]
    assert group.acquire().generation == 1
    assert group.status.reconciled_revision == 3


@pytest.mark.asyncio
async def test_an_invalid_target_keeps_the_state_in_use(group, component):
    await apply(group, snapshot(1, a="2"))
    await apply(group, snapshot(2, a="bad"))
    assert group.acquire().view == "view:2"
    assert group.status.state == "invalid"
    assert group.status.error_code == "value_invalid"
    assert group.status.error_keys == ("A",)
    # Reconciled stays where it was; checked moves on.
    assert group.status.reconciled_revision == 1
    assert group.status.checked_revision == 2


@pytest.mark.asyncio
async def test_a_failed_prepare_keeps_the_state_in_use_and_backs_off(group, component):
    await apply(group, snapshot(1, a="2"))
    await apply(group, snapshot(2, a="boom"))
    assert group.acquire().view == "view:2"
    assert group.status.state == "failed"
    assert group.status.error_code == "prepare_failed"
    report = group.status.as_report()
    # The exception text (which here quotes a URL) is never reported.
    assert "hooks.example" not in repr(report)
    # The same target is not retried before its backoff ends...
    await apply(group, snapshot(3, a="boom"))
    assert component.prepared == ["2", "boom"]
    # ...but a new relevant target is tried at once.
    await apply(group, snapshot(4, a="3"))
    assert group.acquire().view == "view:3"
    assert group.status.state == "applied"


@pytest.mark.asyncio
async def test_a_failed_target_is_retried_after_its_backoff(group, component):
    await apply(group, snapshot(1, a="boom"))
    assert component.prepared == ["boom"]
    await asyncio.sleep(0.12)
    await apply(group, snapshot(2, a="boom"))
    assert component.prepared == ["boom", "boom"]


@pytest.mark.asyncio
async def test_a_superseded_candidate_is_never_published(group, component):
    """However often the target moves during prepare, only the newest is published."""
    component.gate = asyncio.Event()
    published: list[str] = []
    group._on_publish = lambda _group, item: published.append(item.view)
    group.submit(snapshot(1, a="2"))
    task = asyncio.create_task(group.run_once())
    for revision, value in ((2, "3"), (3, "4"), (4, "5")):
        await component.started.wait()
        component.started.clear()
        group.submit(snapshot(revision, a=value))
        # Let the in-flight prepare finish; it is now stale.
        component.gate.set()
        await asyncio.sleep(0)
        component.gate.clear()
    component.gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert published == ["view:5"]
    assert group.acquire().view == "view:5"
    assert set(component.discarded) >= {"view:2", "view:3", "view:4"}
    assert group.status.reconciled_revision == 4


@pytest.mark.asyncio
async def test_a_candidate_whose_target_turned_invalid_is_discarded(group, component):
    await apply(group, snapshot(1, a="2"))
    component.started.clear()
    component.gate = asyncio.Event()
    group.submit(snapshot(2, a="3"))
    task = asyncio.create_task(group.run_once())
    await component.started.wait()
    group.submit(snapshot(3, a="bad"))
    component.gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert group.acquire().view == "view:2"
    assert "view:3" in component.discarded
    assert group.status.state == "invalid"


@pytest.mark.asyncio
async def test_a_new_epoch_invalidates_a_candidate(group, component):
    component.gate = asyncio.Event()
    group.submit(snapshot(5, a="2"))
    task = asyncio.create_task(group.run_once())
    await component.started.wait()
    # A restored database: a new epoch, even with the same target.
    group.submit(snapshot(1, a="2", epoch="epoch-two"))
    component.gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert "view:2" in component.discarded
    current = group.acquire()
    assert current.view == "view:2"
    assert current.version == revisions.SourceVersion("epoch-two", 1)
    assert group.status.epoch == "epoch-two"
    assert group.status.reconciled_revision == 1
    assert not group.status.regressed


@pytest.mark.asyncio
async def test_an_older_revision_in_the_same_epoch_is_flagged_and_applied(group, component):
    await apply(group, snapshot(9, a="2"))
    await apply(group, snapshot(3, a="1"))
    assert group.status.regressed
    assert group.acquire().view == "view:1"


@pytest.mark.asyncio
async def test_cancellation_during_prepare_discards_the_candidate(group, component):
    component.gate = asyncio.Event()
    group.submit(snapshot(1, a="2"))
    task = asyncio.create_task(group.run_once())
    await component.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert group.acquire().view == "view:boot"
    assert group.acquire().generation == 0


@pytest.mark.asyncio
async def test_a_prepare_that_hangs_times_out(group, component):
    component.prepare_hang = True
    await apply(group, snapshot(1, a="2"))
    assert group.status.state == "failed"
    assert group.status.error_code == "prepare_timeout"
    assert group.acquire().view == "view:boot"


@pytest.mark.asyncio
async def test_a_failed_retire_keeps_the_publication(group, component):
    component.retire_failures = 1
    await apply(group, snapshot(1, a="2"))
    assert group.acquire().view == "view:2"
    assert group.status.state == "applied"
    assert group.status.cleanup_degraded
    assert group.retained == 1
    await asyncio.sleep(0.06)
    await group.run_once()
    assert not group.status.cleanup_degraded
    assert group.retained == 0
    assert component.retired == ["view:boot"]


@pytest.mark.asyncio
async def test_too_many_unretired_states_block_new_applications(component):
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        max_retained=1,
        retire_timeout=0.1,
        backoff_initial=10,
        backoff_max=10,
    )
    group.install_baseline()
    component.retire_failures = 100
    await apply(group, snapshot(1, a="2"))
    assert group.retained == 1
    await apply(group, snapshot(2, a="3"))
    assert group.status.state == "cleanup_blocked"
    assert group.acquire().view == "view:2"
    assert component.prepared == ["2"]


@pytest.mark.asyncio
async def test_an_unsupported_contract_keeps_the_state_in_use(group, component):
    await apply(group, snapshot(1, a="2"))
    await apply(group, snapshot(2, a="3", schema_version=2))
    assert group.status.state == "unsupported"
    assert group.status.error_code == "schema_version_unsupported"
    assert group.acquire().view == "view:2"
    await apply(group, snapshot(3, a="3", keys=("A", "B", "C")))
    assert group.status.error_code == "contract_keys_mismatch"
    assert component.prepared == ["2"]


@pytest.mark.asyncio
async def test_deactivation_freezes_the_last_published_state(group, component):
    await apply(group, snapshot(1, a="2"))
    await apply(group, snapshot(2, a="3", active=False))
    assert group.status.state == "dormant-frozen"
    # Not the boot baseline: the state last published stays until a restart.
    assert group.acquire().view == "view:2"
    assert group.status.pending_keys == ("A",)
    # Reactivation applies the saved target again.
    await apply(group, snapshot(3, a="3"))
    assert group.acquire().view == "view:3"


@pytest.mark.asyncio
async def test_a_failed_read_marks_the_state_stale(group, component):
    await apply(group, snapshot(1, a="2"))
    group.read_failed()
    assert group.status.read_stale
    assert group.acquire().view == "view:2"
    await apply(group, snapshot(2, a="2"))
    assert not group.status.read_stale


@pytest.mark.asyncio
async def test_a_reader_fence_fails_once_a_newer_state_is_published(group, component):
    first = group.acquire()
    assert group.is_current(first)
    await apply(group, snapshot(1, a="2"))
    assert not group.is_current(first)
    assert group.is_current(group.acquire())


@pytest.mark.asyncio
async def test_paused_is_reported_for_a_state_idle_by_configuration(group, component):
    await apply(group, snapshot(1, a="off"))
    assert group.status.state == "paused"
    assert group.status.detail == {"view": "view:off"}


@pytest.mark.asyncio
async def test_the_applier_task_follows_submissions_and_stops_cleanly(group, component):
    group.start()
    group.submit(snapshot(1, a="2"))
    for _ in range(100):
        if group.acquire().view == "view:2":
            break
        await asyncio.sleep(0.01)
    assert group.acquire().view == "view:2"
    component.gate = asyncio.Event()
    group.submit(snapshot(2, a="3"))
    await asyncio.wait_for(component.started.wait(), timeout=1)
    import time

    await group.stop(time.monotonic() + 1)
    # The prepare in flight was abandoned: nothing new was published.
    assert group.acquire().view == "view:2"


class _Reporter:
    def __init__(self) -> None:
        self.reports: dict[str, dict[str, Any]] = {}

    def set_group(self, name: str, status: dict[str, Any]) -> None:
        self.reports[name] = status

    def remove_group(self, name: str) -> None:
        self.reports.pop(name, None)


@pytest.mark.asyncio
async def test_the_coordinator_reports_every_group_and_unknown_ones(component):
    coordinator = live.Coordinator()
    group = coordinator.register(
        live.LiveGroup("test.group", schema_versions=(1,), keys=KEYS, component=component)
    )
    group.install_baseline()
    reporter = _Reporter()
    coordinator.reporter = reporter  # type: ignore[assignment]
    snap = snapshot(1, a="2")
    groups = dict(snap.contracts.groups)
    groups["future.group"] = revisions.GroupContract("future.group", 3, ("X",), active=True)
    snap = revisions.SourceSnapshot(
        source=snap.source,
        version=snap.version,
        contracts=revisions.Contracts(groups),
        rows=snap.rows,
    )
    coordinator.submit(snap)
    await group.run_once()
    assert reporter.reports["test.group"]["state"] == "applied"
    assert reporter.reports["future.group"]["state"] == "unsupported"
    assert reporter.reports["future.group"]["error_code"] == "unknown_group"
    assert coordinator.capabilities()["groups"] == {"test.group": [1]}
    # The unknown group leaves the contract: its stale report goes too.
    coordinator.submit(snapshot(2, a="2"))
    assert "future.group" not in reporter.reports
    assert "test.group" in reporter.reports


@pytest.mark.asyncio
async def test_an_unknown_group_read_before_the_reporter_starts_is_reported(component):
    coordinator = live.Coordinator()
    coordinator.register(
        live.LiveGroup("test.group", schema_versions=(1,), keys=KEYS, component=component)
    )
    snap = snapshot(1)
    groups = dict(snap.contracts.groups)
    groups["future.group"] = revisions.GroupContract("future.group", 3, ("X",), active=True)
    # The boot read, before there is a reporter.
    coordinator.submit(
        revisions.SourceSnapshot(
            source=snap.source,
            version=snap.version,
            contracts=revisions.Contracts(groups),
            rows=snap.rows,
        )
    )

    class Started(_Reporter):
        on_report = None

        def start(self) -> None:
            pass

    reporter = Started()
    coordinator.start(reporter)  # type: ignore[arg-type]
    try:
        assert reporter.reports["future.group"]["error_code"] == "unknown_group"
        assert reporter.reports["future.group"]["epoch"] == "epoch-one"
    finally:
        coordinator.reporter = None
        await coordinator.stop(timeout=0.5)


@pytest.mark.asyncio
async def test_a_failed_discard_is_retried_and_counts_against_the_bound(component):
    failures = {"left": 1}
    discarded: list[str] = []

    async def discard(prepared: str) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("busy")
        discarded.append(prepared)

    component.discard = discard  # type: ignore[method-assign]
    group = live.LiveGroup(
        "test.group",
        schema_versions=(1,),
        keys=KEYS,
        component=component,
        backoff_initial=0.05,
        backoff_max=0.05,
        max_retained=1,
    )
    group.install_baseline()
    component.gate = asyncio.Event()
    group.submit(snapshot(1, a="2"))
    task = asyncio.create_task(group.run_once())
    await component.started.wait()
    component.started.clear()
    group.submit(snapshot(2, a="3"))
    component.gate.set()
    await asyncio.wait_for(task, timeout=2)
    # "view:2" could not be discarded at first: it is held, not lost...
    assert discarded == []
    assert group.status.cleanup_degraded
    # ...the bound counted it, so nothing new was prepared meanwhile.
    assert group.retained >= 1
    await asyncio.sleep(0.06)
    await group.run_once()
    assert "view:2" in discarded


@pytest.mark.asyncio
async def test_an_observer_failure_never_logs_its_text(group, component, caplog):
    secret = "SYNTHETIC-SECRET-IN-OBSERVER"

    def observer(_group, _published):
        raise RuntimeError(secret)

    group._on_publish = observer
    await apply(group, snapshot(1, a="2"))
    assert group.acquire().view == "view:2"
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
