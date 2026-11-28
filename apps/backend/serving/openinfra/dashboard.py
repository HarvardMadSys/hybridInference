"""The documents the OpenInfra routes serve, built from :mod:`serving.openinfra.store`.

The shapes are those of the dashboard's original Worker, whose page the
console's ``/openinfra`` page replaces, so a reader of either knows the other:

- the dashboard (:func:`build_dashboard`): the fleet's state (``healthy``,
  ``busy``, ``degraded`` or ``offline``), fleet-wide figures in ``kpis``, every
  host, GPU and engine (``sglang``, which holds vLLM engines too), the requests
  and tokens served in the last hour and day, and recent requests;
- the history (:func:`build_history`): time series for GPUs, engines and
  hosts, one point per ``grain_ms``;
- the export (:func:`build_export`): identities, served totals and the
  history of the last days, for an administrator to download.

Engines come in the deployment's tab order (``OPENINFRA_ENGINE_ORDER``, by
name), the unlisted ones after, alphabetically; retired engines are left out
everywhere.
"""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any

from serving.openinfra import store

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: The widest history window, in hours (7 days).
MAX_HISTORY_HOURS = 24 * 7
#: The narrowest history window, in hours.
MIN_HISTORY_HOURS = 0.25
#: The history window when none is asked for.
DEFAULT_HISTORY_HOURS = 2.0
#: The longest export, in days.
MAX_EXPORT_DAYS = 30

#: Queued requests past which the fleet is degraded, and busy.
_DEGRADED_QUEUE = 1500
_BUSY_QUEUE = 200
#: Mean GPU utilization past which the fleet is busy.
_BUSY_UTIL_PCT = 80


def now_ms() -> int:
    """The current time in milliseconds since the epoch."""
    return int(time.time() * 1000)


def _finite(values: Iterable[Any]) -> list[float]:
    return [
        float(v)
        for v in values
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    ]


def _avg(values: Iterable[Any]) -> float | None:
    numbers = _finite(values)
    return sum(numbers) / len(numbers) if numbers else None


def _sum(values: Iterable[Any]) -> float:
    return sum(_finite(values))


def _parsed(raw: Any) -> Any:
    """A stored free-form JSON value, or ``[]`` when it is absent or unreadable."""
    if raw is None:
        return []
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return []


