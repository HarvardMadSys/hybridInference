"""PostgreSQL tests for configuration source versions, contracts and instance reports.

Every write path to ``app_config`` must advance the source revision in its own
transaction (a statement trigger), a rollback must take the advance back, and a
snapshot must pair the rows with exactly their version.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
import pytest_asyncio

from serving.config import instances, live_control, revisions
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP, MANAGED_GROUPS
from serving.config.app_config_store import AppConfigStore, ConfigRow
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_TABLES = ("app_config", "config_source_revisions", "config_instances")


async def _drop(connection) -> None:
    for table in _TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await connection.execute("DROP FUNCTION IF EXISTS config_source_bump_app_config() CASCADE")


@pytest.fixture
def pg_dsn() -> str:
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def pool(pg_dsn: str):
    pool = await postgres.create_pool(min_size=1, max_size=4)
    async with pool.acquire() as connection:
        await _drop(connection)
    try:
        yield pool
    finally:
        async with pool.acquire() as connection:
            await _drop(connection)
        await pool.close()


@pytest_asyncio.fixture
async def store(pool) -> AppConfigStore:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    return store


async def _version(pool) -> revisions.SourceVersion:
    async with pool.acquire() as connection:
        version, _ = await revisions.read_version(connection)
    assert version is not None
    return version


@pytest.mark.asyncio
async def test_schema_starts_at_revision_zero_with_nothing_active(pool, store) -> None:
    version = await _version(pool)
    assert version.revision == 0
    assert len(version.epoch) == 16
    snapshot = await store.fetch_snapshot()
    # An empty table still has a version, read with it.
    assert snapshot.rows == {}
    assert snapshot.version == version
    assert snapshot.contracts.groups == {}
    # Idempotent: a second boot neither resets the history nor activates anything.
    await store.write([ConfigRow("SITE_NAME", "x")])
    await store.ensure_schema()
    again = await _version(pool)
    assert again.epoch == version.epoch
    # An upsert fires both the INSERT and the UPDATE statement triggers: the
    # revision may skip, never stand still.
    assert again.revision >= 1
    assert (await store.fetch_snapshot()).contracts.groups == {}


@pytest.mark.asyncio
async def test_every_write_path_advances_the_revision(pool, store) -> None:
    seen = [await _version(pool)]

    async def advanced() -> None:
        current = await _version(pool)
        assert current.epoch == seen[-1].epoch
        assert current.revision > seen[-1].revision, (seen[-1], current)
        seen.append(current)

    await store.write([ConfigRow("SITE_NAME", "a")])  # admin upsert
    await advanced()
    await store.insert_missing([ConfigRow("SITE_DOCS_URL", "https://d", source="env_import")])
    await advanced()  # boot import
    await store.insert_missing(
        [ConfigRow("JWT_SECRET_KEY", "g", secret=True, source="generated")], replace_reset=True
    )
    await advanced()  # generated secret
    await store.mark_secret(["SITE_DOCS_URL"])
    await advanced()
    await store.reset("SITE_NAME", secret=False, updated_by="admin")
    await advanced()  # reset
    await store.delete("SITE_DOCS_URL")
    await advanced()  # delete
    async with pool.acquire() as connection:
        await connection.execute("UPDATE app_config SET value = 'raw' WHERE key = 'JWT_SECRET_KEY'")
        await advanced()  # plain SQL
        await connection.execute("UPDATE app_config SET value = 'raw' WHERE key = 'NO_SUCH_KEY'")
        await advanced()  # a no-op statement still advances, conservatively
        await connection.execute("TRUNCATE app_config")
        await advanced()  # TRUNCATE


@pytest.mark.asyncio
async def test_a_rollback_takes_its_advance_back(pool, store) -> None:
    before = await _version(pool)
    with pytest.raises(RuntimeError):
        async with store.transaction() as transaction:
            await transaction.write([ConfigRow("SITE_NAME", "rolled back")])
            await transaction.write([ConfigRow("SITE_NAME", "twice")])
            inside = await transaction.current_version()
            # Every statement advances, visible inside the transaction.
            assert inside.revision >= before.revision + 2
            raise RuntimeError("abort")
    assert await _version(pool) == before
    assert await store.fetch_all() == {}


@pytest.mark.asyncio
async def test_a_missing_counter_row_is_recreated_with_a_new_epoch(pool, store) -> None:
    before = await _version(pool)
    async with pool.acquire() as connection:
        await connection.execute("DELETE FROM config_source_revisions")
    await store.write([ConfigRow("SITE_NAME", "after maintenance")])
    after = await _version(pool)
    assert after.revision >= 1
    assert after.epoch != before.epoch


@pytest.mark.asyncio
async def test_a_stale_expected_version_stores_nothing(pool, store) -> None:
    first = await _version(pool)
    async with store.transaction(expected=first) as transaction:
        await transaction.write([ConfigRow("SITE_NAME", "one")])
    with pytest.raises(revisions.VersionConflict) as caught:
        async with store.transaction(expected=first) as transaction:
            await transaction.write([ConfigRow("SITE_NAME", "two")])
    assert caught.value.current == await _version(pool)
    rows = await store.fetch_all()
    assert rows["SITE_NAME"].value == "one"


@pytest.mark.asyncio
async def test_the_version_lock_holds_off_a_plain_sql_writer(pool, store) -> None:
    """A writer that skips the advisory lock still waits: its trigger needs the version row."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with store.transaction() as transaction:
            await transaction.write([ConfigRow("SITE_NAME", "held")])
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    async with pool.acquire() as connection:
        raw = asyncio.create_task(
            connection.execute("INSERT INTO app_config (key, value) VALUES ('SITE_DOCS_URL', 'x')")
        )
        await asyncio.sleep(0.3)
        assert not raw.done()
        # A snapshot taken meanwhile sees neither uncommitted write.
        snapshot = await store.fetch_snapshot()
        assert snapshot.rows == {}
        release.set()
        await task
        await raw
    snapshot = await store.fetch_snapshot()
    assert set(snapshot.rows) == {"SITE_NAME", "SITE_DOCS_URL"}
    assert snapshot.version.revision >= 2


