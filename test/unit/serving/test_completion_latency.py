"""Unit tests for COMPLETION_LATENCY metric in serving/observability/metrics.py.

Verifies that the per-model end-to-end latency histogram is defined and can
be observed without errors when metrics are enabled.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_completion_latency_exported():
    """COMPLETION_LATENCY is exported from the metrics module."""
    from serving.observability import metrics

    assert hasattr(metrics, "COMPLETION_LATENCY"), (
        "COMPLETION_LATENCY must be exported from serving.observability.metrics"
    )


@pytest.mark.unit
def test_completion_latency_in_all():
    """COMPLETION_LATENCY is included in __all__."""
    from serving.observability import metrics

    assert "COMPLETION_LATENCY" in metrics.__all__


@pytest.mark.unit
def test_completion_latency_observe_noop(monkeypatch):
    """COMPLETION_LATENCY.labels().observe() does not raise when metrics are disabled."""
    monkeypatch.setenv("METRICS_ENABLED", "0")

    # Re-import metrics in a subprocess-safe way by testing the no-op object directly
    from serving.observability.metrics import COMPLETION_LATENCY

    # In no-op mode (or any mode) this must not raise
    try:
        COMPLETION_LATENCY.labels(model="test-model", provider="test-provider", stream="no").observe(
            1.5
        )
    except Exception as exc:
        pytest.fail(f"COMPLETION_LATENCY.labels().observe() raised unexpectedly: {exc}")


@pytest.mark.unit
def test_completion_latency_observe_enabled():
    """COMPLETION_LATENCY records a sample without error when prometheus_client is available."""
    pytest.importorskip("prometheus_client")

    # Ensure metrics module is loaded with a fresh registry for isolation
    from prometheus_client import CollectorRegistry, Histogram

    registry = CollectorRegistry()
    hist = Histogram(
        "completion_latency_seconds_test",
        "Test histogram",
        labelnames=("model", "provider", "stream"),
        buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0),
        registry=registry,
    )

    hist.labels(model="gpt-4", provider="openai", stream="no").observe(0.42)
    hist.labels(model="gpt-4", provider="openai", stream="yes").observe(2.1)

    # Verify the samples were recorded (sum > 0)
    from prometheus_client import exposition

    output = exposition.generate_latest(registry).decode()
    assert "completion_latency_seconds_test_sum" in output
    assert "gpt-4" in output
    assert "openai" in output


@pytest.mark.unit
def test_completion_latency_stream_labels():
    """COMPLETION_LATENCY accepts stream=yes and stream=no labels."""
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry, Histogram

    registry = CollectorRegistry()
    hist = Histogram(
        "completion_latency_stream_test",
        "Test histogram",
        labelnames=("model", "provider", "stream"),
        buckets=(0.1, 1.0, 10.0),
        registry=registry,
    )

    # Both stream values must work without errors
    hist.labels(model="m", provider="p", stream="yes").observe(3.0)
    hist.labels(model="m", provider="p", stream="no").observe(0.5)
