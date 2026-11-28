"""OpenInfra's SQL: what a snapshot writes, what the routes read, and the rollups.

Every time is milliseconds since the Unix epoch. The schema is revision
``0025_openinfra``'s; its docstring describes the tables.

**Samples.** Each host, GPU and engine keeps at most one sample per 6-second
bucket (:data:`SAMPLE_MS`): the bucket is part of the primary key and a second
sample in it is dropped, so two flushes racing each other cannot both land.
At a 5-second reporting interval every bucket still gets a sample, so the
one-minute history and the rollups keep all their points, and the served
totals integrate rates over a spacing of 6 seconds.

**Status.** A snapshot stores ``online`` or ``degraded`` (a GPU at 85 °C or
with ECC errors; an engine with more than 2000 queued requests or its SMs at
95 %). Whatever is silent for :data:`STALE_MS` is read as ``offline``, at the
moment it is read; nothing has to mark it.

**Rollups.** :func:`refresh_rollups` recomputes, for each grain, the buckets
from the newest one that has a row (and always the one before it, for a late
flush) up to the current one: a 20-minute bucket from its samples, an hour
from its three 20-minute rollups. A grain with no rollups yet, or that has
fallen behind, starts from its backfill horizon instead, and an hour older
than the 20-minute horizon is computed from its samples. A rollup keeps its
bucket's last sample, the number of samples, and for engines the sums of the
request, cached-prefill, uncached-prefill and decode rates.

**Retired engines** (:func:`serving.openinfra.config.retired_engines`) stay
out of every read: live rows, served totals, history and request events.
Their rows, samples and rollups are kept.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from serving.openinfra.fields import (
    CPU_FIELDS,
    CPU_HISTORY,
    ENGINE_HISTORY,
    ENGINE_METRICS,
    ENGINE_SUMS,
    GPU_NUMBERS,
    GPU_ROLLUP_FIELDS,
    GPU_SAMPLE_FIELDS,
    REQUEST_FIELDS,
)
from serving.openinfra.prometheus import engine_metrics
from serving.openinfra.wire import IngestError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from serving.openinfra.wire import EngineIngest, ServerIngest, Snapshot

#: One sample per host, GPU and engine per bucket of this many milliseconds.
SAMPLE_MS = 6_000
GRAIN_1M = 60_000
GRAIN_20M = 20 * 60_000
GRAIN_1H = 60 * 60_000
#: Silence after which a host, GPU or engine reads as offline.
STALE_MS = 90_000
#: How far back a grain with no rollups, or one that fell behind, is computed.
ROLLUP_20M_BACKFILL_HOURS = 36
ROLLUP_1H_BACKFILL_HOURS = 30 * 24
#: Request events listed per engine.
REQUESTS_PER_ENGINE = 40

#: A GPU this hot is degraded.
_HOT_C = 85
#: An engine with this many queued requests, or its SMs this busy, is degraded.
_DEGRADED_QUEUE = 2000
_DEGRADED_SM_PCT = 95


def bucket_start(ts: int, grain: int) -> int:
    """The start of the *grain*-millisecond bucket holding *ts*."""
    return ts // grain * grain


def _json(value: Any) -> str | None:
    """A snapshot's free-form value as JSON text; ``None`` when the key was absent (``...``)."""
    return None if value is ... else json.dumps(value)


def _uncached_prefill(alias: str) -> str:
    """A sample's uncached prompt rate.

    ``prefill_tps`` arrives as the uncached rate, so it is taken as it is. Only
    a sample with no ``prefill_tps`` at all has it estimated from its cached
    rate and hit rate; a 0 is a measured zero.
    """
    p = f"{alias}."
    return (
        f"CASE WHEN COALESCE({p}prefill_tps, 0) > 0 THEN {p}prefill_tps "
        f"WHEN {p}prefill_tps IS NULL AND {p}cache_hit_rate > 0 AND {p}cache_hit_rate < 1 "
        f"AND COALESCE({p}cached_prefill_tps, 0) > 0 "
        f"THEN {p}cached_prefill_tps * (1.0 - {p}cache_hit_rate) / {p}cache_hit_rate "
        "ELSE 0 END"
    )


def _placeholders(count: int, start: int = 1) -> str:
    return ", ".join(f"${i}" for i in range(start, start + count))


def _columns(names: Iterable[str]) -> str:
    return ", ".join(names)


def _excluded(names: Iterable[str]) -> str:
    return ", ".join(f"{name} = EXCLUDED.{name}" for name in names)


# --- writes ------------------------------------------------------------------

