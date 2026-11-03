"""A RouteWise candidate that loses its reservation race is a capacity refusal.

``_build_candidates`` reads a pool's free capacity, the solver picks from what it
built, and ``_commit_candidate`` only then takes the reservation. Another request
can take the last concurrency slot, or spend the last quota unit, in between.
The commit then fails while the endpoint's circuit still admits it: the claim
was granted and handed back, so the refusal is capacity, not health.

When every candidate is lost that way the request must end as the capacity
``TargetUnavailableError`` (503, which clients retry), not the
``ValueError("No route configured ...")`` that the API maps to a 500 and an
operator reads as a models.yaml mistake. Nothing was sent upstream, so no
endpoint may be charged a failure for it.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import AllCircuitsOpenError, FixedRouter, TargetUnavailableError
from routing.routewise.config import RouteWiseConfig
from routing.routewise.quota import ProviderQuotaSnapshot
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "race-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-8200"
_QUOTA_ENDPOINT = f"{_MODEL_ID}:subscription-api"
_CONCURRENCY_POOL = "gpu-pool"


class _CountingAdapter(BaseAdapter):
    """Adapter double that counts every upstream call it would have made."""

    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)
        self.calls = 0

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.calls += 1
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'


def _concurrency_adapter() -> _CountingAdapter:
    return _CountingAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider="local",
            base_url="http://localhost:8200/v1",
            endpoint_id=_LOCAL_ENDPOINT,
            pricing={"prompt": "0", "completion": "0"},
            provider_type="concurrency",
            concurrency={"limit": 1},
            concurrency_pool=_CONCURRENCY_POOL,
        )
    )


def _quota_adapter() -> _CountingAdapter:
    return _CountingAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider="subscription",
            base_url="https://subscription.example/v1",
            endpoint_id=_QUOTA_ENDPOINT,
            pricing={"prompt": "3", "completion": "15"},
            provider_type="quota",
            quota={"limit": 1},
            quota_source={
                "provider": "stub",
                "usage_label": "Daily requests",
                "unit": "requests",
            },
        )
    )


def _router(adapter: _CountingAdapter) -> tuple[RouteWiseRouter, EndpointHealthRegistry]:
    registry = EndpointHealthRegistry()
    table = FixedRouter(health_registry=registry)
    table.register_route(_MODEL_ID, [(adapter, 1.0)])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(
            budget_alpha=0.0,
            random_seed=0,
            routewise_probe_enabled=False,
            db_bootstrap_enabled=False,
        ),
        health_registry=registry,
    )
    return router, registry


def _ready_quota_pool(router: RouteWiseRouter) -> Any:
    """Seed the only quota pool with a fresh provider snapshot and calibrate prices.

    Quota pools are snapshot-backed and quota candidates are priced off the cost
    envelope; with either missing the build skips the candidate before any
    reservation is attempted, which is not the race under test.
    """
    pool = next(iter(router.quota_pools.values()))
    router.quota_snapshots._snapshots[pool.source] = ProviderQuotaSnapshot(
        source=pool.source,
        used=0.0,
        limit=float(pool.policy.limit),
        reset_at=None,
        fetched_at=dt.datetime.now(dt.UTC),
    )
    router.quota_snapshots._local_increments[pool.source] = 0
    envelope_pool = router._routewise_pool(_MODEL_ID)
    observed_at = time.time() - 1.0
    for cost in (0.001, 0.5) * 25:
        router.envelope.observe(envelope_pool, cost, now=observed_at)
    return pool


def _lose_the_concurrency_race(router: RouteWiseRouter, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Let a concurrent request take the pool's last slot between build and commit."""
    pool = router.concurrency_pools[_CONCURRENCY_POOL]
    real_try_acquire = pool.try_acquire

    def competitor_takes_the_slot_first() -> bool:
        assert real_try_acquire() is True, "the competitor should get the free slot"
        return real_try_acquire()

    monkeypatch.setattr(pool, "try_acquire", competitor_takes_the_slot_first)
    return pool


