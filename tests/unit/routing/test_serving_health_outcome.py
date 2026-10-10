"""Serving health must require evidence of useful model progress.

Incident shape
--------------
A provider answered HTTP 200 with a well-formed body whose only content was a
warmup notice::

    "The model is starting up - this takes about 120 seconds. Please wait..."

That response *is* transport success: the socket was fine, the process was
alive, readiness passed. Every existing success path therefore counted it as a
healthy serving result, and ``record_success`` clears ``consecutive_failures``,
closes an OPEN or HALF_OPEN breaker, and ends a credential-rejection run.

During a cold start or model-loading stall that response repeats. Each repeat
re-closed a breaker the previous failures had just opened, so the endpoint read
as healthy while producing no work at all -- the process was up, the circuit was
closed, and no request was actually being served.

The durable rule lives in ``EndpointHealthRegistry.record_success`` rather than
at its many call sites, so these tests drive the registry directly. They also
pin the liveness/progress separation: a healthy *process* is not evidence of
useful *serving*.
"""

from __future__ import annotations

import pytest

from routing.completion_outcome import CompletionOutcome
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState

_ENDPOINT = "openai:api.example.com:443"
_THRESHOLD_ENV = "CIRCUIT_FAILURE_THRESHOLD"

WARMUP_STUB = "The model is starting up - this takes about 120 seconds. Please wait..."
REAL_ANSWER = "Applied the fix and verified it."


def _body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# Outcomes a provider can report that are NOT evidence of useful work.
NO_PROGRESS_OUTCOMES = [
    CompletionOutcome.TRANSIENT_NO_PROGRESS,
    CompletionOutcome.EMPTY,
    CompletionOutcome.REPEATED_NOOP,
    CompletionOutcome.PROVIDER_ERROR,
    CompletionOutcome.ABORTED,
    CompletionOutcome.UNKNOWN,
]


def _trip(monkeypatch, registry: EndpointHealthRegistry, endpoint_id: str = _ENDPOINT) -> None:
    """Drive ``endpoint_id`` into an OPEN breaker via consecutive failures."""
    for _ in range(3):
        registry.record_failure(endpoint_id, reason="upstream_502")
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN


def test_repeated_warmup_responses_cannot_reset_serving_health(monkeypatch):
    """The incident itself: many non-empty warmup responses, breaker stays open."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    for _ in range(5):
        registry.record_success(_ENDPOINT, outcome=CompletionOutcome.TRANSIENT_NO_PROGRESS)

    circuit = registry._circuits[_ENDPOINT]
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN
    assert circuit.consecutive_failures == 3


def test_genuine_completion_recovers_the_breaker_normally(monkeypatch):
    """The other half of the contract: real work must still recover the breaker."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.PROGRESS)

    circuit = registry._circuits[_ENDPOINT]
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    assert circuit.consecutive_failures == 0


def test_every_non_progress_outcome_is_inert(monkeypatch):
    """No outcome lacking real work may clear accumulated failure state."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for outcome in NO_PROGRESS_OUTCOMES:
        registry = EndpointHealthRegistry()
        _trip(monkeypatch, registry)
        registry.record_success(_ENDPOINT, outcome=outcome)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN, outcome
        assert registry._circuits[_ENDPOINT].consecutive_failures == 3, outcome


def test_non_progress_does_not_force_close_half_open(monkeypatch):
    """A half-open breaker must stay half-open until real work proves recovery."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    assert registry.begin_dispatch(_ENDPOINT) is not None
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    for outcome in NO_PROGRESS_OUTCOMES:
        registry.record_success(_ENDPOINT, outcome=outcome)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN, outcome

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.COMPLETE)
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


