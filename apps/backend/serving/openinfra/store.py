"""OpenInfra's SQL: what a snapshot writes, what the routes read, and the rollups.

Every time is milliseconds since the Unix epoch. The schema is revision
``0026_openinfra``'s; its docstring describes the tables.

**Samples.** Each host, GPU and engine keeps at most one sample per second
(:data:`SAMPLE_MS`): the bucket is part of the primary key and a second
sample in it is dropped, so two flushes racing each other cannot both land.
A sample is stored at the time the reporter took it (the snapshot's ``ts``)
while the reporter's clock is within :data:`MAX_CLOCK_SKEW_MS` of the
gateway's, so network delay cannot move two consecutive snapshots into the
same second; otherwise at the gateway's time. A history point longer than a
second is the mean of the samples in it, and the served totals integrate the
samples' rates over their spacing.

**Status.** A snapshot stores ``online`` or ``degraded`` (a GPU at 85 °C or
with ECC errors; an engine with more than 2000 queued requests or its SMs at
95 %). Whatever is silent for :data:`STALE_MS` is read as ``offline``, at the
moment it is read; nothing has to mark it.

**Rollups.** :func:`refresh_rollups` recomputes, for each grain, the buckets
from the newest one that has a row (and always the one before it, for a late
flush) up to the current one: a 20-minute bucket from its samples, an hour
from its three 20-minute rollups. A grain with no rollups yet, or that has
fallen behind, starts from its backfill horizon instead, and an hour older
than the 20-minute horizon is computed from its samples. A rollup keeps the
means of its bucket's samples, the number of samples, and for engines the
sums of the request, cached-prefill, uncached-prefill and decode rates. A
bucket whose samples have been deleted (:func:`serving.openinfra.archive.prune`)
is never recomputed, so its rollup stays as it was.

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
SAMPLE_MS = 1_000
#: A snapshot's own time is used for its samples while it is this close to
#: the gateway's clock; otherwise the gateway's time is.
MAX_CLOCK_SKEW_MS = 60_000
#: A request event older than this against its snapshot's time, both by the
#: reporter's clock, is dropped: it is not recent, and its week may already be
#: archived. One newer than :data:`MAX_CLOCK_SKEW_MS` past it is dropped too.
MAX_REQUEST_AGE_MS = 15 * 60_000
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

_ENGINE_IDENTITY = (
    "node_id",
    "name",
    "model_name",
    "endpoint",
    "engine",
    "gpu_indexes",
    "server_id",
)
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
#: A request event a reporter sends again (a retry after a lost answer, or
#: overlapping log windows) meets the table's unique identity and is skipped.
_INSERT_REQUEST = (
    f"INSERT INTO openinfra_request_events (engine_id, ts, {_columns(REQUEST_FIELDS)}) "
    f"VALUES ($1, $2, {_placeholders(len(REQUEST_FIELDS), 3)}) "
    "ON CONFLICT DO NOTHING"
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

    Raw Prometheus text can expose several models, and parsed ``metrics`` add
    one more entry. Entries of the same model merge, a parsed value winning
    over one read from the text. A server that reports more than one model
    gets a row per model, ``<server id>:<model>``; otherwise its row is the
    server id.

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
    by_model: dict[str | None, dict[str, Any]] = {}
    for metrics in listed:
        model = metrics.get("model_name") or engine.server.model_name
        seen = by_model.get(model)
        if seen is None:
            by_model[model] = dict(metrics)
        else:
            seen.update({key: value for key, value in metrics.items() if value is not None})
    split = len(by_model) > 1
    rows = []
    for model, metrics in by_model.items():
        row_id = f"{engine.server.id}:{model}" if split and model else engine.server.id
        rows.append((engine.server, row_id, {**metrics, "model_name": model}))
    return rows


def sample_time(sent: int | None, now: int) -> int:
    """When a snapshot's samples are stored: when it was taken, unless the reporter's clock is off.

    *sent* is the snapshot's ``ts``, by the reporter's clock, and *now* the
    gateway's; both in milliseconds.
    """
    if sent is not None and abs(sent - now) <= MAX_CLOCK_SKEW_MS:
        return sent
    return now


def _request_time(ts: int | None, taken: int, shift: int) -> int | None:
    """Where a request event is stored, or ``None`` when it is out of bounds.

    *taken* is when its snapshot was taken and *ts* the event's own time, both
    by the reporter's clock, which is what bounds it; an event without a time
    takes the snapshot's. *shift* moves the result onto the time the
    snapshot's samples are stored at: 0 unless the reporter's clock is off.
    """
    if ts is None:
        return taken + shift
    if ts < taken - MAX_REQUEST_AGE_MS or ts > taken + MAX_CLOCK_SKEW_MS:
        return None
    return ts + shift


class SnapshotWrite:
    """A parsed snapshot as the statements that store it, built before anything is written.

    Building it is where a malformed engine is refused, so a refused body
    writes nothing.
    """

    def __init__(self, snapshot: Snapshot, now: int) -> None:
        """Lay out *snapshot*'s rows as of *now* (milliseconds, the gateway's clock).

        Live rows are seen at *now*; samples are stored at
        :func:`sample_time`. Request events are bounded by the reporter's
        clock, which they share with the snapshot's ``ts``, and moved with
        the samples when that clock is off; :attr:`requests_dropped` counts
        those out of bounds.

        Raises:
            IngestError: An engine sent neither metrics nor Prometheus text.
        """
        at = sample_time(snapshot.ts, now)
        bucket = at // SAMPLE_MS
        taken = snapshot.ts if snapshot.ts is not None else at
        shift = at - taken
        self.node: tuple[Any, ...] | None = None
        self.cpu_sample: tuple[Any, ...] | None = None
        self.gpus: list[tuple[Any, ...]] = []
        self.gpu_samples: list[tuple[Any, ...]] = []
        self.engines: list[tuple[Any, ...]] = []
        self.engine_samples: list[tuple[Any, ...]] = []
        self.requests: list[tuple[Any, ...]] = []
        self.requests_dropped = 0

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
                    at,
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
                    (gid, bucket, at, *(gpu.numbers.get(name) for name in GPU_SAMPLE_FIELDS))
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
                        server.id,
                        _engine_status(metrics),
                        *(metrics.get(name) for name in ENGINE_METRICS),
                        now,
                    )
                )
                self.engine_samples.append(
                    (row_id, bucket, at, *(metrics.get(name) for name in ENGINE_METRICS))
                )
            model = engine.server.model_name or (engine.metrics or {}).get("model_name")
            for event in engine.requests:
                ts = _request_time(event["ts"], taken, shift)
                if ts is None:
                    self.requests_dropped += 1
                    continue
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


async def load_recent_requests(
    conn: Any, engines: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Each engine's newest :data:`REQUESTS_PER_ENGINE` request events, engine by engine.

    *engines* are (row id, server id) pairs. An event carries no model, so a
    server split into a row per model has its events, stored under the server
    id, listed under each of its rows; ``server_id`` in the result is the row.
    """
    if not engines:
        return []
    rows = await conn.fetch(
        f"SELECT ids.row_id AS server_id, r.ts, {_columns(f'r.{n}' for n in REQUEST_FIELDS)} "
        "FROM unnest($1::text[], $2::text[]) WITH ORDINALITY AS ids(row_id, base_id, position) "
        "CROSS JOIN LATERAL ("
        "SELECT * FROM openinfra_request_events e "
        "WHERE e.engine_id = ids.row_id OR e.engine_id = ids.base_id "
        "ORDER BY e.ts DESC LIMIT $3"
        ") r ORDER BY ids.position, r.ts DESC",
        [row_id for row_id, _ in engines],
        [base_id for _, base_id in engines],
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


#: The ids a samples query reads, as an array for ``= ANY``: the sample
#: tables' primary key then serves each one as an index range, and a long
#: retention does not turn a window of minutes into a scan of the table. $3 is
#: a host id or ``''`` for all, $4 the retired engine ids.
_GPU_IDS = "ARRAY(SELECT id FROM openinfra_gpus WHERE $3 = '' OR node_id = $3)"
_ENGINE_IDS = (
    "ARRAY(SELECT id FROM openinfra_engines "
    "WHERE ($3 = '' OR node_id = $3) AND NOT (id = ANY($4::text[])))"
)
_NODE_IDS = "ARRAY(SELECT id FROM openinfra_nodes WHERE $3 = '' OR id = $3)"


def _without_id(row: Any) -> dict[str, Any]:
    """A history row without the join's ``id``."""
    item = dict(row)
    del item["id"]
    return item


async def _served_from_samples(
    conn: Any, since: int, retired: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Requests and tokens per engine since *since*: each sample's rates times their spacing."""
    rows = await conn.fetch(
        "SELECT sv.id AS server_id, sv.name, t.reqs, t.cached_tps, t.prefill_tps, t.decode_tps, "
        "t.n, t.t0, t.t1 FROM ("
        "SELECT s.engine_id, "
        "SUM(COALESCE(s.req_per_min, 0)) AS reqs, "
        "SUM(COALESCE(s.cached_prefill_tps, 0)) AS cached_tps, "
        f"SUM({_uncached_prefill('s')}) AS prefill_tps, "
        "SUM(COALESCE(s.gen_throughput, 0)) AS decode_tps, "
        "COUNT(*) AS n, MIN(s.ts) AS t0, MAX(s.ts) AS t1 "
        "FROM openinfra_engine_samples s "
        f"WHERE s.engine_id = ANY({_ENGINE_IDS}) AND s.bucket >= $1 AND s.ts >= $2 "
        "GROUP BY s.engine_id"
        ") t JOIN openinfra_engines sv ON sv.id = t.engine_id",
        since // SAMPLE_MS,
        since,
        "",
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


#: A GPU's history values as the means of a bucket's samples (alias ``s``).
_GPU_MEANS = (
    "AVG(s.utilization_pct)",
    "AVG(CASE WHEN s.memory_total_mb > 0 THEN 100.0 * s.memory_used_mb / s.memory_total_mb END)",
    *(f"AVG(s.{name})" for name in GPU_ROLLUP_FIELDS[2:]),
)
_ENGINE_MEANS = tuple(f"AVG(s.{source})" for _, source in ENGINE_HISTORY)
_CPU_MEANS = tuple(f"AVG(s.{name})" for name in CPU_HISTORY)


def _named(expressions: Iterable[str], names: Iterable[str]) -> str:
    return ", ".join(
        f"{expression} AS {name}" for expression, name in zip(expressions, names, strict=True)
    )


async def _history_from_samples(
    conn: Any, since: int, node: str, retired: Sequence[str], bucket_ms: int
) -> dict[str, list[dict[str, Any]]]:
    """One point per GPU, engine and host and *bucket_ms* bucket: the means of its samples.

    A point's ``ts`` is its bucket's start.
    """
    lower = since // SAMPLE_MS
    gpu = await conn.fetch(
        "SELECT g.node_id, g.gpu_index, g.id AS gpu_id, h.* FROM ("
        f"SELECT s.gpu_id AS id, s.ts / $4 * $4 AS ts, {_named(_GPU_MEANS, GPU_ROLLUP_FIELDS)} "
        "FROM openinfra_gpu_samples s "
        f"WHERE s.gpu_id = ANY({_GPU_IDS}) AND s.bucket >= $1 AND s.ts >= $2 "
        "GROUP BY s.gpu_id, s.ts / $4"
        ") h JOIN openinfra_gpus g ON g.id = h.id "
        "ORDER BY g.node_id, g.gpu_index, g.id, h.ts",
        lower,
        since,
        node,
        bucket_ms,
    )
    engine = await conn.fetch(
        "SELECT sv.node_id, sv.id AS server_id, sv.name, h.* FROM ("
        "SELECT s.engine_id AS id, s.ts / $5 * $5 AS ts, "
        f"{_named(_ENGINE_MEANS, (name for name, _ in ENGINE_HISTORY))} "
        "FROM openinfra_engine_samples s "
        f"WHERE s.engine_id = ANY({_ENGINE_IDS}) AND s.bucket >= $1 AND s.ts >= $2 "
        "GROUP BY s.engine_id, s.ts / $5"
        ") h JOIN openinfra_engines sv ON sv.id = h.id "
        "ORDER BY sv.name NULLS FIRST, sv.id, h.ts",
        lower,
        since,
        node,
        list(retired),
        bucket_ms,
    )
    cpu = await conn.fetch(
        "SELECT n.id AS node_id, h.* FROM ("
        f"SELECT s.node_id AS id, s.ts / $4 * $4 AS ts, {_named(_CPU_MEANS, CPU_HISTORY)} "
        "FROM openinfra_cpu_samples s "
        f"WHERE s.node_id = ANY({_NODE_IDS}) AND s.bucket >= $1 AND s.ts >= $2 "
        "GROUP BY s.node_id, s.ts / $4"
        ") h JOIN openinfra_nodes n ON n.id = h.id "
        "ORDER BY n.id, h.ts",
        lower,
        since,
        node,
        bucket_ms,
    )
    return {
        "gpu": [_without_id(row) for row in gpu],
        "sglang": [_without_id(row) for row in engine],
        "cpu": [_without_id(row) for row in cpu],
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


#: The spacing of a history window's points, by the longest window drawn at
#: it; longer windows are drawn an hour apart. Up to three hours they are
#: computed from the samples, beyond from the rollups.
HISTORY_GRAINS: tuple[tuple[float, int], ...] = (
    (0.25, 5_000),
    (0.5, 10_000),
    (1.0, 20_000),
    (3.0, GRAIN_1M),
    (24.0, GRAIN_20M),
)


def history_grain(hours: float) -> int:
    """The spacing of the points of a window of *hours* (:data:`HISTORY_GRAINS`)."""
    for longest, grain in HISTORY_GRAINS:
        if hours <= longest:
            return grain
    return GRAIN_1H


async def load_history(
    conn: Any, hours: float, *, node: str = "", retired: Sequence[str] = (), now: int
) -> dict[str, Any]:
    """The time series of the last *hours*, with ``grain_ms``, the spacing of their points.

    Each point is the mean of the samples in it. A window drawn 20 minutes
    apart or more comes from the rollups, or while they hold nothing for it,
    from the samples a minute apart.
    """
    since = now - int(hours * 3_600_000)
    grain = history_grain(hours)
    if grain < GRAIN_20M:
        rows = await _history_from_samples(conn, bucket_start(since, grain), node, retired, grain)
    else:
        rows = await _history_from_rollups(conn, grain, since, node, retired)
        if not any(rows.values()):
            grain = GRAIN_1M
            rows = await _history_from_samples(
                conn, bucket_start(since, grain), node, retired, grain
            )
    return {"grain_ms": grain, **rows}


# --- rollups -----------------------------------------------------------------

_ENGINE_ROLLUP_COLUMNS = (*(name for name, _ in ENGINE_HISTORY), *ENGINE_SUMS)
#: The four rate sums a rollup keeps for the served totals, in ENGINE_SUMS order.
_ENGINE_ROLLUP_SUMS = (
    "SUM(COALESCE(s.req_per_min, 0))",
    "SUM(COALESCE(s.cached_prefill_tps, 0))",
    f"SUM({_uncached_prefill('s')})",
    "SUM(COALESCE(s.gen_throughput, 0))",
)

#: A grain's buckets in [$2, $3) (sample buckets), from their samples: the
#: means of each bucket's samples and their count, for engines also the four
#: rate sums and the span the samples cover.
_FROM_SAMPLES: dict[str, str] = {
    "gpu": (
        "INSERT INTO openinfra_gpu_rollups "
        f"(grain, bucket_ts, gpu_id, samples, {_columns(GPU_ROLLUP_FIELDS)}) "
        f"SELECT $1, s.ts / $1 * $1, s.gpu_id, COUNT(*)::int, {_columns(_GPU_MEANS)} "
        "FROM openinfra_gpu_samples s "
        "WHERE s.gpu_id = ANY(ARRAY(SELECT id FROM openinfra_gpus)) "
        "AND s.bucket >= $2 AND s.bucket < $3 "
        "GROUP BY s.gpu_id, s.ts / $1 "
        "ON CONFLICT (grain, bucket_ts, gpu_id) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded(GPU_ROLLUP_FIELDS)
    ),
    "engine": (
        "INSERT INTO openinfra_engine_rollups "
        f"(grain, bucket_ts, engine_id, samples, {_columns(_ENGINE_ROLLUP_COLUMNS)}, "
        "first_ts, last_ts) "
        "SELECT $1, s.ts / $1 * $1, s.engine_id, COUNT(*)::int, "
        f"{_columns((*_ENGINE_MEANS, *_ENGINE_ROLLUP_SUMS))}, MIN(s.ts), MAX(s.ts) "
        "FROM openinfra_engine_samples s "
        "WHERE s.engine_id = ANY(ARRAY(SELECT id FROM openinfra_engines)) "
        "AND s.bucket >= $2 AND s.bucket < $3 "
        "GROUP BY s.engine_id, s.ts / $1 "
        "ON CONFLICT (grain, bucket_ts, engine_id) DO UPDATE SET samples = EXCLUDED.samples, "
        + _excluded((*_ENGINE_ROLLUP_COLUMNS, "first_ts", "last_ts"))
    ),
    "cpu": (
        "INSERT INTO openinfra_cpu_rollups "
        f"(grain, bucket_ts, node_id, samples, {_columns(CPU_HISTORY)}) "
        f"SELECT $1, s.ts / $1 * $1, s.node_id, COUNT(*)::int, {_columns(_CPU_MEANS)} "
        "FROM openinfra_cpu_samples s "
        "WHERE s.node_id = ANY(ARRAY(SELECT id FROM openinfra_nodes)) "
        "AND s.bucket >= $2 AND s.bucket < $3 "
        "GROUP BY s.node_id, s.ts / $1 "
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
    """Hours from bucket $3 on, from their 20-minute rollups ($2): their means, weighted by samples.

    A value's mean counts only the rollups that have it. The samples are the
    20-minute rollups' sum, as are the rate sums; with *spans*, the hour's span
    runs from its first rollup's first sample to its last rollup's last.
    """
    aggregated = (*sums, *(("first_ts", "last_ts") if spans else ()))
    aggregate = {"first_ts": "MIN", "last_ts": "MAX"}
    means = (
        f"SUM({name} * samples) / NULLIF(SUM(samples) FILTER (WHERE {name} IS NOT NULL), 0)"
        for name in values
    )
    totals = (f"{aggregate.get(name, 'SUM')}({name})" for name in aggregated)
    return (
        f"INSERT INTO {table} (grain, bucket_ts, {key}, samples, "
        f"{_columns((*values, *aggregated))}) "
        f"SELECT $1, bucket_ts / $1 * $1, {key}, SUM(samples)::int, "
        f"{_columns((*means, *totals))} "
        f"FROM {table} WHERE grain = $2 AND bucket_ts >= $3 "
        f"GROUP BY {key}, bucket_ts / $1 "
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


#: Every per-second bucket of a time since 2001 is at least this, and every
#: 6-second bucket of a time before 2160 below it.
_PER_SECOND_FLOOR = 1_000_000_000
_SAMPLE_TABLES = (
    ("openinfra_cpu_samples", "node_id", "openinfra_nodes"),
    ("openinfra_gpu_samples", "gpu_id", "openinfra_gpus"),
    ("openinfra_engine_samples", "engine_id", "openinfra_engines"),
)


def _count(status: str) -> int:
    """The row count of a ``DELETE n`` or ``UPDATE n`` status."""
    return int(status.split()[-1])


async def convert_six_second_buckets(conn: Any) -> int:
    """Give samples with a 6-second bucket their second's; how many there were.

    Revision ``0027_openinfra_archives`` converted those stored before it. The
    release before writes more if it runs again after it, as in a rollback;
    left as they are they would read as decades old. They all sit below every
    per-second bucket, so each key costs one index probe while there are none.
    A sample whose second already has one of its own gives way to it.
    """
    found = 0
    for table, key, identities in _SAMPLE_TABLES:
        keys = f"ARRAY(SELECT id FROM {identities})"
        found += _count(
            await conn.execute(
                f"DELETE FROM {table} o WHERE o.{key} = ANY({keys}) AND o.bucket < $1 "
                "AND o.bucket = o.ts / 6000 "
                f"AND EXISTS (SELECT 1 FROM {table} n WHERE n.{key} = o.{key} "
                "AND n.bucket = o.ts / 1000)",
                _PER_SECOND_FLOOR,
            )
        )
        found += _count(
            await conn.execute(
                f"UPDATE {table} SET bucket = ts / 1000 "
                f"WHERE {key} = ANY({keys}) AND bucket < $1 AND bucket = ts / 6000",
                _PER_SECOND_FLOOR,
            )
        )
    return found
