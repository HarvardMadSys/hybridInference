"""A hedge frees its primary's capacity when the primary's leg ends, exactly once.

When a backup won a stream race, the losing primary was cancelled but its
concurrency slot stayed held until the backup's whole stream had finished --
on a subscription that allows one request at a time, the endpoint sat unusable
for the length of someone else's response. A primary that failed outright held
its slot the same way while the backup served the request.

The release is handed from the decision to the hedge (``ProviderReservation
.hand_off``), so whichever path frees the slot, it is freed once.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.decisions import ProviderReservation, RoutingDecision, RoutingTrace
from routing.routewise.router import RouteWiseRouter
from tests.unit.routing.test_routewise_router import _FakeRouteTable, _make_adapter

CONC = "test-model:conc"
API = "test-model:api"


class _Status503(RuntimeError):
    status_code = 503


def _content(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


def _router(conc: Any, api: Any) -> RouteWiseRouter:
    table = _FakeRouteTable()
    table.add("test-model", [(conc, 1.0), (api, 1.0)])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(
            random_seed=3, latency_hedge_mode="probability_target", latency_slo_sec=0.2
        ),
    )
    now = time.time()
    for _ in range(100):
        # The free concurrency slot is the primary, the paid API the backup.
        router._latency_profiles[CONC].record(now, 20.0)
        router._latency_profiles[API].record(now, 50.0)
    return router


def _legs() -> tuple[Any, Any]:
    conc = _make_adapter(provider_type="concurrency", endpoint_id=CONC, concurrency={"limit": 1})
    api = _make_adapter(
        provider_type="on_demand", endpoint_id=API, prompt_price="1.0", completion_price="2.0"
    )
    return conc, api


def _count_releases(monkeypatch, router: RouteWiseRouter) -> tuple[Any, list[float]]:
    pool = next(iter(router.concurrency_pools.values()))
    releases: list[float] = []
    release = pool.release

    def counting() -> None:
        releases.append(time.monotonic())
        release()

    monkeypatch.setattr(pool, "release", counting)
    return pool, releases


@pytest.mark.unit
def test_a_handed_off_release_happens_once():
    calls: list[int] = []
    reservation = ProviderReservation(lambda: calls.append(1))
    decision = RoutingDecision(
        adapter=object(), reservation=reservation, metadata={}, trace=RoutingTrace()
    )

    release = reservation.hand_off()
    decision.release()
    assert calls == []
    release()
    release()
    assert calls == [1]
    reservation.hand_off()()
    assert calls == [1]


@pytest.mark.unit
def test_a_released_reservation_hands_off_nothing():
    calls: list[int] = []
    reservation = ProviderReservation(lambda: calls.append(1))
    reservation.release()

    reservation.hand_off()()

    assert calls == [1]


@pytest.mark.unit
async def test_a_primary_that_loses_the_stream_race_frees_its_slot_once_closed(monkeypatch):
    conc, api = _legs()
    state: dict[str, float | None] = {"primary_closed_at": None}

    async def slow_primary(messages, **params):
        try:
            await asyncio.sleep(30)
            yield _content("late")
        finally:
            state["primary_closed_at"] = time.monotonic()

    async def backup(messages, **params):
        for i in range(6):
            yield _content(f"tok{i}")
            await asyncio.sleep(0.05)

    conc.stream_chat_completion = slow_primary
    api.stream_chat_completion = backup
    router = _router(conc, api)
    pool, releases = _count_releases(monkeypatch, router)

    active_during_backup = []
    async for chunk in router.stream_chat_completion(
        "test-model", [{"role": "user", "content": "x"}]
    ):
        if isinstance(chunk, str) and '"content": "tok' in chunk:
            active_during_backup.append(pool.active)
    stream_ended_at = time.monotonic()

    assert state["primary_closed_at"] is not None
    assert len(releases) == 1
    # Freed after the primary's stream closed, and long before the backup's
    # stream ended -- not held for the rest of it.
    assert state["primary_closed_at"] <= releases[0] < stream_ended_at - 0.1
    assert active_during_backup[-1] == 0
    assert pool.active == 0


@pytest.mark.unit
async def test_a_failed_primary_frees_its_slot_while_the_backup_streams(monkeypatch):
    conc, api = _legs()

    async def failing_primary(messages, **params):
        raise _Status503("overloaded")
        yield  # pragma: no cover - makes this an async generator

    async def backup(messages, **params):
        for i in range(6):
            yield _content(f"tok{i}")
            await asyncio.sleep(0.03)

    conc.stream_chat_completion = failing_primary
    api.stream_chat_completion = backup
    router = _router(conc, api)
    pool, releases = _count_releases(monkeypatch, router)

    active_during_backup = []
    async for chunk in router.stream_chat_completion(
        "test-model", [{"role": "user", "content": "x"}]
    ):
        if isinstance(chunk, str) and '"content": "tok' in chunk:
            active_during_backup.append(pool.active)

    assert active_during_backup and all(active == 0 for active in active_during_backup)
    assert len(releases) == 1


@pytest.mark.unit
async def test_a_failed_primary_frees_its_slot_while_a_non_streaming_backup_runs(monkeypatch):
    conc, api = _legs()
    backup_started = asyncio.Event()
    finish_backup = asyncio.Event()

    async def failing_primary(messages, **params):
        raise _Status503("overloaded")

    async def backup(messages, **params):
        backup_started.set()
        await finish_backup.wait()
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    conc.chat_completion = failing_primary
    api.chat_completion = backup
    router = _router(conc, api)
    pool, releases = _count_releases(monkeypatch, router)

    request = asyncio.create_task(
        router.chat_completion("test-model", [{"role": "user", "content": "x"}])
    )
    await asyncio.wait_for(backup_started.wait(), timeout=5)
    assert pool.active == 0
    assert len(releases) == 1
    finish_backup.set()
    response = await request

    assert response["_routing"]["endpoint_id"] == API
    assert len(releases) == 1


@pytest.mark.unit
async def test_a_primary_that_loses_a_non_streaming_race_is_freed_once(monkeypatch):
    conc, api = _legs()

    async def slow_primary(messages, **params):
        await asyncio.sleep(30)

    async def backup(messages, **params):
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    conc.chat_completion = slow_primary
    api.chat_completion = backup
    router = _router(conc, api)
    pool, releases = _count_releases(monkeypatch, router)

    response = await router.chat_completion("test-model", [{"role": "user", "content": "x"}])

    assert response["_routing"]["endpoint_id"] == API
    assert len(releases) == 1
    assert pool.active == 0


@pytest.mark.unit
async def test_a_caller_that_leaves_during_the_primarys_close_still_frees_it_once(monkeypatch):
    conc, api = _legs()
    closing = asyncio.Event()

    async def slow_primary(messages, **params):
        try:
            await asyncio.sleep(30)
            yield _content("late")
        finally:
            closing.set()
            # A provider that takes a while to close.
            await asyncio.sleep(0.2)

    async def backup(messages, **params):
        for i in range(50):
            yield _content(f"tok{i}")
            await asyncio.sleep(0.01)

    conc.stream_chat_completion = slow_primary
    api.stream_chat_completion = backup
    router = _router(conc, api)
    pool, releases = _count_releases(monkeypatch, router)

    stream = router.stream_chat_completion("test-model", [{"role": "user", "content": "x"}])
    async for chunk in stream:
        if isinstance(chunk, str) and '"content": "tok' in chunk:
            break
    await asyncio.wait_for(closing.wait(), timeout=5)
    # The caller goes away while the losing primary is still closing.
    await stream.aclose()
    assert pool.active == 1
    for _ in range(100):
        if releases:
            break
        await asyncio.sleep(0.01)

    assert len(releases) == 1
    assert pool.active == 0


@pytest.mark.unit
async def test_a_caller_that_leaves_frees_the_winning_backup_before_aclose_returns():
    """The attempt is closed in the caller's task, not left to the event loop's finalizer."""
    primary = _make_adapter(
        provider_type="concurrency", endpoint_id="test-model:conc-a", concurrency={"limit": 1}
    )
    backup = _make_adapter(
        provider_type="concurrency", endpoint_id="test-model:conc-b", concurrency={"limit": 1}
    )

    async def slow_primary(messages, **params):
        await asyncio.sleep(30)
        yield _content("late")

    async def streaming_backup(messages, **params):
        for i in range(50):
            yield _content(f"tok{i}")
            await asyncio.sleep(0.01)

    primary.stream_chat_completion = slow_primary
    backup.stream_chat_completion = streaming_backup
    table = _FakeRouteTable()
    table.add("test-model", [(primary, 1.0), (backup, 1.0)])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(
            random_seed=3, latency_hedge_mode="probability_target", latency_slo_sec=0.2
        ),
    )
    now = time.time()
    for _ in range(100):
        router._latency_profiles["test-model:conc-a"].record(now, 20.0)
        router._latency_profiles["test-model:conc-b"].record(now, 50.0)
    backup_pool = router.concurrency_pools[router._endpoint_concurrency_pool["test-model:conc-b"]]

    stream = router.stream_chat_completion("test-model", [{"role": "user", "content": "x"}])
    async for chunk in stream:
        if isinstance(chunk, str) and '"content": "tok' in chunk:
            break
    assert backup_pool.active == 1

    await stream.aclose()

    assert backup_pool.active == 0
