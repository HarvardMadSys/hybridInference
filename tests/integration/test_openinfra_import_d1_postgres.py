"""Importing an OpenInfra D1 export into the gateway's tables."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
import pytest_asyncio

from serving.openinfra import archive, import_d1
from tests.fixtures import postgres
from tests.integration.test_openinfra_store_postgres import TABLES

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

T0 = 1_800_000_000_000

#: A D1 database as an older dashboard left it: fewer columns than the gateway has.
D1_SCHEMA = """
CREATE TABLE nodes (id TEXT PRIMARY KEY, hostname TEXT NOT NULL, region TEXT, cluster TEXT,
  status TEXT NOT NULL DEFAULT 'unknown', last_seen_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL, metadata TEXT, cpu_util_pct REAL);
CREATE TABLE gpus (id TEXT PRIMARY KEY, node_id TEXT NOT NULL, gpu_index INTEGER NOT NULL,
  name TEXT, processes TEXT, utilization_pct REAL, status TEXT NOT NULL DEFAULT 'unknown',
  last_seen_at INTEGER NOT NULL);
CREATE TABLE sglang_servers (id TEXT PRIMARY KEY, node_id TEXT, name TEXT, engine TEXT,
  status TEXT NOT NULL DEFAULT 'unknown', num_running_reqs INTEGER, req_per_min REAL,
  last_seen_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE gpu_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, gpu_id TEXT NOT NULL,
  ts INTEGER NOT NULL, utilization_pct REAL, memory_used_mb INTEGER);
CREATE TABLE sglang_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT NOT NULL,
  ts INTEGER NOT NULL, num_running_reqs INTEGER, req_per_min REAL);
CREATE TABLE cpu_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL,
  ts INTEGER NOT NULL, cpu_util_pct REAL);
CREATE TABLE request_events (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT NOT NULL,
  ts INTEGER NOT NULL, model TEXT, input_len INTEGER, ttft_s REAL);
CREATE TABLE sglang_rollups (grain INTEGER NOT NULL, bucket_ts INTEGER NOT NULL,
  server_id TEXT NOT NULL, samples INTEGER, running REAL, reqs_sum REAL,
  PRIMARY KEY (grain, bucket_ts, server_id));
