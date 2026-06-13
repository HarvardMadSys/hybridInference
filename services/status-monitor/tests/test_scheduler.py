"""Tests for the scheduler's probe cycle reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from status_monitor.config import AppConfig, RegistryConfig, Settings
from status_monitor.prober import ProbeResult
from status_monitor.scheduler import probe_once
from status_monitor.state import StatusStore

pytestmark = pytest.mark.asyncio


async def test_probe_once_prunes_when_no_targets(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = StatusStore(history_size=5, state_path=str(state_path))
    store.record(
        ProbeResult(model_id="gone", ok=True, checked_at="2026-06-13T00:00:00+00:00")
    )

    # No registry and no overrides => zero targets; the stale model must be
    # pruned (and persisted) rather than lingering on the dashboard.
    config = AppConfig(settings=Settings(), registry=RegistryConfig(path=None))
    await probe_once(config, store)

    assert store.snapshot()["total"] == 0
    assert state_path.is_file()  # pruned state was persisted
