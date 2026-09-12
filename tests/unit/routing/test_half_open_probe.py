"""Behavior contracts for the half-open probe.

A half-open circuit is the gateway asking one question -- "is this endpoint back
yet?" -- and it can only be answered by sending one request. Admitting every
concurrent caller instead turns each cooldown expiry into a fresh stampede
against an endpoint that is probably still down, so admission is split in two:
``allow_request`` answers (purely) whether an endpoint is a candidate, and
``begin_dispatch`` claims the right to be the one probe.

The claim is a resource with a dispatch's lifetime, like the prefill lease next
to it: whoever takes it hands it back in a ``finally``, and only the holder can,
so neither an abandoned stream nor a request that bypassed admission entirely can
decide when the next probe starts.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitBreaker, _CircuitState
from routing.routers import AllCircuitsOpenError, FixedRouter
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "probe-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_HEALTHY_ENDPOINT = f"{_MODEL_ID}:healthy"
_RECOVERING_ENDPOINT = f"{_MODEL_ID}:recovering"


@pytest.fixture(autouse=True)
def _one_failure_opens_a_thirty_second_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")


class _CountingAdapter(BaseAdapter):
    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)
        self.chat_calls = 0

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _adapter(provider: str, endpoint_id: str, price: str = "1") -> _CountingAdapter:
    return _CountingAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider=provider,
            base_url=f"https://{provider}.example/v1",
            endpoint_id=endpoint_id,
            pricing={"prompt": price, "completion": price},
        )
    )


def _routewise_router(registry: EndpointHealthRegistry, route: FixedRouter) -> RouteWiseRouter:
    return RouteWiseRouter(
        route_table=route,
        config=RouteWiseConfig(
            budget_alpha=0.0,
            random_seed=0,
            fallback_mode="strict",
            db_bootstrap_enabled=False,
        ),
        health_registry=registry,
    )


async def _open_circuit(registry: EndpointHealthRegistry, endpoint_id: str) -> None:
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(endpoint_id, reason="upstream_502")
        await asyncio.sleep(0)
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN


def _elapse_cooldown(registry: EndpointHealthRegistry, endpoint_id: str) -> None:
    """Put the endpoint at the far end of its cooldown.

    The clock is monotonic, so age it rather than sleep through it.
    """
    circuit = registry._circuits[endpoint_id]
    circuit.last_opened -= circuit.cooldown_seconds + 1


async def test_enumeration_never_consumes_the_probe_slot() -> None:
    """Asking is free.

    Routers ask ``allow_request`` of every candidate on a route and then dispatch
    to at most one, so a slot spent on the question would be spent for endpoints
    nobody calls -- and, with no outcome ever recorded for them, never handed
    back. That is how the endpoint this exists to rescue would be locked out of
    the pool instead.
    """
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    for _ in range(50):
        assert registry.allow_request(_RECOVERING_ENDPOINT) is True
        assert registry.snapshot()[_RECOVERING_ENDPOINT]["circuit_state"] == _CircuitState.OPEN

    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


async def test_only_one_concurrent_dispatcher_wins_the_probe() -> None:
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    start = threading.Barrier(8)
    claims: list[bool] = []
    claims_lock = threading.Lock()

    def _dispatch() -> None:
        start.wait()
        claimed = registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None
        with claims_lock:
            claims.append(claimed)

    threads = [threading.Thread(target=_dispatch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert claims.count(True) == 1
    assert registry.snapshot()[_RECOVERING_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN


async def test_a_successful_probe_closes_the_circuit_and_readmits_everyone() -> None:
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    claim = registry.begin_dispatch(_RECOVERING_ENDPOINT)
    assert claim is not None
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_success(_RECOVERING_ENDPOINT)
        await asyncio.sleep(0)
    registry.end_dispatch(claim)

    assert registry.snapshot()[_RECOVERING_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    # A closed circuit is not a queue: the endpoint is back, so every caller gets
    # it, not one at a time.
    assert all(registry.begin_dispatch(_RECOVERING_ENDPOINT) for _ in range(10))


async def test_a_failed_probe_reopens_the_circuit_and_restarts_the_cooldown() -> None:
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    claim = registry.begin_dispatch(_RECOVERING_ENDPOINT)
    assert claim is not None
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(_RECOVERING_ENDPOINT, reason="upstream_502")
        await asyncio.sleep(0)
    registry.end_dispatch(claim)

    assert registry.snapshot()[_RECOVERING_ENDPOINT]["circuit_state"] == _CircuitState.OPEN
    assert registry.allow_request(_RECOVERING_ENDPOINT) is False
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is None


def test_a_below_threshold_failure_hands_the_probe_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that fails without re-tripping is still a finished probe.

    ``on_failure`` returns early when the streak is short of the threshold,
    leaving the breaker half-open. The dispatch is over either way, so its
    release is what reopens the window -- the breaker does not read the outcome
    to decide, because an outcome is not proof of whose dispatch ended.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    circuit = _CircuitBreaker(_RECOVERING_ENDPOINT)
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = -circuit.cooldown_seconds

    admitted, token = circuit.begin_dispatch()
    assert admitted is True
    assert circuit.begin_dispatch() == (False, None)

    circuit.on_failure(reason="upstream_timeout")
    assert token is not None
    circuit.end_dispatch(token)

    # Still half-open: this is the early-return path, not a re-trip.
    assert circuit.state == _CircuitState.HALF_OPEN
    assert circuit.begin_dispatch()[0] is True


async def test_a_dispatch_that_reports_nothing_cannot_wedge_the_endpoint() -> None:
    """The deadline is the backstop for an outcome that never arrives.

    An abandoned SSE generator, a stream of nothing but keep-alives, or a
    selection the caller discarded all leave a claim with no outcome behind it.
    Its budget is the cooldown, so the cost is one more cooldown of delay rather
    than an endpoint that never gets probed again.
    """
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None
    assert registry.allow_request(_RECOVERING_ENDPOINT) is False
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is None

    circuit = registry._circuits[_RECOVERING_ENDPOINT]
    # Pretend the claim's deadline elapsed rather than sleeping through it.
    circuit._probe_deadline -= circuit.cooldown_seconds + 1

    assert registry.allow_request(_RECOVERING_ENDPOINT) is True
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


async def test_zero_cooldown_keeps_admission_unconditional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CIRCUIT_COOLDOWN_SECONDS=0`` is the deployment asking for no gating.

    The probe budget is derived from the cooldown, so a zero cooldown degenerates
    to the unconditional admission this breaker had before probes existed.
    """
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)

    for _ in range(10):
        assert registry.allow_request(_RECOVERING_ENDPOINT) is True
        assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