"""


def build_d1(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(D1_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("node-a", "host-a", "lab", None, "online", T0, T0 - 1, '{"rack": 1}', 12.5),
            ("node-b", "host-b", None, None, "offline", T0, T0, "not json", None),
        ],
    )
    conn.execute(
        "INSERT INTO gpus VALUES ('node-a:0', 'node-a', 0, 'Test GPU', '[]', 50, 'online', ?)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO sglang_servers VALUES ('chat-a', 'node-a', 'chat-model', 'sglang', "
        "'online', 3, 12.0, ?, ?)",
        (T0, T0 - 5),
    )
    conn.executemany(
        "INSERT INTO gpu_samples (gpu_id, ts, utilization_pct, memory_used_mb) VALUES (?, ?, ?, ?)",
        [
            ("node-a:0", T0, 10, 100),
            ("node-a:0", T0 + 500, 20, 200),  # same second: the first is kept
            ("node-a:0", T0 + 6_000, 30, 300),
            ("node-a:0", "not a time", 40, 400),  # refused: no usable ts
        ],
    )
    conn.executemany(
        "INSERT INTO sglang_samples (server_id, ts, num_running_reqs, req_per_min) "
        "VALUES (?, ?, ?, ?)",
        [("chat-a", T0, 1, 6.0), ("chat-a", T0 + 6_000, 2, 6.0)],
    )
    conn.execute(
        "INSERT INTO cpu_samples (node_id, ts, cpu_util_pct) VALUES ('node-a', ?, 7)", (T0,)
    )
    conn.executemany(
        "INSERT INTO request_events (server_id, ts, model, input_len, ttft_s) VALUES (?, ?, ?, ?, ?)",
        [
            ("chat-a", T0 - 2_000, "org/chat", 100, 0.5),
            ("chat-a", T0 - 1_000, "org/chat", 7.6, 0.25),
            # D1 kept a duplicate; the gateway keeps one.
            ("chat-a", T0 - 2_000, "org/chat", 100, 0.5),
        ],
    )
    conn.execute("INSERT INTO sglang_rollups VALUES (1200000, ?, 'chat-a', 2, 2, 12)", (T0,))
    conn.commit()
    conn.close()


def dump_sql(database, path) -> None:
    conn = sqlite3.connect(database)
    path.write_text("PRAGMA defer_foreign_keys=TRUE;\n" + "\n".join(conn.iterdump()) + "\n")
    conn.close()


@pytest_asyncio.fixture
async def conn():
    pool = await postgres.create_pool(min_size=1, max_size=1)
    async with pool.acquire() as connection:
        await connection.execute(f"TRUNCATE {', '.join(TABLES)}")
        try:
            yield connection
        finally:
            await connection.execute(f"TRUNCATE {', '.join(TABLES)}")
    await pool.close()


async def rows(conn, sql: str):
    return [dict(row) for row in await conn.fetch(sql)]


async def test_an_export_lands_in_the_gateways_tables(conn, tmp_path):
    database, dump = tmp_path / "d1.sqlite", tmp_path / "openinfra.sql"
    build_d1(database)
    dump_sql(database, dump)

    results = await import_d1.run(dump, conn)

    assert results["gpu_samples"] == (4, 1, 0)
    assert results["gpus"] == (1, 0, 0)
    assert results["request_events"] == (3, 0, 0)
    assert results["gpu_rollups"] == (0, 0, 0)  # absent from this export
    nodes = await rows(
        conn,
        "SELECT id, hostname, region, status, metadata, cpu_util_pct, load1 "
        "FROM openinfra_nodes ORDER BY id",
    )
    assert [(n["id"], n["hostname"], n["region"], n["status"]) for n in nodes] == [
        ("node-a", "host-a", "lab", "online"),
        ("node-b", "host-b", None, "offline"),
    ]
    assert json.loads(nodes[0]["metadata"]) == {"rack": 1}
    assert nodes[1]["metadata"] is None  # it did not parse
    assert nodes[0]["load1"] is None  # a column the export lacks
    samples = await rows(
        conn,
        "SELECT gpu_id, bucket, ts, utilization_pct, memory_used_mb "
        "FROM openinfra_gpu_samples ORDER BY ts",
    )
    assert [(s["bucket"], s["ts"], s["utilization_pct"]) for s in samples] == [
        (T0 // 1_000, T0, 10.0),
        (T0 // 1_000 + 6, T0 + 6_000, 30.0),
    ]
    engines = await rows(conn, "SELECT engine_id, ts FROM openinfra_engine_samples ORDER BY ts")
    assert [e["engine_id"] for e in engines] == ["chat-a", "chat-a"]
    events = await rows(
        conn, "SELECT engine_id, ts, input_len, ttft_s FROM openinfra_request_events ORDER BY ts"
    )
    assert [(e["engine_id"], e["input_len"]) for e in events] == [("chat-a", 100), ("chat-a", 8)]
    rollups = await rows(conn, "SELECT engine_id, samples, reqs_sum FROM openinfra_engine_rollups")
    assert rollups == [{"engine_id": "chat-a", "samples": 2, "reqs_sum": 12.0}]
    gpus = await rows(conn, "SELECT id, processes FROM openinfra_gpus")
    assert json.loads(gpus[0]["processes"]) == []


async def test_running_again_adds_nothing_and_the_gateways_rows_win(conn, tmp_path):
    database = tmp_path / "d1.sqlite"
    build_d1(database)
    await conn.execute(
        "INSERT INTO openinfra_nodes (id, hostname, status, last_seen_at, created_at) "
        "VALUES ('node-a', 'renamed', 'online', $1, $1)",
        T0 + 60_000,
    )
    await conn.execute(
        "INSERT INTO openinfra_request_events (engine_id, ts) VALUES ('chat-a', $1)", T0 - 1_500
    )
    # The gateway's own first sample of this GPU, between the export's two.
    await conn.execute(
        "INSERT INTO openinfra_gpu_samples (gpu_id, bucket, ts, utilization_pct) "
        "VALUES ('node-a:0', $1, $2, 99)",
        (T0 + 3_000) // 1_000,
        T0 + 3_000,
    )

    await import_d1.run(database, conn)  # a SQLite file works as well as a dump
    results = await import_d1.run(database, conn)

    assert await conn.fetchval("SELECT hostname FROM openinfra_nodes WHERE id = 'node-a'") == (
        "renamed"
    )
    # Only what came before the gateway's own first sample, once.
    samples = await rows(conn, "SELECT ts, utilization_pct FROM openinfra_gpu_samples ORDER BY ts")
    assert [(s["ts"], s["utilization_pct"]) for s in samples] == [(T0, 10.0), (T0 + 3_000, 99.0)]
    assert results["gpu_samples"] == (4, 1, 3)
    # Only the event from before the gateway's first one was imported, once.
    events = await rows(conn, "SELECT ts FROM openinfra_request_events ORDER BY ts")
    assert [e["ts"] for e in events] == [T0 - 2_000, T0 - 1_500]


async def test_nothing_is_imported_into_an_archived_week(conn, tmp_path):
    database = tmp_path / "d1.sqlite"
    build_d1(database)
    week = archive.week_start(T0)
    await conn.execute(
        "INSERT INTO openinfra_archives (week_start, location, tables, archived_at) "
        "VALUES ($1, '/archive', '{}', 1)",
        week,
    )

    results = await import_d1.run(database, conn)

    assert await conn.fetchval("SELECT COUNT(*) FROM openinfra_gpu_samples") == 0
    assert await conn.fetchval("SELECT COUNT(*) FROM openinfra_request_events") == 0
    assert results["gpu_samples"] == (4, 1, 3)
    # Rollups stay in the database whatever the archive holds.
    assert await conn.fetchval("SELECT COUNT(*) FROM openinfra_engine_rollups") == 1


async def test_a_dry_run_writes_nothing(conn, tmp_path):
    database = tmp_path / "d1.sqlite"
    build_d1(database)
    results = await import_d1.run(database, conn, dry_run=True)
    assert results["nodes"] == (2, 0, 0)
    assert await conn.fetchval("SELECT COUNT(*) FROM openinfra_nodes") == 0


def test_the_insert_keeps_existing_rows_and_takes_the_earliest_sample():
    table = next(t for t in import_d1.TABLES if t.source == "sglang_samples")
    target = {
        name: import_d1.Column(name, kind, nullable)
        for name, kind, nullable in (
            ("engine_id", "text", False),
            ("bucket", "bigint", False),
            ("ts", "bigint", False),
            ("req_per_min", "double precision", True),
        )
    }
    table_plan = import_d1.plan(table, ["id", "server_id", "ts", "req_per_min", "gone"], target)
    assert [(source, column.name) for source, column in table_plan.pairs] == [
        ("server_id", "engine_id"),
        ("ts", "ts"),
        ("req_per_min", "req_per_min"),
    ]
    assert table_plan.convert(("chat-a", 12_345, "6")) == ("chat-a", 12_345, 6.0, 12)
    assert table_plan.convert(("chat-a", None, 6)) is None
    sql = import_d1.insert_sql(table_plan)
    assert sql.startswith(
        "INSERT INTO openinfra_engine_samples (engine_id, ts, req_per_min, bucket)"
    )
    assert "ORDER BY u.ts ON CONFLICT (engine_id, bucket) DO NOTHING" in sql


async def test_an_import_waits_for_an_archive_run(conn, tmp_path):
    """No week is archived while rows are being imported into it."""
    database = tmp_path / "d1.sqlite"
    build_d1(database)
    holder = await postgres.create_pool(min_size=1, max_size=1)
    try:
        async with holder.acquire() as archiving:
            await archiving.execute(archive.LOCK_SQL)
            task = asyncio.create_task(import_d1.run(database, conn))
            await asyncio.sleep(0.5)
            assert not task.done()
            # Asked on the other connection: the import's is waiting for the lock.
            assert await archiving.fetchval("SELECT COUNT(*) FROM openinfra_gpu_samples") == 0
            await archiving.execute(archive.UNLOCK_SQL)
            await asyncio.wait_for(task, timeout=30)
    finally:
        await holder.close()
    assert await conn.fetchval("SELECT COUNT(*) FROM openinfra_gpu_samples") == 2
    # And lets go of it.
    assert await conn.fetchval(archive.TRY_LOCK_SQL)
    await conn.execute(archive.UNLOCK_SQL)
