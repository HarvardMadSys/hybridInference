"""Failures below the circuit threshold must leave evidence.

``_CircuitBreaker.on_failure`` returned before any logger call when neither trip
condition was met, so failures 1 and 2 of every streak were invisible and an
endpoint failing steadily just under both thresholds -- the shape that never
pages -- produced no log evidence at all. The only record of it was an
``api_logs`` row per request, which is where the RCA had to go looking.
"""

from __future__ import annotations

import logging

import pytest

from routing.endpoint_health import _CircuitBreaker, _CircuitState


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")


@pytest.mark.unit
def test_a_sub_threshold_failure_is_recorded(caplog):
    breaker = _CircuitBreaker(provider="deepseek-v4-flash:local-8004")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        breaker.on_failure(availability=0.92, reason="upstream_500", detail="bad gateway")

    assert breaker.state == _CircuitState.CLOSED
    (record,) = [
        r for r in caplog.records if getattr(r, "event", None) == "endpoint_failure_below_threshold"
    ]
    assert record.provider == "deepseek-v4-flash:local-8004"
    assert record.consecutive_failures == 1
    assert record.threshold == 3
    assert record.availability == pytest.approx(0.92)
    assert record.reason == "upstream_500"
    assert record.upstream_error == "bad gateway"


@pytest.mark.unit
def test_every_failure_in_the_streak_is_recorded_until_the_circuit_trips(caplog):
    """Bounded volume: at most ``failure_threshold - 1`` lines before the page."""
    breaker = _CircuitBreaker(provider="deepseek-v4-flash:local-8004")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(5):
            breaker.on_failure(availability=0.95, reason="upstream_500")

    sub_threshold = [
        r for r in caplog.records if getattr(r, "event", None) == "endpoint_failure_below_threshold"
    ]
    assert [r.consecutive_failures for r in sub_threshold] == [1, 2]
    assert breaker.state == _CircuitState.OPEN


@pytest.mark.unit
def test_the_trip_itself_is_not_reported_as_sub_threshold(caplog):
    """An availability-floor trip opens on failure one; it must not log both."""
    breaker = _CircuitBreaker(provider="deepseek-v4-flash:local-8004")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        breaker.on_failure(availability=0.64, reason="stream_exception")

    events = [getattr(r, "event", None) for r in caplog.records]
    assert "endpoint_failure_below_threshold" not in events
    assert "circuit_open" in events


@pytest.mark.unit
def test_a_successful_request_resets_the_streak_and_the_reporting(caplog):
    breaker = _CircuitBreaker(provider="deepseek-v4-flash:local-8004")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        breaker.on_failure(availability=0.99, reason="upstream_500")
        breaker.on_success()
        breaker.on_failure(availability=0.99, reason="upstream_500")

    sub_threshold = [
        r for r in caplog.records if getattr(r, "event", None) == "endpoint_failure_below_threshold"
    ]
    assert [r.consecutive_failures for r in sub_threshold] == [1, 1]
