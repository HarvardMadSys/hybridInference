"""Weekly archives of OpenInfra's tables, and the retention, against PostgreSQL."""

from __future__ import annotations

import csv
import gzip
import json

import pytest
import pytest_asyncio

from serving.openinfra import archive, config, maintenance, store
from serving.openinfra.wire import parse_snapshot
from tests.fixtures import postgres
from tests.integration.test_openinfra_store_postgres import TABLES, body, engine

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

HOUR = 3_600_000
DAY = 24 * HOUR
#: A Monday, 00:00 UTC: 2027-01-11.
WEEK = archive.week_start(1_800_000_000_000)
NEXT = WEEK + archive.WEEK_MS
#: When the week can first be archived.
READY = NEXT + archive.ARCHIVE_GRACE_MS


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(TABLES)}")
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"TRUNCATE {', '.join(TABLES)}")
        await pool.close()


async def ingest(pool, at: int, *, util: float = 50, requests: list[dict] | None = None) -> None:
    payload = body(
        gpus=[{"index": 0, "utilization_pct": util}], engines=[engine(requests=requests)]
    )
    async with pool.acquire() as conn:
        await store.SnapshotWrite(parse_snapshot(payload), at).apply(conn)


async def count(pool, table: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(f"SELECT COUNT(*) FROM {table}")


def read_csv(path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", newline="") as handle:
        return list(csv.DictReader(handle))


async def test_an_ended_week_is_written_whole_and_recorded(pool, tmp_path):
    await ingest(pool, WEEK + HOUR, util=10, requests=[{"ts": WEEK + HOUR - 1_000, "input_len": 9}])
    await ingest(pool, WEEK + 3 * DAY, util=20)
    await ingest(pool, NEXT + HOUR, util=30)  # the week after: not archived
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, NEXT)
        written = await archive.archive_ended_weeks(conn, tmp_path, READY + 60_000)

    assert written == [WEEK]
    week_dir = tmp_path / "2027-01-11"
    manifest = json.loads((week_dir / "manifest.json").read_text())
    assert manifest["format"] == archive.FORMAT
    assert manifest["week_start"] == "2027-01-11T00:00:00Z"
    assert manifest["week_end"] == "2027-01-18T00:00:00Z"
    assert manifest["tables"]["openinfra_gpu_samples"]["rows"] == 2
    assert manifest["tables"]["openinfra_request_events"]["rows"] == 1
    assert set(manifest["tables"]) == {table.name for table in archive.TABLES}

    samples = read_csv(week_dir / "openinfra_gpu_samples.csv.gz")
    assert [(row["gpu_id"], int(row["ts"]), float(row["utilization_pct"])) for row in samples] == [
        ("node-a:0", WEEK + HOUR, 10.0),
        ("node-a:0", WEEK + 3 * DAY, 20.0),
    ]
    events = read_csv(week_dir / "openinfra_request_events.csv.gz")
    assert [(row["engine_id"], row["input_len"]) for row in events] == [("chat-a", "9")]
    rollups = read_csv(week_dir / "openinfra_gpu_rollups.csv.gz")
    assert rollups
    assert all(WEEK <= int(row["bucket_ts"]) < NEXT for row in rollups)
    # The live rows as they were, to read the ids by.
    assert [row["id"] for row in read_csv(week_dir / "openinfra_gpus.csv.gz")] == ["node-a:0"]
    # An empty table still says what its columns are.
    with gzip.open(week_dir / "openinfra_cpu_rollups.csv.gz", "rt") as handle:
        assert handle.readline().startswith("grain,bucket_ts,node_id,samples")
    assert not list(tmp_path.glob(".*"))  # nothing left half-written

    async with pool.acquire() as conn:
        weeks = await archive.archived_weeks(conn)
        again = await archive.archive_ended_weeks(conn, tmp_path, READY + 60_000)
    assert list(weeks) == [WEEK]
    assert weeks[WEEK]["location"] == str(week_dir)
    assert weeks[WEEK]["tables"]["openinfra_gpu_samples"] == 2
    assert again == []
    # Archiving deletes nothing.
    assert await count(pool, "openinfra_gpu_samples") == 3


async def test_a_week_waits_for_its_grace_and_an_empty_one_is_skipped(pool, tmp_path):
    await ingest(pool, WEEK - archive.WEEK_MS + HOUR)  # a week, then one with nothing in it
    await ingest(pool, NEXT + HOUR)
    async with pool.acquire() as conn:
        early = await archive.archive_ended_weeks(conn, tmp_path, READY - 60_000)
        later = await archive.archive_ended_weeks(conn, tmp_path, READY + archive.WEEK_MS)
    assert early == [WEEK - archive.WEEK_MS]
    assert later == [NEXT]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "2027-01-04",
        "2027-01-18",
    ]


