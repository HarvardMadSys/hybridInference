"""Server-side Prometheus metrics with graceful no-op fallback.

This module centralizes metric definitions for the API server. It tries to
import ``prometheus_client``. If unavailable at runtime (e.g., during local
tests without the dependency), all helpers degrade to no-ops and ``/metrics``
can return a minimal payload.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
except Exception:  # pragma: no cover - fallback when lib missing
    CollectorRegistry = None  # type: ignore[assignment]
    Counter = None  # type: ignore[assignment]
    Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]


_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"


def _noop(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
    return None


if _ENABLED and CollectorRegistry and Counter and Histogram:
    REGISTRY = CollectorRegistry()

    API_REQUESTS = Counter(
        "api_requests_total",
        "Total API requests",
        labelnames=("route", "method", "status_code", "stream"),
        registry=REGISTRY,
    )

    API_REQUEST_LATENCY = Histogram(
        "api_request_duration_seconds",
        "Request duration in seconds",
        labelnames=("route", "method", "stream"),
        buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 10, 20),
        registry=REGISTRY,
    )

    API_TTFT = Histogram(
        "api_ttft_seconds",
        "Time to first token in seconds",
        labelnames=("provider", "model"),
        buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 10),
        registry=REGISTRY,
    )

    PROVIDER_LATENCY = Histogram(
        "provider_request_duration_seconds",
        "Upstream provider call duration in seconds",
        labelnames=("provider", "model", "operation"),
        buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 10, 20),
        registry=REGISTRY,
    )

    API_RETRIES = Counter(
        "api_retries_total",
        "Total upstream retries",
        labelnames=("provider", "reason"),
        registry=REGISTRY,
    )

    API_FALLBACKS = Counter(
        "api_fallbacks_total",
        "Total routing fallbacks between providers",
        labelnames=("from_provider", "to_provider", "reason"),
        registry=REGISTRY,
    )

    API_TOKENS = Counter(
        "api_tokens_total",
        "Total tokens by direction",
        labelnames=("model", "provider", "direction"),
        registry=REGISTRY,
    )

    API_TOKEN_COST_DOLLARS = Counter(
        "api_token_cost_dollars_total",
        "Accumulated token cost in USD (estimated)",
        labelnames=("model", "provider"),
        registry=REGISTRY,
    )

    MODEL_REFUSALS = Counter(
        "model_refusals_total",
        "Model refusal or safety filter events",
        labelnames=("model", "provider", "reason"),
        registry=REGISTRY,
    )

    MODEL_TRUNCATIONS = Counter(
        "model_truncations_total",
        "Model truncation events",
        labelnames=("model", "provider", "direction"),
        registry=REGISTRY,
    )

    STREAMING_INTERRUPTION = Counter(
        "streaming_interruptions_total",
        "Streaming interruptions/errors",
        labelnames=("model", "provider", "stage"),
        registry=REGISTRY,
    )

    RATE_LIMIT_HITS = Counter(
        "rate_limit_hits_total",
        "Rate limiter hits by model and outcome",
        labelnames=("model", "outcome"),  # outcome=accepted|rejected
        registry=REGISTRY,
    )

    def render_latest() -> bytes:
        return generate_latest(REGISTRY)

    @contextmanager
    def latency_timer(hist: Histogram, **labels: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            hist.labels(**labels).observe(elapsed)

else:  # No-op fallbacks to avoid hard dependency during tests
    REGISTRY = None  # type: ignore[assignment]
    API_REQUESTS = type("Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()})()
    API_REQUEST_LATENCY = type(
        "NoopH",
        (),
        {
            "labels": lambda *a, **k: type("L", (), {"observe": _noop})(),
        },
    )()
    API_TTFT = API_REQUEST_LATENCY
    PROVIDER_LATENCY = API_REQUEST_LATENCY
    API_RETRIES = type("Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()})()
    API_FALLBACKS = type("Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()})()
    API_TOKENS = type("Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()})()
    API_TOKEN_COST_DOLLARS = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    MODEL_REFUSALS = type("Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()})()
    MODEL_TRUNCATIONS = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    STREAMING_INTERRUPTION = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    RATE_LIMIT_HITS = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()

    def render_latest() -> bytes:  # pragma: no cover
        return b"# metrics disabled\n"

    @contextmanager
    def latency_timer(_hist: Any, **_labels: str) -> Iterator[None]:  # pragma: no cover
        yield


__all__ = [
    "API_REQUESTS",
    "API_REQUEST_LATENCY",
    "API_TTFT",
    "PROVIDER_LATENCY",
    "API_RETRIES",
    "API_FALLBACKS",
    "API_TOKENS",
    "API_TOKEN_COST_DOLLARS",
    "MODEL_REFUSALS",
    "MODEL_TRUNCATIONS",
    "STREAMING_INTERRUPTION",
    "RATE_LIMIT_HITS",
    "render_latest",
    "latency_timer",
]