@pytest.mark.asyncio
async def test_a_snapshot_pairs_rows_with_their_version_under_concurrent_writes(
    pool, store
) -> None:
    stop = asyncio.Event()
    #: The revision each committed write left, by the value it wrote.
    committed: dict[int, str] = {}

    async def writer() -> None:
        index = 0
        while not stop.is_set():
            index += 1
            async with store.transaction() as transaction:
                await transaction.write([ConfigRow("SITE_NAME", str(index))])
                version = await transaction.current_version()
            committed[version.revision] = str(index)

    task = asyncio.create_task(writer())
    checked = 0
    try:
        for _ in range(60):
            snapshot = await store.fetch_snapshot()
            revision = snapshot.version.revision
            if "SITE_NAME" in snapshot.rows and revision in committed:
                # The rows read are exactly the ones the version names.
                assert snapshot.rows["SITE_NAME"].value == committed[revision]
                checked += 1
            await asyncio.sleep(0.005)
    finally:
        stop.set()
        await task
    assert checked > 0


@pytest.mark.asyncio
async def test_activation_is_explicit_conditional_and_gated_on_capability(pool, store) -> None:
    manifest = MANAGED_GROUPS[ALERTS_RUNTIME_GROUP]
    version = await _version(pool)
    # Nothing registered is never taken as coverage, and a count is required.
    with pytest.raises(live_control.ControlError, match="inventory counts 1"):
        await live_control.activate(
            store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=1
        )
    with pytest.raises(live_control.ControlError, match="at least 1"):
        await live_control.activate(
            store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=0
        )
    old = instances.InstanceReporter(
        pool=pool,
        instance_id="old-build",
        started_at=dt.datetime.now(dt.UTC),
        capabilities={"protocol": 1, "groups": {}},
    )
    assert await old.report_once()
    capable = instances.InstanceReporter(
        pool=pool,
        instance_id="new-build",
        started_at=dt.datetime.now(dt.UTC),
        capabilities={"protocol": 1, "groups": {ALERTS_RUNTIME_GROUP: [1]}},
    )
    assert await capable.report_once()
    # A live process without the group refuses activation; there is no override.
    with pytest.raises(live_control.ControlError, match="do not support"):
        await live_control.activate(
            store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=2
        )
    async with pool.acquire() as connection:
        await connection.execute("DELETE FROM config_instances WHERE instance_id = 'old-build'")
    with pytest.raises(live_control.ControlError, match="inventory counts 2"):
        await live_control.activate(
            store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=2
        )
    # A write between the preview and the activation refuses it.
    await store.write([ConfigRow("SITE_NAME", "moved on")])
    with pytest.raises(revisions.VersionConflict):
        await live_control.activate(
            store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=1
        )
    version = await _version(pool)
    activated = await live_control.activate(
        store, ALERTS_RUNTIME_GROUP, expected=version, changed_by="test", expect_instances=1
    )
    assert activated.revision == version.revision + 1
    snapshot = await store.fetch_snapshot()
    contract = snapshot.contracts.groups[ALERTS_RUNTIME_GROUP]
    assert contract.active
    assert contract.schema_version == manifest.schema_version
    assert contract.keys == manifest.keys
    deactivated = await live_control.deactivate(
        store, ALERTS_RUNTIME_GROUP, expected=activated, changed_by="test"
    )
    assert deactivated.revision == activated.revision + 1
    assert not (await store.fetch_snapshot()).contracts.groups[ALERTS_RUNTIME_GROUP].active


