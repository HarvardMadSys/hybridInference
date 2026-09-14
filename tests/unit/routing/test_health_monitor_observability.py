"""The ``health_check:`` probe loop must say what it found.

``HealthMonitor._run`` wrote its verdict into ``_status`` every interval and
logged nothing. The only reader, ``RoutingManager._group_adapters``, runs from
``apply()``, which bootstrap calls synchronously right after ``load()`` -- so the
prober task ``load()`` just created has not run a single probe and ``_status`` is
still empty. That is not a stale read but a guaranteed one: the probe has never
influenced a routing decision, and never told anyone what it saw either.

These tests pin the reporting half. Gating on the verdict is deliberately not
done here (it needs hysteresis and a guard against zeroing a model's last route
-- nine on-demand models have exactly one).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar

import pytest

import routing.health as health_module
from routing.health import HealthMonitor
from routing.manager import RoutingManager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
def test_the_verdict_is_still_advisory():
    """Guard the premise of every log line below: nothing gates on _status."""
    monitor = HealthMonitor(timeout_s=2, interval_s=60)
    monitor._record("http://gpu-1:8000/v1", healthy=False)

    assert monitor.is_healthy("http://gpu-1:8000/v1") is False
    # An endpoint the prober has never reached on is assumed healthy, which is
    # what makes an empty _status indistinguishable from an all-healthy fleet.
    assert monitor.is_healthy("http://never-probed:8000/v1") is True


@pytest.mark.unit
def test_a_failing_endpoint_logs_once_not_once_per_probe(caplog):
    monitor = HealthMonitor(timeout_s=2, interval_s=60)

    with caplog.at_level(logging.INFO, logger="routing.health"):
        for _ in range(5):
            monitor._record("http://gpu-1:8000/v1", healthy=False)

    records = [r for r in caplog.records if getattr(r, "event", None) == "endpoint_probe_failed"]
    assert len(records) == 1, "a permanently dead endpoint must not log every minute"
    assert records[0].levelno == logging.WARNING
    assert records[0].endpoint == "http://gpu-1:8000/v1"
    assert records[0].status == "unhealthy"


@pytest.mark.unit
def test_recovery_is_a_transition_too(caplog):
    monitor = HealthMonitor(timeout_s=2, interval_s=60)

    with caplog.at_level(logging.INFO, logger="routing.health"):
        monitor._record("http://gpu-1:8000/v1", healthy=False)
        monitor._record("http://gpu-1:8000/v1", healthy=True)
        monitor._record("http://gpu-1:8000/v1", healthy=True)

    events = [getattr(r, "event", None) for r in caplog.records]
    assert events == ["endpoint_probe_failed", "endpoint_probe_recovered"]


@pytest.mark.unit
def test_a_healthy_fleet_stays_silent(caplog):
    """The first pass compares against the optimistic default, not against None."""
    monitor = HealthMonitor(timeout_s=2, interval_s=60)

    with caplog.at_level(logging.INFO, logger="routing.health"):
        for _ in range(3):
            monitor._record("http://gpu-1:8000/v1", healthy=True)

    assert caplog.records == []


class _NullSession:
    """Stand-in for ``aiohttp.ClientSession``; every probe is stubbed out."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


@pytest.mark.unit
async def test_the_probe_loop_logs_one_line_for_many_failing_probes(caplog, monkeypatch):
    """End-to-end through ``_run``, which is where the repetition would come from."""
    monitor = HealthMonitor(timeout_s=1, interval_s=0)
    monitor.interval_s = 0.001

    async def _always_down(session, endpoint):
        return False

    monkeypatch.setattr(health_module.aiohttp, "ClientSession", _NullSession)
    monkeypatch.setattr(monitor, "_check_once", _always_down)

    with caplog.at_level(logging.INFO, logger="routing.health"):
        task = asyncio.create_task(monitor._run(["http://gpu-1:8000/v1"]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records = [r for r in caplog.records if getattr(r, "event", None) == "endpoint_probe_failed"]
    assert len(records) == 1
    assert monitor.status_snapshot() == {"http://gpu-1:8000/v1": False}


@pytest.mark.unit
def test_status_snapshot_is_detached():
    monitor = HealthMonitor(timeout_s=2, interval_s=60)
    monitor._record("http://gpu-1:8000/v1", healthy=False)

    snapshot = monitor.status_snapshot()
    snapshot["http://gpu-1:8000/v1"] = True

    assert monitor.is_healthy("http://gpu-1:8000/v1") is False


def _routing_yaml(tmp_path: Path, health_check: int) -> Path:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "default_router: fixed\n"
        f"health_check: {health_check}\n"
        "local_deployment:\n"
        "  - endpoint: http://gpu-1:8000/v1\n"
        "    models: [m]\n"
        "remote_deployment: []\n"
    )
    return path


@pytest.mark.unit
def test_manager_status_publishes_the_probe_verdicts(tmp_path: Path):
    """``/routing`` is where an operator can finally see what the prober knows."""

    class _Router:
        routes: ClassVar[dict[str, object]] = {}

    manager = RoutingManager(_Router(), _routing_yaml(tmp_path, health_check=0))
    manager.load()
    # Probing is off in this config, so there is no monitor and nothing to show.
    assert manager.get_status()["endpoint_health"] == {}

    manager.health = HealthMonitor(timeout_s=2, interval_s=60)
    manager.health._record("http://gpu-1:8000/v1", healthy=False)
    status = manager.get_status()

    assert status["endpoint_health"] == {"http://gpu-1:8000/v1": False}
    # Stated in the payload because the obvious reading of the map is wrong:
    # an unhealthy endpoint here is still taking traffic.
    assert status["endpoint_health_enforced"] is False