_NODE_COLUMNS = (
    "id",
    "hostname",
    "region",
    "cluster",
    "status",
    "last_seen_at",
    "created_at",
    "metadata",
    *CPU_FIELDS,
)
_UPSERT_NODE = (
    f"INSERT INTO openinfra_nodes ({_columns(_NODE_COLUMNS)}) "
    f"VALUES ($1, $2, $3, $4, 'online', $5, $5, $6::jsonb, {_placeholders(len(CPU_FIELDS), 7)}) "
    "ON CONFLICT (id) DO UPDATE SET "
    "hostname = EXCLUDED.hostname, "
    "region = COALESCE(EXCLUDED.region, openinfra_nodes.region), "
    "cluster = COALESCE(EXCLUDED.cluster, openinfra_nodes.cluster), "
    "status = 'online', "
    "last_seen_at = EXCLUDED.last_seen_at, "
    "metadata = COALESCE(EXCLUDED.metadata, openinfra_nodes.metadata), "
    + ", ".join(
        f"{name} = COALESCE(EXCLUDED.{name}, openinfra_nodes.{name})" for name in CPU_FIELDS
    )
)
_INSERT_CPU_SAMPLE = (
    f"INSERT INTO openinfra_cpu_samples (node_id, bucket, ts, {_columns(CPU_FIELDS)}) "
    f"VALUES ($1, $2, $3, {_placeholders(len(CPU_FIELDS), 4)}) "
    "ON CONFLICT (node_id, bucket) DO NOTHING"
)

#: GPU fields a snapshot only fills in, keeping the stored value when it sends none.
_GPU_KEPT = frozenset({"memory_total_mb", "power_limit_w"})
_GPU_COLUMNS = (
    "id",
    "node_id",
    "gpu_index",
    "uuid",
    "name",
    "driver",
    "pstate",
    "processes",
    "status",
    "last_seen_at",
    *GPU_NUMBERS,
)
_UPSERT_GPU = (
    f"INSERT INTO openinfra_gpus ({_columns(_GPU_COLUMNS)}) "
    f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, "
    f"{_placeholders(len(GPU_NUMBERS), 11)}) "
    "ON CONFLICT (id) DO UPDATE SET "
    "uuid = COALESCE(EXCLUDED.uuid, openinfra_gpus.uuid), "
    "name = COALESCE(EXCLUDED.name, openinfra_gpus.name), "
    "driver = COALESCE(EXCLUDED.driver, openinfra_gpus.driver), "
    "pstate = EXCLUDED.pstate, "
    "processes = COALESCE(EXCLUDED.processes, openinfra_gpus.processes), "
    "status = EXCLUDED.status, "
    "last_seen_at = EXCLUDED.last_seen_at, "
    + ", ".join(
        f"{name} = COALESCE(EXCLUDED.{name}, openinfra_gpus.{name})"
        if name in _GPU_KEPT
        else f"{name} = EXCLUDED.{name}"
        for name in GPU_NUMBERS
    )
)
_INSERT_GPU_SAMPLE = (
    f"INSERT INTO openinfra_gpu_samples (gpu_id, bucket, ts, {_columns(GPU_SAMPLE_FIELDS)}) "
    f"VALUES ($1, $2, $3, {_placeholders(len(GPU_SAMPLE_FIELDS), 4)}) "
    "ON CONFLICT (gpu_id, bucket) DO NOTHING"
)

_ENGINE_IDENTITY = ("node_id", "name", "model_name", "endpoint", "engine", "gpu_indexes")
_ENGINE_COLUMNS = (
    "id",
    *_ENGINE_IDENTITY,
    "status",
    *ENGINE_METRICS,
    "last_seen_at",
    "created_at",
)
_UPSERT_ENGINE = (
    f"INSERT INTO openinfra_engines ({_columns(_ENGINE_COLUMNS)}) "
    f"VALUES ({_placeholders(len(_ENGINE_COLUMNS) - 1)}, "
    f"${len(_ENGINE_COLUMNS) - 1}) "
    "ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(
        f"{name} = COALESCE(EXCLUDED.{name}, openinfra_engines.{name})" for name in _ENGINE_IDENTITY
    )
    + ", status = EXCLUDED.status, "
    + _excluded(ENGINE_METRICS)
    + ", last_seen_at = EXCLUDED.last_seen_at"
)
_INSERT_ENGINE_SAMPLE = (
    f"INSERT INTO openinfra_engine_samples (engine_id, bucket, ts, {_columns(ENGINE_METRICS)}) "
    f"VALUES ($1, $2, $3, {_placeholders(len(ENGINE_METRICS), 4)}) "
    "ON CONFLICT (engine_id, bucket) DO NOTHING"
)
_INSERT_REQUEST = (
    f"INSERT INTO openinfra_request_events (engine_id, ts, {_columns(REQUEST_FIELDS)}) "
    f"VALUES ($1, $2, {_placeholders(len(REQUEST_FIELDS), 3)})"
)


def gpu_id(node_id: str, index: int) -> str:
    """A GPU's id: its host's id and its index, so host ids must be unique."""
    return f"{node_id}:{index}"