@pytest.mark.asyncio
async def test_status_counts_only_live_reconciled_instances(pool, store) -> None:
    version = await _version(pool)

    async def report(instance_id: str, groups: dict, *, capable: bool = True) -> None:
        reporter = instances.InstanceReporter(
            pool=pool,
            instance_id=instance_id,
            started_at=dt.datetime.now(dt.UTC),
            capabilities={
                "protocol": 1,
                "groups": {ALERTS_RUNTIME_GROUP: [1]} if capable else {},
            },
        )
        for name, body in groups.items():
            reporter.set_group(name, body)
        assert await reporter.report_once()

    applied = {
        ALERTS_RUNTIME_GROUP: {
            "state": "applied",
            "epoch": version.epoch,
            "reconciled_revision": version.revision,
        }
    }
    await report("a", applied)
    await report("b", applied)
    await report("old", {}, capable=False)
    await report(
        "behind",
        {
            ALERTS_RUNTIME_GROUP: {
                "state": "applied",
                "epoch": version.epoch,
                "reconciled_revision": version.revision - 1,
            }
        },
    )
    await report("stale", applied)
    async with pool.acquire() as connection:
        # Heartbeats are stamped by the database clock; age one past the bound.
        await connection.execute(
            "UPDATE config_instances SET heartbeat_at = NOW() - interval '120 seconds' "
            "WHERE instance_id = 'stale'"
        )
    status = await live_control.fetch_status(store, this_instance="a")
    group = status["groups"][0]
    coverage = group["coverage"]
    assert coverage["registered"] == 5
    assert coverage["converged"] == 2
    assert coverage["unsupported"] == 1
    assert coverage["applying"] == 1
    # A lost heartbeat is unknown, never success.
    assert coverage["unknown"] == 1
    assert coverage["verified"] is False
    assert [row["this_process"] for row in group["instances"] if row["instance_id"] == "a"] == [
        True
    ]


@pytest.mark.asyncio
async def test_an_older_report_never_overwrites_a_newer_one(pool, store) -> None:
    reporter = instances.InstanceReporter(
        pool=pool,
        instance_id="one",
        started_at=dt.datetime.now(dt.UTC),
        capabilities={"protocol": 1, "groups": {}},
    )
    reporter.set_group("g", {"state": "applied", "generation": 2})
    assert await reporter.report_once()
    async with pool.acquire() as connection:
        # A delayed write carrying an older sequence lands after the newer one.
        await connection.fetchval(
            instances.UPSERT_REPORT_SQL,
            "one",
            "unknown",
            "{}",
            dt.datetime.now(dt.UTC),
            "running",
            0,
            json.dumps({"g": {"state": "applying", "generation": 1}}),
        )
        stored = await connection.fetchrow(
            "SELECT report_seq, groups FROM config_instances WHERE instance_id = 'one'"
        )
    assert stored["report_seq"] == 1
    assert json.loads(stored["groups"])["g"]["generation"] == 2


@pytest.mark.asyncio
async def test_a_new_epoch_removes_every_registration(pool, store) -> None:
    reporter = instances.InstanceReporter(
        pool=pool,
        instance_id="before-restore",
        started_at=dt.datetime.now(dt.UTC),
        capabilities={"protocol": 1, "groups": {}},
    )
    assert await reporter.report_once()
    version = await _version(pool)
    fresh = await live_control.reset_epoch(store, expected=version)
    assert fresh.epoch != version.epoch
    assert fresh.revision == version.revision + 1
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT COUNT(*) FROM config_instances") == 0
    # The process registers again on its next report.
    assert await reporter.report_once()
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT COUNT(*) FROM config_instances") == 1


@pytest.mark.asyncio
async def test_schema_creation_races_safely(pg_dsn, pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "secret BOOLEAN NOT NULL DEFAULT FALSE, source TEXT NOT NULL DEFAULT 'admin', "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_by TEXT)"
        )
    stores = [AppConfigStore(pool) for _ in range(4)]
    await asyncio.gather(*(each.ensure_schema() for each in stores))
    async with pool.acquire() as connection:
        triggers = await connection.fetchval(
            "SELECT COUNT(*) FROM pg_trigger WHERE tgname = $1", revisions.TRIGGER_NAME
        )
    assert triggers == 1
    assert (await _version(pool)).revision == 0
