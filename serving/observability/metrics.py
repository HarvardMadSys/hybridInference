"""No-op metrics shims.

Prometheus and the prometheus-client dependency were removed from this
project. The metric symbols below remain as zero-overhead no-ops so the
many existing callsites (``API_REQUESTS.labels(...).inc()``,
``ROUTEWISE_QUOTA_REMAINING.set(...)``, ``latency_timer(...)``, etc.) keep
working without a refactor.

If metrics are ever reintroduced, replace this module with a real
implementation; nothing else in the codebase needs to change.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


def _noop(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
    return None


# Counter / histogram shim: ``X.labels(...).inc()`` / ``X.labels(...).observe(...)``.
_LabeledNoOp = type(
    "Noop",
    (),
    {"labels": lambda *_a, **_k: type("L", (), {"inc": _noop, "observe": _noop})()},
)

# Gauge shim with both the unlabeled (``g.set(v)`` / ``g.inc()``/``g.dec()``)
# and labeled (``g.labels(...).set(v)``) interfaces used in the codebase.
_LabeledGaugeNoOp = type(
    "NoopGauge",
    (),
    {
        "labels": lambda *_a, **_k: type("L", (), {"set": _noop, "inc": _noop, "dec": _noop})(),
        "set": _noop,
        "inc": _noop,
        "dec": _noop,
    },
)


REGISTRY: Any = None

# Core API metrics
API_REQUESTS = _LabeledNoOp()
API_REQUEST_LATENCY = _LabeledNoOp()
API_TTFT = _LabeledNoOp()
PROVIDER_LATENCY = _LabeledNoOp()
API_RETRIES = _LabeledNoOp()
API_FALLBACKS = _LabeledNoOp()
API_TOKENS = _LabeledNoOp()
API_TOKEN_ANOMALIES = _LabeledNoOp()
STREAMING_INTERRUPTION = _LabeledNoOp()
RATE_LIMIT_HITS = _LabeledNoOp()
API_MODEL_REQUESTS = _LabeledNoOp()
API_CONCURRENCY = _LabeledGaugeNoOp()

# Provider metrics
PROVIDER_AVAILABILITY = _LabeledGaugeNoOp()

# Database metrics
DATABASE_CONNECTED = _LabeledGaugeNoOp()

# User statistics metrics
USERS_TOTAL = _LabeledGaugeNoOp()
USERS_ACTIVE_DAILY = _LabeledGaugeNoOp()
USERS_ACTIVE_MONTHLY = _LabeledGaugeNoOp()

# Rate limit queueing metrics
RATE_LIMIT_QUEUE_SIZE = _LabeledGaugeNoOp()
RATE_LIMIT_QUEUE_WAIT = _LabeledNoOp()

# Circuit breaker metrics
CIRCUIT_STATE = _LabeledGaugeNoOp()
CIRCUIT_OPEN_TOTAL = _LabeledNoOp()

# RouteWise routing metrics
ROUTING_STRATEGY_SELECTED = _LabeledNoOp()
ROUTEWISE_TIER_DECISIONS = _LabeledNoOp()
ROUTEWISE_QUOTA_REMAINING = _LabeledGaugeNoOp()
ROUTEWISE_SC_ACTIVE = _LabeledGaugeNoOp()
ROUTEWISE_HEDGE_DECISIONS = _LabeledNoOp()
ROUTEWISE_BACKUP_WINS = _LabeledNoOp()
ROUTEWISE_VALUE_ESTIMATE = _LabeledNoOp()
ROUTEWISE_LP_STATUS = _LabeledNoOp()
ROUTEWISE_CANARY_DECISIONS = _LabeledNoOp()


def render_latest() -> bytes:  # pragma: no cover
    """Return an empty body; metrics scraping is no longer supported."""
    return b"# metrics disabled\n"


@contextmanager
def latency_timer(_hist: Any, **_labels: str) -> Iterator[None]:  # pragma: no cover
    """No-op latency timer context manager."""
    yield


def status_class_from_code(code: int) -> str:
    """Return coarse HTTP status class label for a status code."""
    if 200 <= code < 300:
        return "2xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


# Precompiled patterns for route normalization (kept because callers use it
# for log labels, not just metrics).
_UUID_RE = re.compile(r"/[0-9a-fA-F-]{36}")
_HEX_RE = re.compile(r"/[0-9a-fA-F]{8,}")
_ID_RE = re.compile(r"/\d+")


def normalize_route(path: str) -> str:
    """Normalize dynamic path segments to avoid label cardinality explosion."""
    path = _UUID_RE.sub("/:uuid", path)
    path = _HEX_RE.sub("/:hex", path)
    path = _ID_RE.sub("/:id", path)
    return path


def _sanitize_label_value(value: str, max_len: int = 80) -> str:
    v = value.strip()
    v = re.sub(r"[^a-zA-Z0-9:_\-/\.]+", "_", v)
    if len(v) > max_len:
        return v[: max_len - 3] + "..."
    return v


def normalize_model_label(model: str) -> str:
    """Normalize model label value (kept for use in structured logs)."""
    import os

    mode = os.getenv("METRICS_MODEL_LABEL", "full")  # full|family
    m = model
    if mode == "family":
        m = re.sub(r"([@:-])(\d{4}-\d{2}-\d{2}|v\d+)$", r"\1", m)
        m = m.rstrip("-:@")
    return _sanitize_label_value(m)


def normalize_provider_label(provider: str) -> str:
    """Normalize provider label value (kept for use in structured logs)."""
    return _sanitize_label_value(provider)


__all__ = [
    # Core API metrics
    "API_CONCURRENCY",
    "API_FALLBACKS",
    "API_MODEL_REQUESTS",
    "API_REQUESTS",
    "API_REQUEST_LATENCY",
    "API_RETRIES",
    "API_TOKENS",
    "API_TOKEN_ANOMALIES",
    "API_TTFT",
    # Circuit breaker metrics
    "CIRCUIT_OPEN_TOTAL",
    "CIRCUIT_STATE",
    # Database metrics
    "DATABASE_CONNECTED",
    # Provider metrics
    "PROVIDER_AVAILABILITY",
    "PROVIDER_LATENCY",
    # Rate limiting metrics
    "RATE_LIMIT_HITS",
    "RATE_LIMIT_QUEUE_SIZE",
    "RATE_LIMIT_QUEUE_WAIT",
    # RouteWise metrics
    "ROUTEWISE_BACKUP_WINS",
    "ROUTEWISE_CANARY_DECISIONS",
    "ROUTEWISE_HEDGE_DECISIONS",
    "ROUTEWISE_LP_STATUS",
    "ROUTEWISE_QUOTA_REMAINING",
    "ROUTEWISE_SC_ACTIVE",
    "ROUTEWISE_TIER_DECISIONS",
    "ROUTEWISE_VALUE_ESTIMATE",
    # Routing strategy metrics
    "ROUTING_STRATEGY_SELECTED",
    # Streaming metrics
    "STREAMING_INTERRUPTION",
    "USERS_ACTIVE_DAILY",
    "USERS_ACTIVE_MONTHLY",
    # User statistics metrics
    "USERS_TOTAL",
    # Helper functions
    "latency_timer",
    "normalize_model_label",
    "normalize_provider_label",
    "normalize_route",
    "render_latest",
    "status_class_from_code",
]