async def test_an_outcome_from_another_request_cannot_free_the_probe() -> None:
    """Only the holder hands the slot back.

    Not every request that reaches an endpoint went through ``begin_dispatch``:
    an explicit provider pin bypasses admission by design, and a request admitted
    while the circuit was still closed can still be in flight a cooldown later.
    Their outcomes say nothing about whether *this* probe has finished, and the
    exempted 4xx is the dangerous one -- it leaves the breaker half-open, so it
    is the only outcome that can repeat indefinitely without changing state. If
    it freed the slot, steady 4xx traffic would put an unbounded number of
    concurrent probes on a recovering endpoint: the stampede, restored.
    """
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    claim = registry.begin_dispatch(_RECOVERING_ENDPOINT)
    assert claim is not None

    for _ in range(5):
        registry.record_failure(
            _RECOVERING_ENDPOINT,
            reason="client_error",
            exc=_client_error(),
        )
        assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is None

    assert registry.snapshot()[_RECOVERING_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN
    # The probe's own dispatch ending is what reopens the window.
    registry.end_dispatch(claim)
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


async def test_a_lapsed_claim_cannot_release_its_successor() -> None:
    """A straggler releases nothing.

    The deadline can hand the slot to a new probe while the old dispatch is still
    unwinding somewhere. Releasing by endpoint id would then free the new probe's
    slot and put two on the wire; releasing by claim is inert.
    """
    registry = EndpointHealthRegistry()
    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    stale = registry.begin_dispatch(_RECOVERING_ENDPOINT)
    assert stale is not None
    circuit = registry._circuits[_RECOVERING_ENDPOINT]
    circuit._probe_deadline -= circuit.cooldown_seconds + 1

    successor = registry.begin_dispatch(_RECOVERING_ENDPOINT)
    assert successor is not None

    registry.end_dispatch(stale)
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is None
    registry.end_dispatch(successor)
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


def _client_error() -> Exception:
    error = RuntimeError("bad request")
    error.status_code = 400  # type: ignore[attr-defined]
    return error


@pytest.mark.asyncio
async def test_router_enumeration_leaves_a_half_open_endpoint_probeable() -> None:
    """The regression, end to end through FixedRouter.

    ``_select_adapter`` runs admission over every weighted candidate and then
    picks one, so a recovering endpoint that is never picked is enumerated on
    every single request. It must come out of that still able to be probed.
    """
    registry = EndpointHealthRegistry()
    healthy = _adapter("healthy", _HEALTHY_ENDPOINT)
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT)
    router = FixedRouter(health_registry=registry)
    router.register_route(_MODEL_ID, [(healthy, 0.9), (recovering, 0.1)])

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    with patch("routing.routers.random.random", return_value=0.0):
        for _ in range(50):
            response = await router.chat_completion(_MODEL_ID, _MESSAGES)
            assert response["_routing"]["endpoint_id"] == _HEALTHY_ENDPOINT

    assert recovering.chat_calls == 0
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


