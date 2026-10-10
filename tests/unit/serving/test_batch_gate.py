"""Unit tests for the per-model batch load gate."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.batch_gate import LoadGate, ModelLoad, batch_may_run


def test_others_present_tracks_interactive_traffic_only() -> None:
    assert ModelLoad(1.0, 0.0, 100.0).others_present
    assert not ModelLoad(0.0, 500.0, 100.0).others_present


def test_batch_yields_when_others_present() -> None:
    assert not batch_may_run(
        ModelLoad(interactive_recent_per_hour=5.0, batch_recent_per_hour=0, baseline_per_hour=1000)
    )


def test_batch_caps_at_headroom_of_baseline() -> None:
    assert batch_may_run(ModelLoad(0.0, 700.0, 1000.0))
    assert not batch_may_run(ModelLoad(0.0, 900.0, 1000.0))


def test_zero_baseline_is_treated_as_idle() -> None:
    assert batch_may_run(ModelLoad(0.0, 99_999.0, 0.0))


@pytest.fixture
def gate_and_pool() -> tuple[LoadGate, MagicMock, MagicMock]:
    conn = MagicMock()
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return LoadGate(window_seconds=900), pool, conn


async def test_model_load_scales_window_to_hourly(gate_and_pool) -> None:
    gate, pool, conn = gate_and_pool
    conn.fetchval = AsyncMock(side_effect=[900, 90, 3600])
    load = await gate.model_load(pool, "some-model")
    assert load.interactive_recent_per_hour == pytest.approx(3600.0)
    assert load.batch_recent_per_hour == pytest.approx(360.0)
    assert load.baseline_per_hour == pytest.approx(3600.0)
