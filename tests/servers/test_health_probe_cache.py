"""The anonymous health endpoints share one store probe.

``/health``, ``/health/ready`` and ``/health/deep`` each ran a ``SELECT 1`` per
store, and scheduled the outage and recovery alert checks, on every call -- from
anyone, as often as they liked. One probe now answers every caller of any of
them for a couple of seconds, and callers that arrive while it runs wait for it
instead of starting their own.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import health
from serving.utils.single_flight import SingleFlightCache

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(
        health,
        "_HEALTH_PROBES",
        SingleFlightCache(health.HEALTH_PROBE_TTL_SEC, max_entries=1, clock=clock),
    )
    return clock


def _healthy(app_services) -> None:
    app_services.operational_store.health_check = AsyncMock(return_value=True)
    app_services.log_store.health_check = AsyncMock(return_value=True)


def _probes(app_services) -> tuple[int, int]:
    return (
        app_services.operational_store.health_check.await_count,
        app_services.log_store.health_check.await_count,
    )


def test_the_window_is_a_few_seconds():
    assert 1 <= health.HEALTH_PROBE_TTL_SEC <= 5


async def test_calls_within_the_window_share_one_probe(test_client, app_services, clock):
    _healthy(app_services)

    first = await test_client.get("/health")
    clock.now += health.HEALTH_PROBE_TTL_SEC - 0.1
    second = await test_client.get("/health")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert _probes(app_services) == (1, 1)


async def test_the_next_call_after_the_window_probes_again(test_client, app_services, clock):
    _healthy(app_services)
    await test_client.get("/health")

    clock.now += health.HEALTH_PROBE_TTL_SEC + 0.1
    app_services.operational_store.health_check = AsyncMock(return_value=False)
    app_services.log_store.health_check = AsyncMock(return_value=False)
    resp = await test_client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["reason"] == "database_disconnected"


async def test_concurrent_callers_wait_for_the_probe_in_flight(test_client, app_services, clock):
    release = asyncio.Event()

    async def slow_probe() -> bool:
        await release.wait()
        return True

    app_services.operational_store.health_check = AsyncMock(side_effect=slow_probe)
    app_services.log_store.health_check = AsyncMock(side_effect=slow_probe)

    calls = [asyncio.ensure_future(test_client.get("/health")) for _ in range(20)]
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(*calls)

    assert {r.status_code for r in responses} == {200}
    assert {r.json()["status"] for r in responses} == {"healthy"}
    assert _probes(app_services) == (1, 1)


async def test_alerts_are_evaluated_once_per_probe(test_client, app_services, clock, monkeypatch):
    _healthy(app_services)
    reports: list[str] = []

    async def record(**kwargs) -> bool:
        reports.append(kwargs["key"])
        return True

    monkeypatch.setattr(health, "alert_on_transition", record)

    for _ in range(5):
        await test_client.get("/health")
    await asyncio.sleep(0)

    # One operational and one log-store transition check, not five of each.
    assert len(reports) == 2


async def test_readiness_and_the_deep_check_share_the_probe(test_client, app_services, clock):
    _healthy(app_services)

    responses = [
        await test_client.get(path)
        for path in ("/health/ready", "/health/ready", "/health/deep", "/health")
    ]

    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    assert _probes(app_services) == (1, 1)


async def test_readiness_sees_an_outage_once_the_window_has_passed(
    test_client, app_services, clock
):
    _healthy(app_services)
    await test_client.get("/health/ready")

    clock.now += health.HEALTH_PROBE_TTL_SEC + 0.1
    app_services.log_store.health_check = AsyncMock(return_value=False)
    resp = await test_client.get("/health/ready")

    assert resp.status_code == 503
    assert resp.json()["reason"] == "store_degraded"


async def test_a_caller_cannot_change_what_the_next_one_is_told(app_services, clock):
    _healthy(app_services)
    stores = (app_services.operational_store, app_services.log_store)

    first = await health._cached_store_health(*stores)
    first["operational_store"]["status"] = "tampered"
    second = await health._cached_store_health(*stores)

    assert second["operational_store"]["status"] == "ok"
