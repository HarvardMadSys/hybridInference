"""The circuit-open page must say which threshold opened the circuit.

The breaker trips on either a failure streak or the availability floor, and the
floor fires independently of the streak. A page from a floor trip therefore read
"Consecutive Failures: 1" beside an availability figure, which looks like a
breaker that opens on a single error -- the opposite of what happened, and it
sends the operator after one request instead of an endpoint that has been
failing a third of its traffic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from routing.endpoint_health import _CircuitBreaker, _CircuitState
from serving.observability.alerts import reset_dedupe_state, reset_transition_state


@pytest.fixture(autouse=True)
def _clean_alert_state():
    reset_transition_state()
    reset_dedupe_state()
    yield
    reset_transition_state()
    reset_dedupe_state()


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")


async def _context_of_one_page(failures) -> dict:
    """Drive ``failures`` into a fresh breaker and return the page's context."""
    breaker = _CircuitBreaker(provider="glm-5.2:staging-api")
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        for kwargs in failures:
            breaker.on_failure(**kwargs)
        assert breaker.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        return mock_alert.await_args.args[2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_availability_floor_trip_names_the_floor_not_the_streak():
    """The production alert's shape: one failure, availability already sagging."""
    context = await _context_of_one_page([{"reason": "stream_exception", "availability": 0.64}])

    assert context["consecutive_failures"] == 1
    assert context["availability"] == "0.64"
    assert context["trip_cause"] == "availability 0.64 < 0.70"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streak_trip_names_the_streak_and_its_threshold():
    context = await _context_of_one_page([{"reason": "upstream_500"}] * 3)

    assert context["trip_cause"] == "consecutive_failures 3 >= 3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_conditions_are_reported_together():
    """When the streak completes on the same failure that crosses the floor.

    Availability holds up while the streak builds -- otherwise the floor would
    have tripped on the first failure -- and sags on the third.
    """
    context = await _context_of_one_page(
        [
            {"reason": "upstream_500", "availability": 0.95},
            {"reason": "upstream_500", "availability": 0.85},
            {"reason": "upstream_500", "availability": 0.62},
        ]
    )

    assert context["trip_cause"] == "consecutive_failures 3 >= 3 and availability 0.62 < 0.70"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trip_cause_is_present_when_availability_is_unknown():
    """A caller that reports no availability still gets an attributed cause."""
    context = await _context_of_one_page([{"reason": "upstream_500"}] * 3)

    assert context["availability"] == "n/a"
    assert "consecutive_failures" in context["trip_cause"]


@pytest.mark.unit
def test_circuit_open_log_carries_the_trip_cause(caplog):
    """The log line needs it too, and only whitelisted keys are serialized."""
    from serving.utils.logging import _STRUCTURED_LOG_KEYS

    assert "trip_cause" in _STRUCTURED_LOG_KEYS

    with caplog.at_level("WARNING", logger="routing.routers"):
        _CircuitBreaker(provider="glm-5.2:staging-api").on_failure(
            reason="stream_exception", availability=0.64
        )

    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "circuit_open"]
    assert record.trip_cause == "availability 0.64 < 0.70"


@pytest.mark.unit
def test_availability_floor_still_trips_below_the_streak_threshold():
    """Behavior is unchanged -- only the reporting is new."""
    breaker = _CircuitBreaker(provider="glm-5.2:staging-api")

    breaker.on_failure(reason="stream_exception", availability=0.64)

    assert breaker.consecutive_failures == 1
    assert breaker.state == _CircuitState.OPEN


@pytest.mark.unit
def test_healthy_availability_does_not_trip_before_the_streak():
    breaker = _CircuitBreaker(provider="glm-5.2:staging-api")

    breaker.on_failure(reason="stream_exception", availability=0.95)
    breaker.on_failure(reason="stream_exception", availability=0.95)

    assert breaker.state == _CircuitState.CLOSED
