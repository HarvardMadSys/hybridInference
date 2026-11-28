"""The ingest body: one host's snapshot of itself, its GPUs and its engines.

A reporter posts one body per flush::

    {"ts": 1700000000000,
     "node": {"id": "node-a", "hostname": "node-a", "region": "...", "cluster": "...",
              "metadata": {...}, "cpu": {"cpu_util_pct": 12.5, ...}},
     "gpus": [{"index": 0, "name": "...", "utilization_pct": 91.2, ...}],
     "sglang": [{"server": {"id": "...", "name": "...", "engine": "vllm", ...},
                 "metrics": {"num_running_reqs": 3, ...},
                 "prometheus": "<text of the engine's /metrics>",
                 "requests": [{"ts": 1700000000000, "input_len": 812, ...}]}]}

``ts`` is when the reporter took the snapshot, by its own clock, and may be
left out. ``sglang`` holds every engine on the host, SGLang and vLLM alike
(``engine`` says which), and may also be a single object. An engine sends ``metrics``,
raw ``prometheus`` text, or both; one its proxy stopped for want of requests,
to start it again on the next, says ``"state": "idle"`` in ``server`` and needs
neither. Parsing is lenient, as the dashboard's
original Worker was, so a reporter of any age keeps working: a number may
arrive as a numeric string, an unknown field is ignored, and a field of the
wrong type is dropped rather than refused. Only the body's shape is refused
(:class:`IngestError`), with a message the reporter logs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from serving.openinfra.fields import CPU_FIELDS, ENGINE_METRICS, GPU_NUMBERS, REQUEST_COUNTS

#: GPUs one body may carry.
MAX_GPUS = 64
#: Engines one body may carry.
MAX_ENGINES = 16
#: Request events kept per engine and body; the rest are ignored.
MAX_REQUESTS = 20
#: Times below this are seconds, not milliseconds.
_MS_THRESHOLD = 1e12
#: Times from here on are refused: past what a double holds exactly, and far
#: past any real clock.
_MAX_TIME = 2**53
#: Largest GPU index accepted (an INTEGER column).
_MAX_GPU_INDEX = 2**31 - 1
#: Largest token count a request event stores (a BIGINT column).
_MAX_COUNT = 2**63 - 1


class IngestError(ValueError):
    """The body does not have the shape of a snapshot; the message says why."""


@dataclass(frozen=True)
class NodeIngest:
    """The reporting host."""

    id: str
    hostname: str | None = None
    region: str | None = None
    cluster: str | None = None
    #: Whatever the reporter put there; ``...`` when it sent no key at all.
    metadata: Any = ...
    cpu: dict[str, float | None] | None = None


@dataclass(frozen=True)
class GpuIngest:
    """One GPU of the host."""

    index: int
    uuid: str | None = None
    name: str | None = None
    driver: str | None = None
    pstate: str | None = None
    #: Whatever the reporter put there; ``...`` when it sent no key at all.
    processes: Any = ...
    numbers: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerIngest:
    """An engine's identity."""

    id: str
    node_id: str | None = None
    name: str | None = None
    model_name: str | None = None
    endpoint: str | None = None
    gpu_indexes: str | None = None
    engine: str | None = None
    #: ``"idle"`` while the proxy that starts the engine on demand has stopped
    #: it; None otherwise, whatever else the reporter sent.
    state: str | None = None


@dataclass(frozen=True)
class EngineIngest:
    """One engine of the host: its identity, metrics and recent requests."""

    server: ServerIngest
    metrics: dict[str, Any] | None = None
    prometheus: str | None = None
    requests: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """A parsed ingest body."""

    node: NodeIngest | None = None
    gpus: tuple[GpuIngest, ...] = ()
    engines: tuple[EngineIngest, ...] = ()
    #: When the reporter took it, by the reporter's clock; ``None`` if not sent.
    ts: int | None = None


def _given(value: Any) -> bool:
    """Whether a field counts as sent, as the Worker's JavaScript tested it.

    Only ``null``, ``false``, ``0``, ``NaN`` and ``""`` count as absent; an
    empty object or array is present, and then has to have the right shape.
    """
    if value is None or value is False:
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return value != ""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IngestError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngestError(f"{label} is required")
    return value.strip()


def optional_string(value: Any) -> str | None:
    """A non-blank string, trimmed; anything else is ``None``."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def number(value: Any) -> float | None:
    """A finite number, or a string holding one; anything else is ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _numbers(obj: dict[str, Any], names: tuple[str, ...]) -> dict[str, float | None]:
    return {name: number(obj.get(name)) for name in names}


def _parse_node(raw: Any) -> NodeIngest:
    obj = _object(raw, "node")
    cpu = _numbers(_object(obj["cpu"], "node.cpu"), CPU_FIELDS) if _given(obj.get("cpu")) else None
    return NodeIngest(
        id=_required_string(obj.get("id"), "node.id"),
        hostname=optional_string(obj.get("hostname")),
        region=optional_string(obj.get("region")),
        cluster=optional_string(obj.get("cluster")),
        metadata=obj.get("metadata", ...),
        cpu=cpu,
    )