def _gpu_status(numbers: dict[str, float | None]) -> str:
    hot = (numbers.get("temperature_c") or 0) >= _HOT_C
    ecc = (numbers.get("ecc_errors") or 0) > 0
    return "degraded" if hot or ecc else "online"


def _engine_status(metrics: dict[str, Any]) -> str:
    queue = metrics.get("num_queue_reqs") or 0
    sm = metrics.get("sm_util_pct") or 0
    return "degraded" if queue > _DEGRADED_QUEUE or sm >= _DEGRADED_SM_PCT else "online"


def _engine_rows(engine: EngineIngest) -> list[tuple[ServerIngest, str, dict[str, Any]]]:
    """Each engine row a snapshot's engine yields: (identity, id, metrics).

    Raw Prometheus text can expose several models; each becomes its own row,
    ``<server id>:<model>``. Parsed ``metrics`` add one more.

    Raises:
        IngestError: The engine sent neither metrics nor Prometheus text.
    """
    listed: list[dict[str, Any]] = []
    if engine.prometheus:
        listed.extend(engine_metrics(engine.prometheus))
    if engine.metrics:
        listed.append(engine.metrics)
    if not listed:
        raise IngestError("sglang requires metrics or prometheus text")
    rows = []
    for metrics in listed:
        model = metrics.get("model_name") or engine.server.model_name
        row_id = f"{engine.server.id}:{model}" if len(listed) > 1 and model else engine.server.id
        rows.append((engine.server, row_id, {**metrics, "model_name": model}))
    return rows


class SnapshotWrite:
    """A parsed snapshot as the statements that store it, built before anything is written.

    Building it is where a malformed engine is refused, so a refused body
    writes nothing.
    """

    def __init__(self, snapshot: Snapshot, now: int) -> None:
        """Lay out *snapshot*'s rows as of *now* (milliseconds).

        Raises:
            IngestError: An engine sent neither metrics nor Prometheus text.
        """
        bucket = now // SAMPLE_MS
        self.node: tuple[Any, ...] | None = None
        self.cpu_sample: tuple[Any, ...] | None = None
        self.gpus: list[tuple[Any, ...]] = []
        self.gpu_samples: list[tuple[Any, ...]] = []
        self.engines: list[tuple[Any, ...]] = []
        self.engine_samples: list[tuple[Any, ...]] = []
        self.requests: list[tuple[Any, ...]] = []

        node = snapshot.node
        if node is not None:
            cpu = node.cpu or {}
            self.node = (
                node.id,
                node.hostname or node.id,
                node.region,
                node.cluster,
                now,
                _json(node.metadata),
                *(cpu.get(name) for name in CPU_FIELDS),
            )
            if node.cpu is not None:
                self.cpu_sample = (
                    node.id,
                    bucket,
                    now,
                    *(cpu.get(name) for name in CPU_FIELDS),
                )
            for gpu in snapshot.gpus:
                gid = gpu_id(node.id, gpu.index)
                self.gpus.append(
                    (
                        gid,
                        node.id,
                        gpu.index,
                        gpu.uuid,
                        gpu.name,
                        gpu.driver,
                        gpu.pstate,
                        _json(gpu.processes),
                        _gpu_status(gpu.numbers),
                        now,
                        *(gpu.numbers.get(name) for name in GPU_NUMBERS),
                    )
                )
                self.gpu_samples.append(
                    (gid, bucket, now, *(gpu.numbers.get(name) for name in GPU_SAMPLE_FIELDS))
                )

        for engine in snapshot.engines:
            for server, row_id, metrics in _engine_rows(engine):
                self.engines.append(
                    (
                        row_id,
                        server.node_id,
                        server.name or row_id,
                        metrics["model_name"],
                        server.endpoint,
                        server.engine,
                        server.gpu_indexes,
                        _engine_status(metrics),
                        *(metrics.get(name) for name in ENGINE_METRICS),
                        now,
                    )
                )
                self.engine_samples.append(
                    (row_id, bucket, now, *(metrics.get(name) for name in ENGINE_METRICS))
                )
            model = engine.server.model_name or (engine.metrics or {}).get("model_name")
            for event in engine.requests:
                ts = event["ts"] if event["ts"] and event["ts"] > 0 else now
                self.requests.append(
                    (
                        engine.server.id,
                        ts,
                        model,
                        *(event.get(name) for name in REQUEST_FIELDS if name != "model"),
                    )
                )

    async def apply(self, conn: Any) -> None:
        """Run the statements on *conn*, in one transaction."""
        async with conn.transaction():
            if self.node is not None:
                await conn.execute(_UPSERT_NODE, *self.node)
            if self.cpu_sample is not None:
                await conn.execute(_INSERT_CPU_SAMPLE, *self.cpu_sample)
            if self.gpus:
                await conn.executemany(_UPSERT_GPU, self.gpus)
                # A GPU listed twice keeps its first sample.
                await conn.executemany(_INSERT_GPU_SAMPLE, self.gpu_samples)
            if self.engines:
                await conn.executemany(_UPSERT_ENGINE, self.engines)
                await conn.executemany(_INSERT_ENGINE_SAMPLE, self.engine_samples)
            if self.requests:
                await conn.executemany(_INSERT_REQUEST, self.requests)