async def test_a_long_gap_between_weeks_is_crossed_in_one_run(pool, tmp_path):
    long_ago = WEEK - 100 * archive.WEEK_MS
    await ingest(pool, long_ago + HOUR)
    await ingest(pool, WEEK + HOUR)
    async with pool.acquire() as conn:
        assert await archive.next_time(conn, long_ago + archive.WEEK_MS) == WEEK + HOUR
        assert await archive.archive_ended_weeks(conn, tmp_path, READY) == [long_ago, WEEK]


async def test_a_run_archives_at_most_its_limit_oldest_first(pool, tmp_path):
    for weeks_back in range(3):
        await ingest(pool, WEEK - weeks_back * archive.WEEK_MS + HOUR)
    async with pool.acquire() as conn:
        first = await archive.archive_ended_weeks(conn, tmp_path, READY, limit=2)
        second = await archive.archive_ended_weeks(conn, tmp_path, READY, limit=2)
    assert first == [WEEK - 2 * archive.WEEK_MS, WEEK - archive.WEEK_MS]
    assert second == [WEEK]


async def test_a_directory_written_but_not_recorded_is_recorded_as_it_is(pool, tmp_path):
    await ingest(pool, WEEK + HOUR)
    async with pool.acquire() as conn:
        await archive.archive_ended_weeks(conn, tmp_path, READY)
        await conn.execute("DELETE FROM openinfra_archives")
        manifest = tmp_path / "2027-01-11" / "manifest.json"
        before = manifest.stat().st_mtime_ns
        assert await archive.archive_ended_weeks(conn, tmp_path, READY) == [WEEK]
        weeks = await archive.archived_weeks(conn)
    assert manifest.stat().st_mtime_ns == before
    assert weeks[WEEK]["tables"]["openinfra_gpu_samples"] == 1


async def test_what_an_interrupted_run_left_is_replaced(pool, tmp_path):
    await ingest(pool, WEEK + HOUR)
    partial = tmp_path / ".2027-01-11.partial"
    partial.mkdir()
    (partial / "openinfra_gpu_samples.csv.gz").write_bytes(b"truncated")
    async with pool.acquire() as conn:
        assert await archive.archive_ended_weeks(conn, tmp_path, READY) == [WEEK]
    assert not partial.exists()
    assert len(read_csv(tmp_path / "2027-01-11" / "openinfra_gpu_samples.csv.gz")) == 1


async def test_a_directory_that_is_not_the_weeks_archive_is_left_alone(pool, tmp_path):
    await ingest(pool, WEEK + HOUR)
    foreign = tmp_path / "2027-01-11"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not an archive")
    async with pool.acquire() as conn:
        with pytest.raises(archive.ArchiveError, match="is not the archive"):
            await archive.archive_ended_weeks(conn, tmp_path, READY)
        assert await archive.archived_weeks(conn) == {}
    assert sorted(path.name for path in foreign.iterdir()) == ["notes.txt"]