def test_liveness_never_recovers_a_half_open_breaker(monkeypatch):
    """Transport liveness cannot close a breaker; only serving progress can."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    assert registry.begin_dispatch(_ENDPOINT) is not None
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    for _ in range(5):
        registry.record_liveness(_ENDPOINT)
        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.HALF_OPEN

    registry.record_success(_ENDPOINT, outcome=CompletionOutcome.COMPLETE)
    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED


def test_unclassified_success_is_rejected_not_counted(monkeypatch):
    """Silence is never positive evidence; callers must use liveness instead."""
    registry = EndpointHealthRegistry()
    _trip(monkeypatch, registry)

    with pytest.raises(TypeError):
        registry.record_success(_ENDPOINT)

    assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN


def test_process_liveness_is_not_serving_progress(monkeypatch):
    """An endpoint that answers but never infers must not claim serving progress.

    ``ensure`` and the snapshot are the process-level liveness surface: they stay
    healthy because the process really is alive. Only the breaker and the failure
    count -- the serving-health surface -- carry the adverse verdict.
    """
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    registry = EndpointHealthRegistry()
    registry.ensure(_ENDPOINT)

    for _ in range(4):
        registry.record_success(_ENDPOINT, outcome=CompletionOutcome.REPEATED_NOOP)

    full = registry.snapshot()
    # Liveness: the endpoint is tracked and reachable.
    assert _ENDPOINT in full
    assert full[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED
    # Serving progress: nothing was produced, so no failure is invented either.
    assert registry._circuits[_ENDPOINT].consecutive_failures == 0


def test_serving_health_correction_does_not_disturb_locality_evidence():
    """The breaker rule must not manufacture or destroy cache-locality evidence.

    Serving health and locality are separate authorities. Gating one on the other
    would either discard valid locality evidence during a health stall or treat a
    cache hit as proof the model was doing useful work.
    """
    from routing.routewise.prefix_cache import (
        Block,
        CacheScope,
        PrefixCacheCoordinator,
        SessionProviderPrefixMemory,
    )

    coordinator = PrefixCacheCoordinator(
        enabled=True,
        memory=SessionProviderPrefixMemory(min_match_tokens=1),
        block_size=8,
        secret=b"unit-test-secret",
        tokenize=lambda t: [ord(c) for c in t],
    )
    scope = CacheScope(
        user_hash="u",
        project_hash="p",
        session_hash="s",
        provider_id="prov",
        endpoint_id=_ENDPOINT,
        model_profile="m",
        key_slot_id="k",
        cache_affecting_params_hash="",
    )
    conversation = (Block(digest="a", token_count=4096),)
    coordinator.remember(scope, conversation)
    coordinator.record_evidence(scope, 4096, blocks=conversation)

    assert coordinator.lookup_state(scope).verified is True


# ---------------------------------------------------------------------------
# Router-level integration
#
# The registry tests above pin the authority rule directly. These prove both
# routers route through that authority, so the rule holds for real traffic and
# not merely for callers that pass an outcome by hand.
# ---------------------------------------------------------------------------


def _routers():
    from routing.routers import FixedRouter
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.router import RouteWiseRouter

    return (
        FixedRouter(health_registry=EndpointHealthRegistry()),
        RouteWiseRouter(config=RouteWiseConfig(), health_registry=EndpointHealthRegistry()),
    )


def test_both_routers_route_serving_health_through_the_outcome_gate(monkeypatch):
    """FixedRouter and RouteWise must behave identically on a warmup response."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for router in _routers():
        name = type(router).__name__
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_success(_ENDPOINT, _body(WARMUP_STUB))

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN, name
        assert registry._circuits[_ENDPOINT].consecutive_failures == 3, name


def test_both_routers_recover_on_genuine_progress(monkeypatch):
    """Real work must still close the breaker on both routers."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for router in _routers():
        name = type(router).__name__
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_success(_ENDPOINT, _body(REAL_ANSWER))

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.CLOSED, name


def test_router_on_success_without_a_response_records_liveness_not_success(monkeypatch):
    """A router call without an outcome keeps the historical contract."""
    monkeypatch.setenv(_THRESHOLD_ENV, "3")
    for router in _routers():
        name = type(router).__name__
        registry = router._health_registry
        _trip(monkeypatch, registry)

        router._on_success(_ENDPOINT)

        assert registry.snapshot()[_ENDPOINT]["circuit_state"] == _CircuitState.OPEN, name
