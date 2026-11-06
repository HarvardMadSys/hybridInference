"""A request refused for its own content is no evidence about an endpoint's latency.

RouteWise turns every failed observation into an error sample in the endpoint's
latency profile, and the LP and the hedge planner read each one as a TTFT of
``RATE_LIMIT_ERROR_PENALTY_MS`` (60 s). Before the failure's status travelled
with the observation, a client-caused 4xx counted the same as an outage, so any
caller could steer RouteWise away from an endpoint -- toward costlier routes and
more hedging -- by sending it malformed requests.

The rule under test mirrors the breaker's client-error exemption
(``is_client_error_status``): 4xx other than 408, 429 and the rejected-credential
statuses are left out; 408, 429, 5xx and failures without a status still count.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from routing.endpoint_health import is_client_error_status
from routing.hybrid_attempt_history import attempt_record
from routing.routers import RoutingObservation
from routing.telemetry import failed_attempt
from serving.servers.routers.completions_logging import CompletionsLogger
from tests.unit.routing.test_routewise_router import _make_router_with_two_api

_ENDPOINT = "test-model:api-a"
_CLIENT_STATUSES = [400, 403, 404, 413, 422]
_ENDPOINT_FAULTS = [None, 401, 407, 408, 429, 500, 502, 503]


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream answered {status_code}")
        self.status_code = status_code


def _failure(status_code: int | None, *, terminal: bool = True) -> RoutingObservation:
    return RoutingObservation(
        model_id="test-model",
        endpoint_id=_ENDPOINT,
        ttft_ms=None,
        total_latency_ms=5000.0,
        token_count=0,
        prompt_tokens=100,
        completion_tokens=0,
        success=False,
        terminal=terminal,
        strategy_metadata={"routewise": {"quota_committed": 0.0}},
        status_code=status_code,
    )


def _outcomes(router: Any) -> int:
    return router._latency_profiles[_ENDPOINT].total_count(time.time())


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("status", _CLIENT_STATUSES)
def test_client_statuses_blame_the_request(status):
    assert is_client_error_status(status)


@pytest.mark.unit
@pytest.mark.parametrize("status", _ENDPOINT_FAULTS)
def test_other_statuses_do_not(status):
    assert not is_client_error_status(status)


# ---------------------------------------------------------------------------
# Live observations
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "earlier-attempt"])
@pytest.mark.parametrize("status", _CLIENT_STATUSES)
def test_a_client_error_adds_no_sample(status, terminal):
    router, _a, _b = _make_router_with_two_api()

    router.record_observation(_failure(status, terminal=terminal))

    assert _outcomes(router) == 0


@pytest.mark.unit
@pytest.mark.parametrize("status", _ENDPOINT_FAULTS)
def test_an_endpoint_fault_is_still_a_penalty_sample(status):
    router, _a, _b = _make_router_with_two_api()

    router.record_observation(_failure(status))

    assert _outcomes(router) == 1
    assert router._latency_profiles[_ENDPOINT].sample_count(time.time()) == 0


@pytest.mark.unit
def test_malformed_requests_cannot_raise_an_endpoints_latency_estimate():
    """The attack in one test: a burst of 400s leaves the LP's estimate where it was."""
    router, _a, _b = _make_router_with_two_api()
    now = time.time()
    router._latency_profiles[_ENDPOINT].record(now, 200.0)
    before = router._mean_ttft_sec(_ENDPOINT, now)

    for _ in range(50):
        router.record_observation(_failure(400))

    assert router._mean_ttft_sec(_ENDPOINT, time.time()) == pytest.approx(before)


@pytest.mark.unit
def test_an_observation_without_the_field_still_counts():
    """A caller predating ``status_code`` (or a duck-typed one) keeps the old reading."""
    router, _a, _b = _make_router_with_two_api()
    obs = SimpleNamespace(
        model_id="test-model",
        endpoint_id=_ENDPOINT,
        ttft_ms=None,
        total_latency_ms=5000.0,
        token_count=0,
        prompt_tokens=100,
        completion_tokens=0,
        success=False,
        terminal=True,
        request_id=None,
        cached_tokens=None,
        strategy_metadata={},
    )

    router.record_observation(obs)

    assert _outcomes(router) == 1


