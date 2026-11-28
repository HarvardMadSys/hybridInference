"""OpenInfra's SQL against PostgreSQL: snapshots in, documents and rollups out."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from serving.openinfra import dashboard, store
from serving.openinfra.wire import IngestError, parse_snapshot
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

#: A time on a 20-minute and an hour boundary, so buckets line up with it.
T0 = 1_800_000_000_000
MINUTE = 60_000

TABLES = (
    "openinfra_nodes",
    "openinfra_gpus",
    "openinfra_engines",
    "openinfra_cpu_samples",
    "openinfra_gpu_samples",
    "openinfra_engine_samples",
    "openinfra_request_events",
    "openinfra_gpu_rollups",
    "openinfra_engine_rollups",
    "openinfra_cpu_rollups",
)


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


def body(
    *,
    node: str = "node-a",
    gpus: list[dict] | None = None,
    engines: list[dict] | None = None,
    cpu: dict | None = None,
    **node_fields,
) -> dict:
    return {
        "node": {"id": node, "cpu": cpu or {"cpu_util_pct": 10}, **node_fields},
        "gpus": gpus if gpus is not None else [{"index": 0, "utilization_pct": 50}],
        "sglang": engines or [],
    }


def engine(
    engine_id: str = "chat-a",
    *,
    name: str = "chat-model",
    node: str = "node-a",
    requests: list[dict] | None = None,
    **metrics,
) -> dict:
    out = {
        "server": {
            "id": engine_id,
            "node_id": node,
            "name": name,
            "engine": "sglang",
            "gpu_indexes": "0",
        },
        "metrics": {"num_running_reqs": 1, **metrics},
    }
    if requests is not None:
        out["requests"] = requests
    return out


async def ingest(pool, payload: dict, now: int) -> None:
    async with pool.acquire() as conn:
        await store.SnapshotWrite(parse_snapshot(payload), now).apply(conn)


async def fetch(pool, sql: str, *args):
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch(sql, *args)]


async def test_a_snapshot_lands_in_the_live_rows_and_one_sample_per_bucket(pool):
    payload = body(
        hostname="host-a",
        region="lab",
        metadata={"rack": 3},
        gpus=[
            {"index": 0, "name": "Test GPU", "utilization_pct": 40, "memory_total_mb": 1000},
            {"index": 1, "utilization_pct": "60", "temperature_c": 90},
        ],
        engines=[engine(num_queue_reqs=5, gen_throughput=100.5)],
    )
    await ingest(pool, payload, T0)
    # A second flush in the same 6 s bucket updates the live rows only.
    payload["gpus"][0]["utilization_pct"] = 45
    await ingest(pool, payload, T0 + 3_000)

    async with pool.acquire() as conn:
        nodes, gpus, engines = await store.load_snapshot(conn, now=T0 + 3_000)
    assert [(n["id"], n["hostname"], n["region"], n["status"]) for n in nodes] == [
        ("node-a", "host-a", "lab", "online")
    ]
    assert json.loads(nodes[0]["metadata"]) == {"rack": 3}
    assert [(g["id"], g["utilization_pct"], g["status"]) for g in gpus] == [
        ("node-a:0", 45.0, "online"),
        ("node-a:1", 60.0, "degraded"),
    ]
    assert engines[0]["id"] == "chat-a"
    assert engines[0]["gen_throughput"] == 100.5
    assert engines[0]["created_at"] == T0
    assert engines[0]["last_seen_at"] == T0 + 3_000

    samples = await fetch(pool, "SELECT gpu_id, ts, utilization_pct FROM openinfra_gpu_samples")
    assert sorted((s["gpu_id"], s["ts"], s["utilization_pct"]) for s in samples) == [
        ("node-a:0", T0, 40.0),
        ("node-a:1", T0, 60.0),
    ]
    assert len(await fetch(pool, "SELECT 1 FROM openinfra_engine_samples")) == 1
    assert len(await fetch(pool, "SELECT 1 FROM openinfra_cpu_samples")) == 1

    # The next bucket takes a sample again.
    await ingest(pool, payload, T0 + 6_000)
    assert len(await fetch(pool, "SELECT 1 FROM openinfra_gpu_samples")) == 4


async def test_absent_identity_keeps_what_was_stored_and_metrics_are_replaced(pool):
    await ingest(
        pool,
        body(
            region="lab",
            gpus=[{"index": 0, "uuid": "GPU-1", "memory_total_mb": 1000, "power_w": 300}],
            engines=[engine(model_name="org/model", num_queue_reqs=7)],
        ),
        T0,
    )
    await ingest(
        pool,
        body(gpus=[{"index": 0, "utilization_pct": 5}], engines=[engine()]),
        T0 + 6_000,
    )
    async with pool.acquire() as conn:
        nodes, gpus, engines = await store.load_snapshot(conn, now=T0 + 6_000)
    assert nodes[0]["region"] == "lab"
    assert nodes[0]["hostname"] == "node-a"
    gpu = gpus[0]
    assert (gpu["uuid"], gpu["memory_total_mb"]) == ("GPU-1", 1000.0)
    assert gpu["power_w"] is None
    assert engines[0]["num_queue_reqs"] is None
    assert engines[0]["model_name"] == "org/model"


async def test_whatever_stops_reporting_reads_as_offline(pool):
    await ingest(pool, body(engines=[engine()]), T0)
    async with pool.acquire() as conn:
        fresh = await store.load_snapshot(conn, now=T0 + store.STALE_MS)
        stale = await store.load_snapshot(conn, now=T0 + store.STALE_MS + 1)
    assert {row["status"] for rows in fresh for row in rows} == {"online"}
    assert {row["status"] for rows in stale for row in rows} == {"offline"}


async def test_prometheus_text_with_two_models_gives_two_engines(pool):
    text = "\n".join(
        [
            "# HELP sglang:num_running_reqs running",
            'sglang:num_running_reqs{model_name="m-one",tp_rank="0"} 3',
            'sglang:num_running_reqs{model_name="m-two",tp_rank="0"} 4',
            'sglang:num_queue_reqs{model_name="m-one"} 1',
        ]
    )
    payload = {
        "node": {"id": "node-a"},
        "sglang": {"server": {"id": "multi", "node_id": "node-a"}, "prometheus": text},
    }
    await ingest(pool, payload, T0)
    rows = await fetch(
        pool,
        "SELECT id, name, model_name, num_running_reqs, num_queue_reqs "
        "FROM openinfra_engines ORDER BY id",
    )
    assert rows == [
        {
            "id": "multi:m-one",
            "name": "multi:m-one",
            "model_name": "m-one",
            "num_running_reqs": 3.0,
            "num_queue_reqs": 1.0,
        },
        {
            "id": "multi:m-two",
            "name": "multi:m-two",
            "model_name": "m-two",
            "num_running_reqs": 4.0,
            "num_queue_reqs": None,
        },
    ]


async def test_an_engine_without_metrics_writes_nothing(pool):
    payload = body(engines=[{"server": {"id": "empty"}}])
    with pytest.raises(IngestError, match="metrics or prometheus"):
        await ingest(pool, payload, T0)
    assert await fetch(pool, "SELECT 1 FROM openinfra_nodes") == []


async def test_served_totals_integrate_rates_over_the_sample_spacing(pool):
    # Ten samples 6 s apart: 60 requests/min, 100 decode, 50 prefill and
    # 30 cached-prefill tokens/s each.
    for i in range(10):
        await ingest(
            pool,
            body(
                engines=[
                    engine(
                        req_per_min=60, gen_throughput=100, prefill_tps=50, cached_prefill_tps=30
                    )
                ]
            ),
            T0 + i * 6_000,
        )
    async with pool.acquire() as conn:
        served = await store.load_served(conn, now=T0 + 60_000)
    hour = served["hour"]
    # 10 samples x 6 s = 60 s: 60 requests, 6000 decode, 3000 prefill, 1800 cached.
    assert hour == {
        "reqs": 60.0,
        "cache_tokens": 1800,
        "prefill_tokens": 3000,
        "decode_tokens": 6000,
    }
    # No rollups yet: the day falls back to the samples.
    assert served["day"] == hour
    assert served["servers"] == [{"id": "chat-a", "name": "chat-model", "hour": hour, "day": hour}]


async def test_a_sample_without_prefill_tps_is_estimated_from_its_cached_rate(pool):
    await ingest(pool, body(engines=[engine(cached_prefill_tps=30, cache_hit_rate=0.75)]), T0)
    async with pool.acquire() as conn:
        served = await store.load_served(conn, now=T0 + 1_000)
    # One sample counts one 6 s bucket: 30 x (0.25 / 0.75) = 10 tokens/s.
    assert served["hour"]["prefill_tokens"] == 60
    assert served["hour"]["cache_tokens"] == 180


async def test_history_keeps_the_newest_sample_of_each_minute(pool):
    for offset, util in ((0, 10), (30_000, 20), (60_000, 30)):
        await ingest(
            pool,
            body(
                gpus=[
                    {
                        "index": 0,
                        "utilization_pct": util,
                        "memory_used_mb": 50,
                        "memory_total_mb": 200,
                    }
                ],
                engines=[engine(num_running_reqs=util)],
                cpu={"cpu_util_pct": util},
            ),
            T0 + offset,
        )
    async with pool.acquire() as conn:
        history = await store.load_history(conn, 2, now=T0 + 90_000)
    assert history["grain_ms"] == store.GRAIN_1M
    assert [(r["ts"], r["utilization_pct"], r["memory_pct"]) for r in history["gpu"]] == [
        (T0 + 30_000, 20.0, 25.0),
        (T0 + 60_000, 30.0, 25.0),
    ]
    assert [(r["server_id"], r["running"]) for r in history["sglang"]] == [
        ("chat-a", 20.0),
        ("chat-a", 30.0),
    ]
    assert [r["cpu_util_pct"] for r in history["cpu"]] == [20.0, 30.0]
    assert history["gpu"][0]["node_id"] == "node-a"
    assert history["gpu"][0]["gpu_index"] == 0


async def test_rollups_keep_each_buckets_last_sample_and_the_rate_sums(pool):
    # Two 20-minute buckets: three samples in the first, one in the second.
    for offset, running in ((0, 1), (6_000, 2), (19 * MINUTE, 3), (20 * MINUTE, 9)):
        await ingest(
            pool,
            body(
                gpus=[{"index": 0, "utilization_pct": running * 10}],
                engines=[engine(num_running_reqs=running, req_per_min=6, gen_throughput=10)],
            ),
            T0 + offset,
        )
    now = T0 + 21 * MINUTE
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, now)
        await store.refresh_rollups(conn, now)  # idempotent

    rows = await fetch(
        pool,
        "SELECT grain, bucket_ts, samples, running, reqs_sum, decode_tps_sum "
        "FROM openinfra_engine_rollups ORDER BY grain, bucket_ts",
    )
    assert rows == [
        {
            "grain": store.GRAIN_20M,
            "bucket_ts": T0,
            "samples": 3,
            "running": 3.0,
            "reqs_sum": 18.0,
            "decode_tps_sum": 30.0,
        },
        {
            "grain": store.GRAIN_20M,
            "bucket_ts": T0 + 20 * MINUTE,
            "samples": 1,
            "running": 9.0,
            "reqs_sum": 6.0,
            "decode_tps_sum": 10.0,
        },
        {
            "grain": store.GRAIN_1H,
            "bucket_ts": T0,
            "samples": 4,
            "running": 9.0,
            "reqs_sum": 24.0,
            "decode_tps_sum": 40.0,
        },
    ]
    gpu_rows = await fetch(
        pool,
        "SELECT grain, bucket_ts, samples, utilization_pct FROM openinfra_gpu_rollups "
        "WHERE grain = $1 ORDER BY bucket_ts",
        store.GRAIN_20M,
    )
    assert [(r["samples"], r["utilization_pct"]) for r in gpu_rows] == [(3, 30.0), (1, 90.0)]
    cpu_rows = await fetch(
        pool,
        "SELECT samples FROM openinfra_cpu_rollups WHERE grain = $1 ORDER BY bucket_ts",
        store.GRAIN_20M,
    )
    assert [r["samples"] for r in cpu_rows] == [3, 1]

    async with pool.acquire() as conn:
        history = await store.load_history(conn, 24, now=now)
        served = await store.load_served(conn, now=now)
    assert history["grain_ms"] == store.GRAIN_20M
    assert [(r["ts"], r["running"]) for r in history["sglang"]] == [
        (T0, 3.0),
        (T0 + 20 * MINUTE, 9.0),
    ]
    # Each bucket's mean rate times its part of the window: the first bucket's
    # 20 minutes (6 requests/min -> 120) and the second's one minute so far (6).
    assert served["day"]["reqs"] == 126.0
    assert served["day"]["decode_tokens"] == 12_600


async def test_a_late_flush_is_counted_by_the_next_refresh(pool):
    await ingest(pool, body(engines=[engine(req_per_min=6)]), T0)
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, T0 + 21 * MINUTE)
    # A flush stamped in the first bucket that committed after its refresh.
    await ingest(pool, body(engines=[engine(req_per_min=6)]), T0 + 12_000)
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, T0 + 26 * MINUTE)
    rows = await fetch(
        pool,
        "SELECT samples FROM openinfra_engine_rollups WHERE grain = $1 AND bucket_ts = $2",
        store.GRAIN_20M,
        T0,
    )
    assert rows == [{"samples": 2}]


async def test_retired_engines_are_left_out_of_every_read(pool):
    await ingest(
        pool,
        body(
            engines=[
                engine("live", name="live-model", requests=[{"ts": T0, "input_len": 5}]),
                engine("old", name="old-model", requests=[{"ts": T0, "input_len": 7}]),
            ]
        ),
        T0,
    )
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, T0 + MINUTE)
        doc = await dashboard.build_dashboard(conn, retired=("old",), now=T0 + 1_000)
        history = await store.load_history(conn, 2, retired=("old",), now=T0 + 1_000)
    assert [e["id"] for e in doc["sglang"]] == ["live"]
    assert [s["id"] for s in doc["served"]["servers"]] == ["live"]
    assert [r["server_id"] for r in doc["requests"]] == ["live"]
    assert {r["server_id"] for r in history["sglang"]} == {"live"}


async def test_request_events_list_each_engines_newest_first(pool):
    events = [{"ts": T0 - i * 1_000, "input_len": i, "output_len": 2.5} for i in range(20)]
    await ingest(pool, body(engines=[engine(requests=events)]), T0)
    await ingest(
        pool,
        body(engines=[engine(requests=[{"ts": 1_800_000_100, "e2e_s": 1.5}])]),
        T0 + 6_000,
    )
    async with pool.acquire() as conn:
        rows = await store.load_recent_requests(
            conn, [("chat-a", "chat-a"), ("missing", "missing")]
        )
    assert len(rows) == 21
    # Seconds are read as milliseconds' thousandths: 1_800_000_100 s.
    assert rows[0]["ts"] == 1_800_000_100_000
    assert rows[0]["e2e_s"] == 1.5
    assert rows[1]["ts"] == T0
    assert rows[1]["output_len"] == 3  # token counts round half up
    assert rows[0]["server_id"] == "chat-a"


async def test_the_dashboard_document(pool):
    await ingest(
        pool,
        body(
            node="node-b",
            cluster="lab",
            cpu={"cpu_util_pct": 20, "mem_used_mb": 1024, "mem_total_mb": 4096},
            gpus=[
                {
                    "index": 0,
                    "utilization_pct": 90,
                    "memory_used_mb": 10,
                    "memory_total_mb": 40,
                    "power_w": 300,
                    "nvlink_tx_bps": 1e9,
                    "nvlink_rx_bps": 1e9,
                },
                {"index": 1, "utilization_pct": 80, "memory_total_mb": 40},
            ],
            engines=[
                engine("b-zeta", name="zeta", node="node-b", num_queue_reqs=250),
                engine("b-alpha", name="alpha", node="node-b", num_queue_reqs=1),
                engine("b-mid", name="mid", node="node-b"),
            ],
        ),
        T0,
    )
    async with pool.acquire() as conn:
        doc = await dashboard.build_dashboard(conn, order=("mid",), now=T0 + 1_000)
    assert doc["generated_at"] == T0 + 1_000
    assert doc["fleet"] == "busy"  # 251 queued
    assert [e["name"] for e in doc["sglang"]] == ["mid", "alpha", "zeta"]
    kpis = doc["kpis"]
    assert kpis["gpus"] == 2
    assert kpis["avg_utilization_pct"] == 85.0
    assert kpis["queue_reqs"] == 251.0
    assert kpis["memory_total_mb"] == 80.0
    assert kpis["nvlink_gbps"] == 2.0
    assert kpis["ram_total_mb"] == 4096.0
    assert doc["nodes"][0]["gpu_count"] == 2
    assert doc["nodes"][0]["metadata"] == []
    assert doc["gpus"][0]["hostname"] == "node-b"
    assert doc["gpus"][0]["cluster"] == "lab"
    assert doc["gpus"][0]["processes"] == []
    assert doc["sglang"][0]["hostname"] == "node-b"
    assert "created_at" not in doc["sglang"][0]
    json.dumps(doc)  # every value is plain JSON


async def test_the_export_document(pool):
    await ingest(pool, body(engines=[engine()]), T0)
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, T0 + MINUTE)
        export = await dashboard.build_export(conn, 7.4, now=T0 + MINUTE)
    assert export["days"] == 7
    assert export["hours"] == 168
    assert export["grain_ms"] == store.GRAIN_1H
    assert export["servers"][0]["id"] == "chat-a"
    assert export["history"]["sglang"][0]["server_id"] == "chat-a"
    json.dumps(export)


async def test_the_day_counts_only_the_time_a_reporter_was_reporting(pool):
    # One engine starts halfway through a bucket, another stops after five
    # minutes of it; each sends 6 requests a minute every 6 s.
    for i in range(100):
        await ingest(
            pool, body(engines=[engine("late", req_per_min=6)]), T0 + 10 * MINUTE + i * 6_000
        )
    for i in range(50):
        await ingest(pool, body(engines=[engine("early", req_per_min=6)]), T0 + i * 6_000)
    now = T0 + 45 * MINUTE
    async with pool.acquire() as conn:
        await store.refresh_rollups(conn, T0 + 21 * MINUTE)
        served = await store.load_served(conn, now=now)
    day = {row["id"]: row["day"]["reqs"] for row in served["servers"]}
    # Ten minutes and five minutes of 6 requests a minute, not two whole buckets.
    assert day == {"late": 60.0, "early": 30.0}
    spans = await fetch(
        pool,
        "SELECT engine_id, first_ts, last_ts FROM openinfra_engine_rollups "
        "WHERE grain = $1 ORDER BY engine_id",
        store.GRAIN_20M,
    )
    assert spans == [
        {"engine_id": "early", "first_ts": T0, "last_ts": T0 + 49 * 6_000},
        {
            "engine_id": "late",
            "first_ts": T0 + 10 * MINUTE,
            "last_ts": T0 + 10 * MINUTE + 99 * 6_000,
        },
    ]


async def test_a_server_with_two_models_lists_its_requests_under_each(pool):
    text = 'sglang:num_running_reqs{model_name="m-one"} 1\nsglang:num_running_reqs{model_name="m-two"} 2\n'
    payload = {
        "node": {"id": "node-a"},
        "sglang": {
            "server": {"id": "multi", "node_id": "node-a"},
            "prometheus": text,
            "requests": [{"ts": T0, "input_len": 10, "e2e_s": 1.0}],
        },
    }
    await ingest(pool, payload, T0)
    async with pool.acquire() as conn:
        doc = await dashboard.build_dashboard(conn, now=T0 + 1_000)
    assert [e["id"] for e in doc["sglang"]] == ["multi:m-one", "multi:m-two"]
    assert sorted(r["server_id"] for r in doc["requests"]) == ["multi:m-one", "multi:m-two"]
    assert {r["input_len"] for r in doc["requests"]} == {10}


async def test_text_and_metrics_for_one_model_make_one_engine(pool):
    payload = {
        "node": {"id": "node-a"},
        "sglang": {
            "server": {"id": "chat-a", "node_id": "node-a", "model_name": "org/chat"},
            "prometheus": 'sglang:num_running_reqs{model_name="org/chat"} 1\n'
            'sglang:num_queue_reqs{model_name="org/chat"} 4\n',
            "metrics": {"model_name": "org/chat", "num_running_reqs": 7},
        },
    }
    await ingest(pool, payload, T0)
    rows = await fetch(
        pool, "SELECT id, server_id, num_running_reqs, num_queue_reqs FROM openinfra_engines"
    )
    # One row, the parsed metrics winning and the text filling in the rest.
    assert rows == [
        {"id": "chat-a", "server_id": "chat-a", "num_running_reqs": 7.0, "num_queue_reqs": 4.0}
    ]
    assert len(await fetch(pool, "SELECT 1 FROM openinfra_engine_samples")) == 1


async def test_a_request_event_sent_twice_is_stored_once(pool):
    events = [
        {"ts": T0, "input_len": 10, "output_len": 5, "e2e_s": 1.5},
        {"ts": T0, "input_len": 10},  # NULL fields still identify it
        {"ts": T0, "input_len": 11, "output_len": 5, "e2e_s": 1.5},  # a different request
    ]
    await ingest(pool, body(engines=[engine(requests=events)]), T0)
    # The reporter resends the same window after a lost answer.
    await ingest(pool, body(engines=[engine(requests=events)]), T0 + 6_000)
    rows = await fetch(pool, "SELECT input_len, output_len FROM openinfra_request_events")
    assert sorted((r["input_len"], r["output_len"] or 0) for r in rows) == [
        (10, 0),
        (10, 5),
        (11, 5),
    ]