def _parse_gpu(raw: Any) -> GpuIngest:
    obj = _object(raw, "gpu")
    index = number(obj.get("index"))
    if index is None:
        raise IngestError("gpu.index is required")
    if not index.is_integer() or not 0 <= index <= _MAX_GPU_INDEX:
        raise IngestError("gpu.index must be a non-negative integer")
    return GpuIngest(
        index=int(index),
        uuid=optional_string(obj.get("uuid")),
        name=optional_string(obj.get("name")),
        driver=optional_string(obj.get("driver")),
        pstate=optional_string(obj.get("pstate")),
        processes=obj.get("processes", ...),
        numbers=_numbers(obj, GPU_NUMBERS),
    )


def _parse_server(raw: Any) -> ServerIngest:
    obj = _object(raw, "sglang.server")
    return ServerIngest(
        id=_required_string(obj.get("id"), "sglang.server.id"),
        node_id=optional_string(obj.get("node_id")),
        name=optional_string(obj.get("name")),
        model_name=optional_string(obj.get("model_name")),
        endpoint=optional_string(obj.get("endpoint")),
        gpu_indexes=optional_string(obj.get("gpu_indexes")),
        engine=optional_string(obj.get("engine")),
        state="idle" if optional_string(obj.get("state")) == "idle" else None,
    )


def parse_metrics(raw: Any) -> dict[str, Any]:
    """An engine's ``metrics``: every known metric, ``None`` where absent, and its model."""
    obj = _object(raw, "sglang.metrics")
    metrics: dict[str, Any] = _numbers(obj, ENGINE_METRICS)
    metrics["model_name"] = optional_string(obj.get("model_name"))
    return metrics


def parse_time(raw: Any) -> int | None:
    """A time in milliseconds, or ``None`` when absent or unusable.

    A value below 10^12 is taken as seconds. Zero, negative and far-future
    values are unusable.
    """
    ts = number(raw)
    if ts is None or ts <= 0:
        return None
    if ts < _MS_THRESHOLD:
        ts *= 1000
    return int(ts) if ts < _MAX_TIME else None


def _parse_request(raw: Any) -> dict[str, Any]:
    obj = _object(raw, "sglang.requests[]")
    event: dict[str, Any] = {"ts": parse_time(obj.get("ts"))}
    for name in (
        "input_len",
        "output_len",
        "cache_len",
        "uncached_len",
        "ttft_s",
        "queue_s",
        "prefill_s",
        "tpot_s",
        "e2e_s",
        "decode_tps",
        "spec_accept_len",
        "spec_accept_rate",
        "batch_size",
    ):
        value = number(obj.get(name))
        if value is not None and name in REQUEST_COUNTS:
            count = _round_half_up(value)
            value = count if abs(count) <= _MAX_COUNT else None
        event[name] = value
    return event


def _parse_engine(raw: Any) -> EngineIngest:
    obj = _object(raw, "sglang")
    requests = obj.get("requests")
    return EngineIngest(
        server=_parse_server(obj.get("server")),
        metrics=parse_metrics(obj["metrics"]) if _given(obj.get("metrics")) else None,
        prometheus=optional_string(obj.get("prometheus")),
        requests=tuple(_parse_request(item) for item in requests[:MAX_REQUESTS])
        if isinstance(requests, list)
        else (),
    )


def parse_snapshot(body: Any) -> Snapshot:
    """Parse an ingest body.

    Raises:
        IngestError: The body is not an object, a part has the wrong shape, it
            carries nothing, or it carries GPUs without the host they belong to.
    """
    obj = _object(body, "body")
    node = _parse_node(obj["node"]) if _given(obj.get("node")) else None
    gpus: tuple[GpuIngest, ...] = ()
    if _given(obj.get("gpus")):
        if not isinstance(obj["gpus"], list):
            raise IngestError("gpus must be an array")
        if len(obj["gpus"]) > MAX_GPUS:
            raise IngestError(f"gpus exceeds {MAX_GPUS}")
        gpus = tuple(_parse_gpu(item) for item in obj["gpus"])
    engines: tuple[EngineIngest, ...] = ()
    if _given(obj.get("sglang")):
        # An array lets a reporter send every engine on the host in one request.
        listed = obj["sglang"] if isinstance(obj["sglang"], list) else [obj["sglang"]]
        if len(listed) > MAX_ENGINES:
            raise IngestError(f"sglang exceeds {MAX_ENGINES}")
        engines = tuple(_parse_engine(item) for item in listed)
    if node is None and not gpus and not engines:
        raise IngestError("body must include node, gpus, or sglang")
    if gpus and node is None:
        raise IngestError("node is required when ingesting gpus")
    return Snapshot(node=node, gpus=gpus, engines=engines, ts=parse_time(obj.get("ts")))
