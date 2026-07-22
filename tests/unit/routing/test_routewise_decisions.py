"""Contracts for request-local RouteWise decisions and traces."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.routewise.decisions import (
    ProviderReservation,
    RoutingDecision,
    RoutingTrace,
)


@pytest.mark.unit
def test_provider_reservation_releases_exact_callback_once():
    released: list[str] = []
    reservation = ProviderReservation(lambda: released.append("acquired-pool"))

    reservation.release()
    reservation.release()

    assert released == ["acquired-pool"]
    assert reservation.released is True


@pytest.mark.unit
def test_provider_reservation_stays_idempotent_when_callback_raises():
    calls = 0

    def _raise() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("release failed")

    reservation = ProviderReservation(_raise)

    with pytest.raises(RuntimeError, match="release failed"):
        reservation.release()
    reservation.release()

    assert calls == 1
    assert reservation.released is True


@pytest.mark.unit
def test_routing_trace_carries_initial_selection_and_fallback_history():
    trace = RoutingTrace(request_id="req-1")
    first = RoutingDecision(
        adapter=SimpleNamespace(),
        reservation=ProviderReservation(),
        metadata={
            "selected_endpoint": "primary",
            "selected_provider_type": "concurrency",
            "failed_attempts": [
                {"endpoint_id": "hedge", "error_type": "TimeoutError", "error": "slow"}
            ],
        },
        trace=trace,
    )
    trace.begin_decision(first.metadata)
    primary_failure = {
        "endpoint_id": "primary",
        "error_type": "TimeoutError",
        "error": "slow",
    }
    trace.record_failed_attempt(primary_failure)
    trace.record_fallback(
        first,
        primary_failure,
        fallback_policy="routewise_resolve",
    )

    second_metadata = {
        "selected_endpoint": "fallback",
        "selected_provider_type": "on_demand",
    }
    trace.begin_decision(second_metadata)

    assert second_metadata["initial_selected_endpoint"] == "primary"
    assert second_metadata["initial_selected_provider_type"] == "concurrency"
    assert second_metadata["fallback_policy"] == "routewise_resolve"
    assert second_metadata["fallback_attempts"] == 2
    assert second_metadata["fallback_excluded_endpoints"] == ["hedge", "primary"]
    assert [attempt["endpoint_id"] for attempt in second_metadata["failed_attempts"]] == [
        "hedge",
        "primary",
    ]
    assert trace.failed_attempts == [primary_failure]