@pytest.mark.asyncio
async def test_router_selection_claims_the_probe_for_the_request_it_serves() -> None:
    """Selection is the commit point, so it is what spends the probe."""
    registry = EndpointHealthRegistry()
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT)
    router = FixedRouter(health_registry=registry)
    router.register_route(_MODEL_ID, [(recovering, 1.0)])

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    adapter, claim = router._select_and_claim_adapter(_MODEL_ID)
    assert adapter is recovering
    assert claim is not None
    # The probe is in flight and this route has nowhere else to go, so the next
    # caller gets the same answer an all-open route gives.
    with pytest.raises(AllCircuitsOpenError):
        router._select_and_claim_adapter(_MODEL_ID)
    # ...until the probe's dispatch ends and hands the slot back.
    router._health_registry.end_dispatch(claim)
    assert router._select_and_claim_adapter(_MODEL_ID)[0] is recovering


@pytest.mark.asyncio
async def test_routewise_enumeration_leaves_a_half_open_endpoint_probeable() -> None:
    """RouteWise enumerates far harder than FixedRouter does.

    ``_build_candidates`` runs over every route candidate on every solve, and a
    single request can solve many times -- once per retry, once per hedge
    checkpoint -- while committing to one endpoint. A model routed through
    RouteWise must not be left with the original stampede, nor with a probe slot
    burned by the solver's bookkeeping.
    """
    registry = EndpointHealthRegistry()
    healthy = _adapter("healthy", _HEALTHY_ENDPOINT, price="0.001")
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT, price="0.002")
    route = FixedRouter(health_registry=registry)
    route.register_route(_MODEL_ID, [(healthy, 0.5), (recovering, 0.5)])
    router = _routewise_router(registry, route)

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    for _ in range(20):
        decision = router._select_decision(_MODEL_ID, {})
        assert decision.adapter is healthy
        decision.release()

    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


@pytest.mark.asyncio
async def test_routewise_selection_claims_the_probe_for_the_request_it_serves() -> None:
    registry = EndpointHealthRegistry()
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT, price="0.001")
    route = FixedRouter(health_registry=registry)
    route.register_route(_MODEL_ID, [(recovering, 1.0)])
    router = _routewise_router(registry, route)

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    decision = router._select_decision(_MODEL_ID, {})
    assert decision.adapter is recovering
    # The probe is in flight and this route has nowhere else to go. Probe
    # contention is not a missing route: it is transient and the client should
    # retry, so it surfaces as the 503-mapped error FixedRouter raises for the
    # byte-identical condition rather than the generic ValueError that
    # ``completions.py`` turns into a 500.
    with pytest.raises(AllCircuitsOpenError):
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="second")
    # Releasing the decision returns both of the resources it committed.
    decision.release()
    assert router._select_decision(_MODEL_ID, {}).adapter is recovering