def order_engines(
    engines: Sequence[dict[str, Any]], order: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """*engines* in tab order: those *order* names first, as listed, then the rest by name."""
    position = {name: i for i, name in enumerate(order)}

    def key(engine: dict[str, Any]) -> tuple[int, str, str]:
        label = engine.get("name") or engine["id"]
        rank = position.get(label, position.get(engine["id"], len(position)))
        return rank, str(label).casefold(), engine["id"]

    return sorted(engines, key=key)


def _gpu(gpu: dict[str, Any], node: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": gpu["id"],
        "node_id": gpu["node_id"],
        "hostname": node["hostname"] if node else gpu["node_id"],
        "region": node["region"] if node else None,
        "cluster": node["cluster"] if node else None,
        "gpu_index": gpu["gpu_index"],
        "uuid": gpu["uuid"],
        "name": gpu["name"],
        "driver": gpu["driver"],
        "memory_total_mb": gpu["memory_total_mb"],
        "utilization_pct": gpu["utilization_pct"],
        "memory_util_pct": gpu["memory_util_pct"],
        "memory_used_mb": gpu["memory_used_mb"],
        "temperature_c": gpu["temperature_c"],
        "power_w": gpu["power_w"],
        "power_limit_w": gpu["power_limit_w"],
        "fan_speed_pct": gpu["fan_speed_pct"],
        "clocks_sm_mhz": gpu["clocks_sm_mhz"],
        "clocks_mem_mhz": gpu["clocks_mem_mhz"],
        "ecc_errors": gpu["ecc_errors"],
        "pstate": gpu["pstate"],
        "processes": _parsed(gpu["processes"]),
        "status": gpu["status"],
        "last_seen_at": gpu["last_seen_at"],
        "tensor_active": gpu["tensor_active"],
        "dram_active": gpu["dram_active"],
        "pcie_tx_bps": gpu["pcie_tx_bps"],
        "pcie_rx_bps": gpu["pcie_rx_bps"],
        "nvlink_tx_bps": gpu["nvlink_tx_bps"],
        "nvlink_rx_bps": gpu["nvlink_rx_bps"],
    }


_ENGINE_OUT = (
    "id",
    "node_id",
    "hostname",
    "region",
    "name",
    "model_name",
    "endpoint",
    "engine",
    "status",
    "num_running_reqs",
    "num_queue_reqs",
    "num_used_tokens",
    "token_usage",
    "gen_throughput",
    "cache_hit_rate",
    "prompt_tokens_total",
    "generation_tokens_total",
    "ttft_avg_s",
    "queue_avg_s",
    "prefill_avg_s",
    "e2e_latency_avg_s",
    "tpot_avg_s",
    "gpu_indexes",
    "sm_util_pct",
    "max_total_num_tokens",
    "num_active_tokens",
    "num_waiting_uncached_tokens",
    "prefill_tps",
    "spec_accept_length",
    "sglang_util_pct",
    "max_running_requests",
    "weight_gb",
    "kv_cache_gb",
    "spec_accept_rate",
    "req_per_min",
    "cached_prefill_tps",
    "chunked_prefill_avg_s",
    "hicache_host_used",
    "hicache_host_total",
    "device_cached_tps",
    "host_cached_tps",
    "loadback_tps",
    "evict_tps",
    "backup_tps",
    "prompt_tokens_avg",
    "uncached_prompt_tokens_avg",
    "generation_tokens_avg",
    "http_error_per_min",
    "retracts",
    "last_seen_at",
)


def _engine(engine: dict[str, Any], node: dict[str, Any] | None) -> dict[str, Any]:
    located = {
        **engine,
        "hostname": node["hostname"] if node else engine["node_id"],
        "region": node["region"] if node else None,
    }
    return {key: located.get(key) for key in _ENGINE_OUT}


def fleet_state(gpus: Sequence[dict[str, Any]], queued: float, mean_util: float | None) -> str:
    """``offline`` with no GPU reporting, else ``degraded``, ``busy`` or ``healthy``."""
    if not any(gpu["status"] != "offline" for gpu in gpus):
        return "offline"
    if any(gpu["status"] == "degraded" for gpu in gpus) or queued > _DEGRADED_QUEUE:
        return "degraded"
    if queued > _BUSY_QUEUE or (mean_util or 0) > _BUSY_UTIL_PCT:
        return "busy"
    return "healthy"


def _node(node: dict[str, Any], gpus: Sequence[dict[str, Any]]) -> dict[str, Any]:
    own = [gpu for gpu in gpus if gpu["node_id"] == node["id"]]
    return {
        **node,
        "metadata": _parsed(node["metadata"]),
        "gpu_count": len(own),
        "avg_utilization_pct": _avg(gpu["utilization_pct"] for gpu in own),
    }


async def build_dashboard(
    conn: Any,
    *,
    retired: Sequence[str] = (),
    order: Sequence[str] = (),
    now: int | None = None,
) -> dict[str, Any]:
    """The current state of every host, GPU and engine, with served totals and recent requests."""
    now = now_ms() if now is None else now
    nodes, gpus, engines = await store.load_snapshot(conn, retired=retired, now=now)
    engines = order_engines(engines, order)
    served = await store.load_served(conn, retired=retired, now=now)
    requests = await store.load_recent_requests(
        conn, [(engine["id"], engine.get("server_id") or engine["id"]) for engine in engines]
    )
    node_by_id = {node["id"]: node for node in nodes}

    online_gpus = [gpu for gpu in gpus if gpu["status"] != "offline"]
    online_engines = [engine for engine in engines if engine["status"] != "offline"]
    queued = _sum(engine["num_queue_reqs"] for engine in engines)
    mean_util = _avg(gpu["utilization_pct"] for gpu in online_gpus)
    return {
        "generated_at": now,
        "fleet": fleet_state(gpus, queued, mean_util),
        "kpis": {
            "nodes": len(nodes),
            "nodes_online": sum(1 for node in nodes if node["status"] == "online"),
            "gpus": len(gpus),
            "gpus_online": len(online_gpus),
            "avg_utilization_pct": mean_util,
            "tensor_active": _avg(gpu["tensor_active"] for gpu in online_gpus),
            "dram_active": _avg(gpu["dram_active"] for gpu in online_gpus),
            "nvlink_gbps": _sum(
                (gpu["nvlink_tx_bps"] or 0) + (gpu["nvlink_rx_bps"] or 0) for gpu in online_gpus
            )
            / 1e9,
            "memory_used_mb": _sum(gpu["memory_used_mb"] for gpu in gpus),
            "memory_total_mb": _sum(gpu["memory_total_mb"] for gpu in gpus),
            "avg_temperature_c": _avg(gpu["temperature_c"] for gpu in online_gpus),
            "power_w": _sum(gpu["power_w"] for gpu in online_gpus),
            "sglang_servers": len(engines),
            "sglang_online": len(online_engines),
            "running_reqs": _sum(engine["num_running_reqs"] for engine in engines),
            "queue_reqs": queued,
            "gen_throughput": _sum(engine["gen_throughput"] for engine in online_engines),
            "token_usage": _avg(engine["token_usage"] for engine in online_engines),
            "cache_hit_rate": _avg(engine["cache_hit_rate"] for engine in online_engines),
            "used_tokens": _sum(engine["num_used_tokens"] for engine in engines),
            "max_tokens": _sum(engine["max_total_num_tokens"] for engine in engines),
            "active_tokens": _sum(engine["num_active_tokens"] for engine in engines),
            "waiting_tokens": _sum(engine["num_waiting_uncached_tokens"] for engine in engines),
            "prefill_tps": _sum(engine["prefill_tps"] for engine in online_engines),
            "cpu_util_pct": _avg(node["cpu_util_pct"] for node in nodes),
            "load1": _avg(node["load1"] for node in nodes),
            "ram_used_mb": _sum(node["mem_used_mb"] for node in nodes),
            "ram_total_mb": _sum(node["mem_total_mb"] for node in nodes),
        },
        "nodes": [_node(node, gpus) for node in nodes],
        "gpus": [_gpu(gpu, node_by_id.get(gpu["node_id"])) for gpu in gpus],
        "sglang": [_engine(engine, node_by_id.get(engine["node_id"])) for engine in engines],
        "served": served,
        "requests": requests,
    }


def clamp_hours(hours: float) -> float:
    """A history window within [:data:`MIN_HISTORY_HOURS`, :data:`MAX_HISTORY_HOURS`]."""
    if not math.isfinite(hours):
        return DEFAULT_HISTORY_HOURS
    return min(MAX_HISTORY_HOURS, max(MIN_HISTORY_HOURS, hours))


async def build_history(
    conn: Any,
    hours: float,
    *,
    node: str = "",
    retired: Sequence[str] = (),
    now: int | None = None,
) -> dict[str, Any]:
    """The time series of the last *hours* (clamped), for every host or only *node*."""
    now = now_ms() if now is None else now
    history = await store.load_history(
        conn, clamp_hours(hours), node=node, retired=retired, now=now
    )
    return {"generated_at": now, **history}


async def build_export(
    conn: Any,
    days: float,
    *,
    retired: Sequence[str] = (),
    order: Sequence[str] = (),
    now: int | None = None,
) -> dict[str, Any]:
    """Identities, served totals and the history of the last *days* (1 to 30)."""
    now = now_ms() if now is None else now
    whole_days = min(MAX_EXPORT_DAYS, max(1, math.floor(days + 0.5) if math.isfinite(days) else 30))
    hours = whole_days * 24
    nodes, gpus, engines = await store.load_snapshot(conn, retired=retired, now=now)
    engines = order_engines(engines, order)
    served = await store.load_served(conn, retired=retired, now=now)
    history = await store.load_history(conn, hours, retired=retired, now=now)
    return {
        "exported_at": now,
        "days": whole_days,
        "hours": hours,
        "grain_ms": history["grain_ms"],
        # None: kept indefinitely, nothing is deleted.
        "retention": {"samples_hours": None, "rollup_20m_hours": None, "rollup_1h_hours": None},
        "nodes": [
            {
                key: node[key]
                for key in ("id", "hostname", "region", "cluster", "status", "last_seen_at")
            }
            for node in nodes
        ],
        "gpus": [
            {key: gpu[key] for key in ("id", "node_id", "gpu_index", "name", "status")}
            for gpu in gpus
        ],
        "servers": [
            {
                key: engine[key]
                for key in (
                    "id",
                    "node_id",
                    "name",
                    "model_name",
                    "engine",
                    "gpu_indexes",
                    "status",
                )
            }
            for engine in engines
        ],
        "served": served,
        "history": history,
    }