# --- reads -------------------------------------------------------------------


def _live(rows: Iterable[Any], now: int) -> list[dict[str, Any]]:
    """Rows as dicts, with whatever has been silent for :data:`STALE_MS` read as offline."""
    out = []
    for row in rows:
        item = dict(row)
        if item["last_seen_at"] < now - STALE_MS:
            item["status"] = "offline"
        out.append(item)
    return out


async def load_snapshot(
    conn: Any, *, retired: Sequence[str] = (), now: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Every host, GPU and engine (retired ones left out), as of *now*."""
    nodes = await conn.fetch("SELECT * FROM openinfra_nodes ORDER BY cluster NULLS FIRST, hostname")
    gpus = await conn.fetch("SELECT * FROM openinfra_gpus ORDER BY node_id, gpu_index")
    engines = await conn.fetch(
        "SELECT * FROM openinfra_engines WHERE NOT (id = ANY($1::text[])) "
        "ORDER BY name NULLS FIRST, id",
        list(retired),
    )
    return _live(nodes, now), _live(gpus, now), _live(engines, now)


async def load_recent_requests(conn: Any, engine_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Each engine's newest :data:`REQUESTS_PER_ENGINE` request events, engine by engine."""
    if not engine_ids:
        return []
    rows = await conn.fetch(
        f"SELECT r.engine_id AS server_id, r.ts, {_columns(f'r.{n}' for n in REQUEST_FIELDS)} "
        "FROM unnest($1::text[]) WITH ORDINALITY AS ids(id, position) "
        "CROSS JOIN LATERAL ("
        "SELECT * FROM openinfra_request_events e WHERE e.engine_id = ids.id "
        "ORDER BY e.ts DESC LIMIT $2"
        ") r ORDER BY ids.position, r.ts DESC",
        list(engine_ids),
        REQUESTS_PER_ENGINE,
    )
    return [dict(row) for row in rows]


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _served(reqs: Any, cache: Any, prefill: Any, decode: Any) -> dict[str, float | int]:
    def num(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if math.isfinite(result) else 0.0

    return {
        "reqs": _round_half_up(num(reqs) * 10) / 10,
        "cache_tokens": _round_half_up(num(cache)),
        "prefill_tokens": _round_half_up(num(prefill)),
        "decode_tokens": _round_half_up(num(decode)),
    }


_ZERO_SERVED = {"reqs": 0, "cache_tokens": 0, "prefill_tokens": 0, "decode_tokens": 0}


async def _served_from_samples(
    conn: Any, since: int, retired: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Requests and tokens per engine since *since*: each sample's rates times their spacing."""
    rows = await conn.fetch(
        "SELECT sv.id AS server_id, sv.name, "
        "SUM(COALESCE(s.req_per_min, 0)) AS reqs, "
        "SUM(COALESCE(s.cached_prefill_tps, 0)) AS cached_tps, "
        f"SUM({_uncached_prefill('s')}) AS prefill_tps, "
        "SUM(COALESCE(s.gen_throughput, 0)) AS decode_tps, "
        "COUNT(*) AS n, MIN(s.ts) AS t0, MAX(s.ts) AS t1 "
        "FROM openinfra_engines sv "
        "JOIN openinfra_engine_samples s "
        "ON s.engine_id = sv.id AND s.bucket >= $1 AND s.ts >= $2 "
        "WHERE NOT (sv.id = ANY($3::text[])) "
        "GROUP BY sv.id, sv.name",
        since // SAMPLE_MS,
        since,
        list(retired),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        n, t0, t1 = row["n"], row["t0"], row["t1"]
        # The samples' average spacing, or one bucket for a single sample.
        dt = (t1 - t0) / 1000 / (n - 1) if n > 1 and t1 > t0 else SAMPLE_MS / 1000
        out[row["server_id"]] = {
            "name": row["name"],
            **_served(
                (row["reqs"] or 0) * dt / 60,
                (row["cached_tps"] or 0) * dt,
                (row["prefill_tps"] or 0) * dt,
                (row["decode_tps"] or 0) * dt,
            ),
        }
    return out


async def _served_from_rollups(
    conn: Any, grain: int, since: int, now: int, retired: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Requests and tokens per engine from rollups between *since* and *now*.

    Each bucket's mean rates times the time its samples cover inside the
    window: from its first sample (or *since*) to a sample spacing past its
    last (or the bucket's end), and up to *now* for the bucket still filling.
    A reporter that started or stopped inside a bucket is counted for the time
    it reported, and no bucket is extrapolated past *now*. A rollup without a
    recorded span (one imported from the dashboard's D1 database) covers its
    whole bucket.
    """
    rows = await conn.fetch(
        "SELECT r.engine_id AS server_id, sv.name, "
        "SUM(COALESCE(r.reqs_sum, 0) / NULLIF(r.samples, 0) * w.ms) AS reqs, "
        "SUM(COALESCE(r.cached_tps_sum, 0) / NULLIF(r.samples, 0) * w.ms) AS cached_tps, "
        "SUM(COALESCE(r.prefill_tps_sum, 0) / NULLIF(r.samples, 0) * w.ms) AS prefill_tps, "
        "SUM(COALESCE(r.decode_tps_sum, 0) / NULLIF(r.samples, 0) * w.ms) AS decode_tps "
        "FROM openinfra_engine_rollups r "
        "CROSS JOIN LATERAL ("
        "SELECT CASE WHEN r.samples > 1 AND r.last_ts > r.first_ts "
        "THEN (r.last_ts - r.first_ts) / (r.samples - 1) ELSE $5 END AS spacing"
        ") g "
        "CROSS JOIN LATERAL ("
        "SELECT GREATEST("
        "CASE WHEN r.bucket_ts + r.grain > $3 THEN $3 "
        "ELSE LEAST(r.bucket_ts + r.grain, COALESCE(r.last_ts + g.spacing, r.bucket_ts + r.grain)) "
        "END - GREATEST(r.bucket_ts, $2, COALESCE(r.first_ts, r.bucket_ts)), 0) AS ms"
        ") w "
        "JOIN openinfra_engines sv ON sv.id = r.engine_id "
        "WHERE r.grain = $1 AND r.bucket_ts > $2 - $1 AND r.bucket_ts < $3 "
        "AND NOT (sv.id = ANY($4::text[])) "
        "GROUP BY r.engine_id, sv.name",
        grain,
        since,
        now,
        list(retired),
        SAMPLE_MS,
    )
    # Rates per minute and per second, weighted by milliseconds.
    return {
        row["server_id"]: {
            "name": row["name"],
            **_served(
                (row["reqs"] or 0) / 60_000,
                (row["cached_tps"] or 0) / 1000,
                (row["prefill_tps"] or 0) / 1000,
                (row["decode_tps"] or 0) / 1000,
            ),
        }
        for row in rows
    }


def _total(rows: Iterable[dict[str, Any]]) -> dict[str, float | int]:
    total = dict(_ZERO_SERVED)
    for row in rows:
        for key in total:
            total[key] += row[key]
    return total


async def load_served(conn: Any, *, retired: Sequence[str] = (), now: int) -> dict[str, Any]:
    """Requests and tokens served in the last hour (from samples) and day (from rollups).

    The day is summed from the 20-minute rollups, or from the samples while
    there are none yet.
    """
    hour = await _served_from_samples(conn, now - 3_600_000, retired)
    day_since = now - 24 * 3_600_000
    day = await _served_from_rollups(conn, GRAIN_20M, day_since, now, retired)
    if not day:
        day = await _served_from_samples(conn, day_since, retired)
    servers = []
    for engine_id in dict.fromkeys([*hour, *day]):
        in_hour, in_day = hour.get(engine_id), day.get(engine_id)
        name = (in_hour or {}).get("name") or (in_day or {}).get("name")
        servers.append(
            {
                "id": engine_id,
                "name": name,
                "hour": {k: in_hour[k] for k in _ZERO_SERVED} if in_hour else dict(_ZERO_SERVED),
                "day": {k: in_day[k] for k in _ZERO_SERVED} if in_day else dict(_ZERO_SERVED),
            }
        )
    return {
        "hour": _total(server["hour"] for server in servers),
        "day": _total(server["day"] for server in servers),
        "servers": servers,
    }


_GPU_HISTORY_FROM_SAMPLE = (
    "s.utilization_pct",
    "CASE WHEN s.memory_total_mb > 0 THEN 100.0 * s.memory_used_mb / s.memory_total_mb END "
    "AS memory_pct",
    *(f"s.{name}" for name in GPU_ROLLUP_FIELDS[2:]),
)
_ENGINE_HISTORY_FROM_SAMPLE = tuple(f"s.{source} AS {name}" for name, source in ENGINE_HISTORY)


def _history_names(names: Iterable[str]) -> str:
    return _columns(names)


async def _history_from_samples(
    conn: Any, since: int, node: str, retired: Sequence[str], bucket_ms: int = GRAIN_1M
) -> dict[str, list[dict[str, Any]]]:
    """One point per GPU, engine and host and *bucket_ms* bucket: its newest sample."""
    lower = since // SAMPLE_MS
    gpu = await conn.fetch(
        "SELECT node_id, gpu_index, gpu_id, ts, "
        f"{_history_names(GPU_ROLLUP_FIELDS)} FROM ("
        "SELECT DISTINCT ON (s.gpu_id, s.ts / $3) g.node_id, g.gpu_index, s.gpu_id, s.ts, "
        f"{_columns(_GPU_HISTORY_FROM_SAMPLE)} "
        "FROM openinfra_gpus g "
        "JOIN openinfra_gpu_samples s ON s.gpu_id = g.id AND s.bucket >= $1 AND s.ts >= $2 "
        "WHERE ($4 = '' OR g.node_id = $4) "
        "ORDER BY s.gpu_id, s.ts / $3, s.ts DESC"
        ") h ORDER BY node_id, gpu_index, ts",
        lower,
        since,
        bucket_ms,
        node,
    )
    engine = await conn.fetch(
        "SELECT node_id, server_id, name, ts, "
        f"{_history_names(name for name, _ in ENGINE_HISTORY)} FROM ("
        "SELECT DISTINCT ON (s.engine_id, s.ts / $3) "
        "sv.node_id, s.engine_id AS server_id, sv.name, s.ts, "
        f"{_columns(_ENGINE_HISTORY_FROM_SAMPLE)} "
        "FROM openinfra_engines sv "
        "JOIN openinfra_engine_samples s ON s.engine_id = sv.id AND s.bucket >= $1 AND s.ts >= $2 "
        "WHERE ($4 = '' OR sv.node_id = $4) AND NOT (sv.id = ANY($5::text[])) "
        "ORDER BY s.engine_id, s.ts / $3, s.ts DESC"
        ") h ORDER BY name NULLS FIRST, ts",
        lower,
        since,
        bucket_ms,
        node,
        list(retired),
    )
    cpu = await conn.fetch(
        f"SELECT node_id, ts, {_history_names(CPU_HISTORY)} FROM ("
        "SELECT DISTINCT ON (s.node_id, s.ts / $3) s.node_id, s.ts, "
        f"{_columns(f's.{name}' for name in CPU_HISTORY)} "
        "FROM openinfra_nodes n "
        "JOIN openinfra_cpu_samples s ON s.node_id = n.id AND s.bucket >= $1 AND s.ts >= $2 "
        "WHERE ($4 = '' OR n.id = $4) "
        "ORDER BY s.node_id, s.ts / $3, s.ts DESC"
        ") h ORDER BY node_id, ts",
        lower,
        since,
        bucket_ms,
        node,
    )
    return {
        "gpu": [dict(row) for row in gpu],
        "sglang": [dict(row) for row in engine],
        "cpu": [dict(row) for row in cpu],
    }


async def _history_from_rollups(
    conn: Any, grain: int, since: int, node: str, retired: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """One point per GPU, engine and host and *grain* bucket, from the rollups."""
    gpu = await conn.fetch(
        "SELECT g.node_id, g.gpu_index, r.gpu_id, r.bucket_ts AS ts, "
        f"{_columns(f'r.{name}' for name in GPU_ROLLUP_FIELDS)} "
        "FROM openinfra_gpu_rollups r JOIN openinfra_gpus g ON g.id = r.gpu_id "
        "WHERE r.grain = $1 AND r.bucket_ts >= $2 AND ($3 = '' OR g.node_id = $3) "
        "ORDER BY g.node_id, g.gpu_index, r.bucket_ts",
        grain,
        since,
        node,
    )
    engine = await conn.fetch(
        "SELECT sv.node_id, r.engine_id AS server_id, sv.name, r.bucket_ts AS ts, "
        f"{_columns(f'r.{name}' for name, _ in ENGINE_HISTORY)} "
        "FROM openinfra_engine_rollups r JOIN openinfra_engines sv ON sv.id = r.engine_id "
        "WHERE r.grain = $1 AND r.bucket_ts >= $2 AND ($3 = '' OR sv.node_id = $3) "
        "AND NOT (sv.id = ANY($4::text[])) "
        "ORDER BY sv.name NULLS FIRST, r.bucket_ts",
        grain,
        since,
        node,
        list(retired),
    )
    cpu = await conn.fetch(
        f"SELECT node_id, bucket_ts AS ts, {_columns(CPU_HISTORY)} "
        "FROM openinfra_cpu_rollups "
        "WHERE grain = $1 AND bucket_ts >= $2 AND ($3 = '' OR node_id = $3) "
        "ORDER BY node_id, bucket_ts",
        grain,
        since,
        node,
    )
    return {
        "gpu": [dict(row) for row in gpu],
        "sglang": [dict(row) for row in engine],
        "cpu": [dict(row) for row in cpu],
    }


def history_grain(hours: float) -> int:
    """The grain a window is drawn at: a minute up to 2 hours, 20 minutes up to a day, then hours."""
    if hours <= 2:
        return GRAIN_1M
    return GRAIN_20M if hours <= 24 else GRAIN_1H


async def load_history(
    conn: Any, hours: float, *, node: str = "", retired: Sequence[str] = (), now: int
) -> dict[str, Any]:
    """The time series of the last *hours*, with ``grain_ms``, the spacing of their points.

    A window longer than two hours is drawn from the rollups; while they hold
    nothing for it, from the samples at a one-minute grain instead.
    """
    since = now - int(hours * 3_600_000)
    grain = history_grain(hours)
    if grain == GRAIN_1M:
        rows = await _history_from_samples(conn, since, node, retired)
    else:
        rows = await _history_from_rollups(conn, grain, since, node, retired)
        if not any(rows.values()):
            grain = GRAIN_1M
            rows = await _history_from_samples(conn, since, node, retired)
    return {"grain_ms": grain, **rows}


# --- rollups -----------------------------------------------------------------

_GPU_ROLLUP_VALUES = (
    "s.utilization_pct",
    "CASE WHEN s.memory_total_mb > 0 THEN 100.0 * s.memory_used_mb / s.memory_total_mb END",
    *(f"s.{name}" for name in GPU_ROLLUP_FIELDS[2:]),
)
_ENGINE_ROLLUP_COLUMNS = (*(name for name, _ in ENGINE_HISTORY), *ENGINE_SUMS)

#: A grain's buckets in [$2, $3) (sample buckets), from their samples: each
#: bucket's last sample and its count, for engines also the four rate sums.
_FROM_SAMPLES: dict[str, str] = {
    "gpu": (
        "INSERT INTO openinfra_gpu_rollups "
        f"(grain, bucket_ts, gpu_id, samples, {_columns(GPU_ROLLUP_FIELDS)}) "
        f"SELECT $1, t.bucket_ts, s.gpu_id, t.samples, {_columns(_GPU_ROLLUP_VALUES)} FROM ("
        "SELECT x.gpu_id, x.ts / $1 * $1 AS bucket_ts, COUNT(*)::int AS samples, "
        "MAX(x.bucket) AS last_bucket "
        "FROM openinfra_gpus g "
        "JOIN openinfra_gpu_samples x ON x.gpu_id = g.id AND x.bucket >= $2 AND x.bucket < $3 "
        "GROUP BY x.gpu_id, x.ts / $1"
        ") t JOIN openinfra_gpu_samples s ON s.gpu_id = t.gpu_id AND s.bucket = t.last_bucket "
        "ON CONFLICT (grain, bucket_ts, gpu_id) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded(GPU_ROLLUP_FIELDS)
    ),
    "engine": (
        "INSERT INTO openinfra_engine_rollups "
        f"(grain, bucket_ts, engine_id, samples, {_columns(_ENGINE_ROLLUP_COLUMNS)}, "
        "first_ts, last_ts) "
        "SELECT $1, t.bucket_ts, s.engine_id, t.samples, "
        f"{_columns(f's.{source}' for _, source in ENGINE_HISTORY)}, "
        f"{_columns(f't.{name}' for name in ENGINE_SUMS)}, t.first_ts, t.last_ts FROM ("
        "SELECT x.engine_id, x.ts / $1 * $1 AS bucket_ts, COUNT(*)::int AS samples, "
        "MAX(x.bucket) AS last_bucket, MIN(x.ts) AS first_ts, MAX(x.ts) AS last_ts, "
        "SUM(COALESCE(x.req_per_min, 0)) AS reqs_sum, "
        "SUM(COALESCE(x.cached_prefill_tps, 0)) AS cached_tps_sum, "
        f"SUM({_uncached_prefill('x')}) AS prefill_tps_sum, "
        "SUM(COALESCE(x.gen_throughput, 0)) AS decode_tps_sum "
        "FROM openinfra_engines sv "
        "JOIN openinfra_engine_samples x "
        "ON x.engine_id = sv.id AND x.bucket >= $2 AND x.bucket < $3 "
        "GROUP BY x.engine_id, x.ts / $1"
        ") t JOIN openinfra_engine_samples s "
        "ON s.engine_id = t.engine_id AND s.bucket = t.last_bucket "
        "ON CONFLICT (grain, bucket_ts, engine_id) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded((*_ENGINE_ROLLUP_COLUMNS, "first_ts", "last_ts"))
    ),
    "cpu": (
        "INSERT INTO openinfra_cpu_rollups "
        f"(grain, bucket_ts, node_id, samples, {_columns(CPU_HISTORY)}) "
        "SELECT $1, t.bucket_ts, s.node_id, t.samples, "
        f"{_columns(f's.{name}' for name in CPU_HISTORY)} FROM ("
        "SELECT x.node_id, x.ts / $1 * $1 AS bucket_ts, COUNT(*)::int AS samples, "
        "MAX(x.bucket) AS last_bucket "
        "FROM openinfra_nodes n "
        "JOIN openinfra_cpu_samples x ON x.node_id = n.id AND x.bucket >= $2 AND x.bucket < $3 "
        "GROUP BY x.node_id, x.ts / $1"
        ") t JOIN openinfra_cpu_samples s ON s.node_id = t.node_id AND s.bucket = t.last_bucket "
        "ON CONFLICT (grain, bucket_ts, node_id) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded(CPU_HISTORY)
    ),
}


def _hourly(
    table: str,
    key: str,
    values: tuple[str, ...],
    sums: tuple[str, ...] = (),
    *,
    spans: bool = False,
) -> str:
    """Hours from bucket $3 on, from their 20-minute rollups ($2): the last one's values.

    The samples are the 20-minute rollups' sum, as are the rate sums; with
    *spans*, the hour's span runs from its first rollup's first sample to its
    last rollup's last.
    """
    aggregated = (*sums, *(("first_ts", "last_ts") if spans else ()))
    aggregate = {"first_ts": "MIN", "last_ts": "MAX"}
    extra = "".join(f", {aggregate.get(name, 'SUM')}({name}) AS {name}" for name in aggregated)
    return (
        f"INSERT INTO {table} (grain, bucket_ts, {key}, samples, "
        f"{_columns((*values, *aggregated))}) "
        f"SELECT $1, t.hour_ts, r.{key}, t.samples, "
        f"{_columns((*(f'r.{name}' for name in values), *(f't.{name}' for name in aggregated)))} "
        "FROM ("
        f"SELECT {key}, bucket_ts / $1 * $1 AS hour_ts, MAX(bucket_ts) AS last_bucket_ts, "
        f"SUM(samples)::int AS samples{extra} "
        f"FROM {table} WHERE grain = $2 AND bucket_ts >= $3 "
        f"GROUP BY {key}, bucket_ts / $1"
        f") t JOIN {table} r "
        f"ON r.grain = $2 AND r.bucket_ts = t.last_bucket_ts AND r.{key} = t.{key} "
        f"ON CONFLICT (grain, bucket_ts, {key}) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded((*values, *aggregated))
    )


_HOURLY_FROM_20M: dict[str, str] = {
    "gpu": _hourly("openinfra_gpu_rollups", "gpu_id", GPU_ROLLUP_FIELDS),
    "engine": _hourly(
        "openinfra_engine_rollups",
        "engine_id",
        tuple(name for name, _ in ENGINE_HISTORY),
        ENGINE_SUMS,
        spans=True,
    ),
    "cpu": _hourly("openinfra_cpu_rollups", "node_id", CPU_HISTORY),
}

_ROLLUP_TABLES = {
    "gpu": "openinfra_gpu_rollups",
    "engine": "openinfra_engine_rollups",
    "cpu": "openinfra_cpu_rollups",
}


def rollup_floor(grain: int, now: int) -> int:
    """The oldest bucket a refresh computes for *grain*: its backfill horizon."""
    hours = ROLLUP_20M_BACKFILL_HOURS if grain == GRAIN_20M else ROLLUP_1H_BACKFILL_HOURS
    return bucket_start(now - hours * 3_600_000, grain)


async def _rollup_since(conn: Any, kind: str, grain: int, now: int) -> int:
    """The first bucket a refresh recomputes.

    Every bucket before the newest one with a row was refreshed after it ended,
    so only that one and those after it can still change. The one before the
    current bucket is always included, for a flush that committed late.
    """
    floor = rollup_floor(grain, now)
    newest = await conn.fetchval(
        f"SELECT MAX(bucket_ts) FROM {_ROLLUP_TABLES[kind]} WHERE grain = $1", grain
    )
    current = bucket_start(now, grain)
    if newest is None:
        return floor
    return max(floor, min(newest, current - grain))


async def refresh_rollups(conn: Any, now: int) -> None:
    """Bring the 20-minute and then the hourly rollups up to *now*."""
    for grain in (GRAIN_20M, GRAIN_1H):
        for kind in ("gpu", "engine", "cpu"):
            since = await _rollup_since(conn, kind, grain, now)
            # Past the 20-minute backfill the 20-minute rollups can have a gap
            # (the job was down longer than that), so those hours come from the
            # samples.
            if grain == GRAIN_1H and since >= rollup_floor(GRAIN_20M, now):
                await conn.execute(_HOURLY_FROM_20M[kind], GRAIN_1H, GRAIN_20M, since)
                continue
            end = bucket_start(now, grain) + grain
            await conn.execute(_FROM_SAMPLES[kind], grain, since // SAMPLE_MS, end // SAMPLE_MS)