class _AbandonableAdapter(_CountingAdapter):
    """An adapter whose stream a consumer can walk away from.

    ``content`` empty models the other half of the same hazard: a well-formed 200
    that never carries a token, which the router's content-gated success signal
    never reports on.
    """

    def __init__(self, config: ModelConfig, *, content: str = "ok") -> None:
        super().__init__(config)
        self.content = content

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        self.chat_calls += 1
        for _ in range(3):
            yield self.format_stream_chunk(model=self.config.id, content=self.content)


@pytest.mark.asyncio
async def test_a_client_that_hangs_up_mid_probe_does_not_cost_a_cooldown() -> None:
    """The leak that matters in production.

    A client disconnect closes the generator with ``GeneratorExit``, which no
    ``except Exception`` catches and no success path reports, so the only unwind
    left is the generator's ``finally``. Miss it and one abandoned request takes a
    *healthy* single-route model to 503 for a whole cooldown.
    """
    registry = EndpointHealthRegistry()
    recovering = _AbandonableAdapter(_adapter("recovering", _RECOVERING_ENDPOINT).config)
    router = FixedRouter(health_registry=registry)
    router.register_route(_MODEL_ID, [(recovering, 1.0)])

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    stream = router.stream_chat_completion(_MODEL_ID, _MESSAGES)
    await stream.__anext__()  # the synthetic routing chunk
    await stream.aclose()

    response = await router.chat_completion(_MODEL_ID, _MESSAGES)
    assert response["_routing"]["endpoint_id"] == _RECOVERING_ENDPOINT


@pytest.mark.asyncio
async def test_a_stream_that_never_carries_content_does_not_cost_a_cooldown() -> None:
    """A 200 with nothing in it is still a finished dispatch."""
    registry = EndpointHealthRegistry()
    recovering = _AbandonableAdapter(
        _adapter("recovering", _RECOVERING_ENDPOINT).config, content=""
    )
    router = FixedRouter(health_registry=registry)
    router.register_route(_MODEL_ID, [(recovering, 1.0)])

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    async for _chunk in router.stream_chat_completion(_MODEL_ID, _MESSAGES):
        pass

    response = await router.chat_completion(_MODEL_ID, _MESSAGES)
    assert response["_routing"]["endpoint_id"] == _RECOVERING_ENDPOINT


@pytest.mark.asyncio
async def test_routewise_returns_the_probe_when_capacity_loses_the_race() -> None:
    """A commit is both gates or neither.

    RouteWise claims admission before capacity so a lost race cannot burn an
    unrefundable quota unit. Nothing is dispatched when the reservation then
    fails, so the probe has to go back too -- otherwise a saturated concurrency
    pool consumes the endpoint's cooldown every window and it is never probed.
    """
    registry = EndpointHealthRegistry()
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT, price="0.001")
    route = FixedRouter(health_registry=registry)
    route.register_route(_MODEL_ID, [(recovering, 1.0)])
    router = _routewise_router(registry, route)

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    with patch.object(router, "_reserve_candidate", return_value=None):
        assert router._select_decision(_MODEL_ID, {}) is None

    assert recovering.chat_calls == 0
    assert registry.allow_request(_RECOVERING_ENDPOINT) is True
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None


@pytest.mark.asyncio
async def test_routewise_returns_the_probe_when_the_decision_cannot_be_built() -> None:
    """The post-commit unwind owns both resources, not just the capacity one.

    ``_select_decision_locked`` already releases the reservation when metadata or
    hedge setup raises, on the stated principle that ownership stays lexical
    after a provider commit. The probe is the second thing that commit takes.
    """
    registry = EndpointHealthRegistry()
    recovering = _adapter("recovering", _RECOVERING_ENDPOINT, price="0.001")
    route = FixedRouter(health_registry=registry)
    route.register_route(_MODEL_ID, [(recovering, 1.0)])
    router = _routewise_router(registry, route)

    await _open_circuit(registry, _RECOVERING_ENDPOINT)
    _elapse_cooldown(registry, _RECOVERING_ENDPOINT)

    with (
        patch.object(router, "_decision_metadata", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        router._select_decision(_MODEL_ID, {})

    assert registry.allow_request(_RECOVERING_ENDPOINT) is True
    assert registry.begin_dispatch(_RECOVERING_ENDPOINT) is not None