# ---------------------------------------------------------------------------
# Startup replay from api_logs
# ---------------------------------------------------------------------------


def _row(status_code: int, *, failed_attempts: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {
        "timestamp": time.time(),
        "model_id": "test-model",
        "provider": "p",
        "endpoint_id": _ENDPOINT,
        "ttft_ms": None,
        "latency_ms": 100.0,
        "status_code": status_code,
        "error": "upstream refused" if status_code >= 400 else None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "failed_attempts": failed_attempts,
    }


@pytest.mark.unit
def test_replay_leaves_out_what_the_live_path_leaves_out():
    router, _a, _b = _make_router_with_two_api()
    attempt = {"endpoint_id": _ENDPOINT, "error_type": "ClientResponseError", "error": "x"}

    router.bootstrap_from_log_rows(
        [
            _row(400),
            _row(422),
            _row(500, failed_attempts=({**attempt, "status_code": 400},)),
        ],
        include_envelope=False,
    )

    # Only the 500 itself: the two client rows and the refused attempt are dropped.
    assert _outcomes(router) == 1


@pytest.mark.unit
def test_replay_keeps_endpoint_faults_and_older_records():
    router, _a, _b = _make_router_with_two_api()
    attempt = {"endpoint_id": _ENDPOINT, "error_type": "TimeoutError", "error": "slow"}

    router.bootstrap_from_log_rows(
        [
            _row(429),
            _row(503, failed_attempts=({**attempt, "status_code": 429},)),
            # Recorded before attempts carried a status: still counted, as before.
            _row(502, failed_attempts=({**attempt, "error": "older"},)),
        ],
        include_envelope=False,
    )

    assert _outcomes(router) == 5


# ---------------------------------------------------------------------------
# The status travels: attempt records, then observations
# ---------------------------------------------------------------------------


def _adapter(endpoint_id: str) -> Any:
    return SimpleNamespace(config=SimpleNamespace(provider="p", endpoint_id=endpoint_id))


@pytest.mark.unit
def test_attempt_records_carry_the_status_when_there_is_one():
    assert failed_attempt(_adapter("m:a"), _StatusError(400))["status_code"] == 400
    assert "status_code" not in failed_attempt(_adapter("m:a"), TimeoutError("late"))
    assert attempt_record("cloud", "m:a", _StatusError(429))["status_code"] == 429
    assert "status_code" not in attempt_record("cloud", "m:a", ConnectionError("down"))


class _Recorder:
    def __init__(self) -> None:
        self.observations: list[RoutingObservation] = []

    def record_observation(self, obs: RoutingObservation) -> None:
        self.observations.append(obs)


def _record(routing: dict[str, Any], **kwargs: Any) -> list[RoutingObservation]:
    recorder = _Recorder()
    CompletionsLogger.__new__(CompletionsLogger).record_routing_observation(
        recorder,
        "test-model",
        routing,
        request_id="req-1",
        ttft_ms=None,
        total_latency_ms=10.0,
        prompt_tokens=0,
        completion_tokens=0,
        **kwargs,
    )
    return recorder.observations


@pytest.mark.unit
def test_every_observation_carries_its_own_attempts_status():
    routing = {
        "endpoint_id": "m:c",
        "failed_attempts": [
            {"endpoint_id": "m:a", "error_type": "E", "error": "a", "status_code": 503},
            {"endpoint_id": "m:b", "error_type": "E", "error": "b"},
        ],
    }

    observations = _record(routing, success=False, status_code=400)

    assert [(o.endpoint_id, o.terminal, o.status_code) for o in observations] == [
        ("m:a", False, 503),
        ("m:b", False, None),
        ("m:c", True, 400),
    ]


@pytest.mark.unit
def test_a_success_carries_no_status():
    (observation,) = _record({"endpoint_id": "m:a"}, success=True, status_code=400)

    assert observation.success is True
    assert observation.status_code is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["400", True, 99, 600, 4.0])
def test_a_malformed_recorded_status_reads_as_none(raw):
    routing = {
        "endpoint_id": "m:c",
        "failed_attempts": [{"endpoint_id": "m:a", "error_type": "E", "status_code": raw}],
    }

    first, _terminal = _record(routing, success=False)

    assert first.status_code is None