async def test_retention_deletes_only_archived_weeks(pool, tmp_path):
    before = WEEK - archive.WEEK_MS
    await ingest(pool, before + HOUR, requests=[{"ts": before + HOUR, "input_len": 1}])
    await ingest(pool, WEEK + HOUR, requests=[{"ts": WEEK + HOUR, "input_len": 2}])
    await ingest(pool, WEEK + 6 * DAY)
    now = NEXT + 7 * DAY + HOUR  # both weeks are past the retention
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, WEEK + 7 * DAY)
        await archive.archive_week(conn, tmp_path, before, now)
        deleted = await archive.prune(conn, now, 7, archived_only=True)
    assert deleted == {
        "openinfra_cpu_samples": 1,
        "openinfra_gpu_samples": 1,
        "openinfra_engine_samples": 1,
        "openinfra_request_events": 1,
    }
    async with pool.acquire() as conn:
        left = await conn.fetch("SELECT ts FROM openinfra_gpu_samples ORDER BY ts")
        events = await conn.fetch("SELECT input_len FROM openinfra_request_events")
    assert [row["ts"] for row in left] == [WEEK + HOUR, WEEK + 6 * DAY]
    assert [row["input_len"] for row in events] == [2]
    # Rollups and live rows stay.
    assert await count(pool, "openinfra_gpu_rollups") > 0
    assert await count(pool, "openinfra_gpus") == 1


async def test_an_archived_week_goes_as_it_passes_the_retention(pool, tmp_path):
    await ingest(pool, WEEK + HOUR)
    await ingest(pool, WEEK + 3 * DAY)
    async with pool.acquire() as conn:
        await archive.archive_ended_weeks(conn, tmp_path, READY)
        # Seven days after the first sample, not after the week.
        deleted = await archive.prune(conn, WEEK + 7 * DAY + 2 * HOUR, 7, archived_only=True)
    assert deleted["openinfra_gpu_samples"] == 1
    assert await count(pool, "openinfra_gpu_samples") == 1


async def test_without_an_archive_everything_past_the_retention_goes(pool, monkeypatch):
    monkeypatch.setattr(archive, "DELETE_BATCH", 2)  # several batches
    for minute in range(5):
        await ingest(pool, WEEK + minute * 60_000)
    await ingest(pool, WEEK + 8 * DAY)
    async with pool.acquire() as conn:
        deleted = await archive.prune(conn, WEEK + 8 * DAY + HOUR, 7, archived_only=False)
    assert deleted["openinfra_gpu_samples"] == 5
    assert await count(pool, "openinfra_gpu_samples") == 1


async def test_zero_days_keeps_everything(pool):
    await ingest(pool, WEEK)
    async with pool.acquire() as conn:
        deleted = await archive.prune(conn, WEEK + 400 * DAY, 0, archived_only=False)
    assert set(deleted.values()) == {0}
    assert await count(pool, "openinfra_gpu_samples") == 1


async def test_the_scheduled_run_archives_then_prunes(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "archive_dir", lambda: tmp_path / "archive")
    monkeypatch.setattr(config, "retention_days", lambda: 7)
    await ingest(pool, WEEK + HOUR)
    await ingest(pool, NEXT + 7 * DAY)

    assert await maintenance.run_maintenance(pool, NEXT + 7 * DAY + archive.ARCHIVE_GRACE_MS)

    assert (tmp_path / "archive" / "2027-01-11" / "manifest.json").exists()
    async with pool.acquire() as conn:
        left = await conn.fetch("SELECT ts FROM openinfra_gpu_samples")
        rollups = await conn.fetchval(
            "SELECT COUNT(*) FROM openinfra_gpu_rollups WHERE bucket_ts < $1", NEXT
        )
    assert [row["ts"] for row in left] == [NEXT + 7 * DAY]
    # The deleted week's rollups were computed before its samples went.
    assert rollups > 0


async def test_the_oldest_row_is_found_across_tables(pool):
    await ingest(pool, WEEK + HOUR, requests=[{"ts": WEEK + HOUR - 600_000, "input_len": 1}])
    async with pool.acquire() as conn:
        assert await archive.oldest_time(conn) == WEEK + HOUR - 600_000
        await conn.execute(f"TRUNCATE {', '.join(TABLES)}")
        assert await archive.oldest_time(conn) is None


def test_week_bounds_are_mondays_in_utc():
    assert archive.week_name(WEEK) == "2027-01-11"
    assert archive.week_start(WEEK) == WEEK
    assert archive.week_start(NEXT - 1) == WEEK
    assert archive.week_start(NEXT) == NEXT
    assert archive.week_name(archive.week_start(0)) == "1969-12-29"
    assert archive.last_archivable_week(READY) == WEEK
    assert archive.last_archivable_week(READY - 1) == WEEK - archive.WEEK_MS
