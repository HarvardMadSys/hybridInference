"""Tests for the status store and persistence."""

from __future__ import annotations

from pathlib import Path

from status_monitor.prober import ProbeResult
from status_monitor.state import StatusStore


def _result(model_id: str, ok: bool, latency: float = 100.0) -> ProbeResult:
    return ProbeResult(
        model_id=model_id,
        ok=ok,
        checked_at="2026-06-13T00:00:00+00:00",
        latency_ms=latency,
    )


def test_history_is_bounded() -> None:
    store = StatusStore(history_size=3)
    for i in range(5):
        store.record(_result("m", ok=True, latency=float(i)))
    snap = store.snapshot()
    history = snap["models"][0]["history"]
    assert len(history) == 3
    assert [h["latency_ms"] for h in history] == [2.0, 3.0, 4.0]


def test_snapshot_counts_and_uptime() -> None:
    store = StatusStore(history_size=10)
    store.record(_result("a", ok=True))
    store.record(_result("a", ok=False))
    store.record(_result("b", ok=True))

    snap = store.snapshot()
    assert snap["total"] == 2
    assert snap["healthy"] == 1  # a's latest is a failure
    assert snap["unhealthy"] == 1
    model_a = next(m for m in snap["models"] if m["model_id"] == "a")
    assert model_a["uptime_ratio"] == 0.5


def test_persistence_round_trip(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.json")
    store = StatusStore(history_size=10, state_path=state_path)
    store.record(_result("a", ok=True, latency=42.0))
    store.save()

    reloaded = StatusStore(history_size=10, state_path=state_path)
    reloaded.load()
    snap = reloaded.snapshot()
    assert snap["total"] == 1
    assert snap["models"][0]["latest"]["latency_ms"] == 42.0