def _forbid_failure_samples(
    registry: EndpointHealthRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def record_failure(endpoint_id: str, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"{endpoint_id} was charged a failure it never had")

    monkeypatch.setattr(registry, "record_failure", record_failure)


async def _route(router: RouteWiseRouter, *, streaming: bool) -> None:
    if streaming:
        async for _chunk in router.stream_chat_completion(_MODEL_ID, _MESSAGES):
            pass
    else:
        await router.chat_completion(_MODEL_ID, _MESSAGES)


# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["chat", "stream"])
async def test_a_lost_concurrency_race_is_a_capacity_refusal(
    streaming: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only candidate's slot goes to another request after the build saw it free."""
    adapter = _concurrency_adapter()
    router, registry = _router(adapter)
    pool = _lose_the_concurrency_race(router, monkeypatch)
    _forbid_failure_samples(registry, monkeypatch)

    with pytest.raises(TargetUnavailableError, match="has capacity") as raised:
        await _route(router, streaming=streaming)

    assert _LOCAL_ENDPOINT in str(raised.value)
    assert adapter.calls == 0, "a request that lost its reservation was sent upstream"
    # The competitor's slot is untouched: the lost commit neither leaked one nor
    # released one it never held.
    assert pool.active == 1
    snapshot = registry.snapshot()[_LOCAL_ENDPOINT]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    assert snapshot["last_error_status"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_lost_quota_race_is_a_capacity_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last quota unit is spent by another request after the build saw it."""
    adapter = _quota_adapter()
    router, registry = _router(adapter)
    pool = _ready_quota_pool(router)
    real_consume = pool.consume

    def competitor_spends_the_last_unit() -> bool:
        assert real_consume() is True, "the competitor should get the last unit"
        return real_consume()

    monkeypatch.setattr(pool, "consume", competitor_spends_the_last_unit)
    _forbid_failure_samples(registry, monkeypatch)

    with pytest.raises(TargetUnavailableError, match="has capacity") as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert _QUOTA_ENDPOINT in str(raised.value)
    assert adapter.calls == 0
    assert pool.remaining == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_half_open_endpoint_that_loses_its_capacity_race_keeps_its_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe claim was granted, then handed back: still capacity, not health.

    This is the case where reading the circuit after the commit matters most.
    ``begin_dispatch`` moved the endpoint to half-open and claimed its probe;
    the reservation then failed and the claim was returned, so the circuit
    admits the endpoint again and the next request may still probe it.
    """
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    adapter = _concurrency_adapter()
    router, registry = _router(adapter)
    registry.ensure(_LOCAL_ENDPOINT)
    circuit = registry._circuits[_LOCAL_ENDPOINT]
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = time.perf_counter() - circuit.cooldown_seconds - 1
    _lose_the_concurrency_race(router, monkeypatch)
    _forbid_failure_samples(registry, monkeypatch)

    with pytest.raises(TargetUnavailableError, match="has capacity"):
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert adapter.calls == 0
    assert circuit.state == _CircuitState.HALF_OPEN
    assert registry.allow_request(_LOCAL_ENDPOINT), "the lost commit kept the probe slot"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_commit_the_circuit_refuses_stays_an_admission_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other branch: the circuit opened between build and commit.

    The build saw the endpoint admissible; by the commit it was open. That is the
    health refusal the build filter reports, observed a moment later, and must
    not be relabelled as capacity.
    """
    adapter = _concurrency_adapter()
    router, registry = _router(adapter)
    real_begin_dispatch = registry.begin_dispatch

    def circuit_opens_first(endpoint_id: str) -> Any:
        circuit = registry._circuits[endpoint_id]
        circuit.state = _CircuitState.OPEN
        circuit.last_opened = time.perf_counter()
        return real_begin_dispatch(endpoint_id)

    monkeypatch.setattr(registry, "begin_dispatch", circuit_opens_first)

    with pytest.raises(AllCircuitsOpenError, match=_LOCAL_ENDPOINT):
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert adapter.calls == 0
    assert router.concurrency_pools[_CONCURRENCY_POOL].active == 0
