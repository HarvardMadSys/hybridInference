"""One caller cannot open a circuit that is serving everybody else.

Regression for a production report. Every caller of ``deepseek-v4-flash`` got

    AllCircuitsOpenError: No admissible route for model deepseek-v4-flash:
    deepseek-v4-flash:local-8003=circuit_open, deepseek-v4-flash:deepseek-api=weight_0,
    deepseek-v4-flash:ollama-api=weight_0, deepseek-v4-flash:staging-api=weight_0

while one caller kept sending requests the model's endpoint failed on. With the
remote routes at weight 0 the local endpoint was the model's only live route, so
three failures from that one caller opened its circuit and took the model away
from everyone; and the same caller's next retry took the half-open probe, failed
it, and opened the circuit again.

A breaker cannot tell from one failure whether the endpoint broke or the request
did, but it can tell callers apart: an endpoint that keeps serving other callers
while it keeps failing one has said something about that caller.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routing import endpoint_health
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.observability.alerts import reset_dedupe_state, reset_transition_state
from serving.utils import context as req_ctx
from serving.utils.logging import _STRUCTURED_LOG_KEYS, JsonFormatter

_MODEL = "deepseek-v4-flash"
_LOCAL = f"{_MODEL}:local-8003"
_MESSAGES = [{"role": "user", "content": "hello"}]


class _StatusError(Exception):
    """Adapter exception carrying an upstream HTTP status (aiohttp uses .status)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _Clock:
    """The monotonic clock caller attribution reads, advanced by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped thresholds, spelled out so a changed default cannot hide here."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    reset_transition_state()
    reset_dedupe_state()
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        yield
    reset_transition_state()
    reset_dedupe_state()
    for task in list(endpoint_health._ALERT_TASKS):
        task.cancel()
    endpoint_health._ALERT_TASKS.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(endpoint_health, "_monotonic", fake)
    return fake


def _as(caller: str | None):
    """Run as ``caller``: the identity every inference surface publishes."""
    if caller is None:
        return req_ctx.push(affinity_key=None, user_id=None, user_name=None)
    return req_ctx.push(affinity_key=f"hash-of-{caller}-key", user_id=caller, user_name=caller)


def _key(caller: str) -> str:
    """The opaque identity the breaker files ``caller``'s failures under."""
    with _as(caller):
        key = endpoint_health._caller_key()
    assert key is not None
    return key


def _fail(registry: EndpointHealthRegistry, caller: str | None, status: int = 500) -> None:
    with _as(caller):
        registry.record_failure(_LOCAL, reason="chat_exception", exc=_StatusError(status))


def _serve(registry: EndpointHealthRegistry, caller: str | None) -> None:
    with _as(caller):
        registry.record_success(_LOCAL)


def _state(registry: EndpointHealthRegistry) -> str:
    return registry.snapshot()[_LOCAL]["circuit_state"]


def _skips(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "caller_repeat_skip_breaker"]


# ---------------------------------------------------------------------------
# The reported outage, through the router
# ---------------------------------------------------------------------------


class _Engine(BaseAdapter):
    """Serves every caller but ``mallory``, whose request it fails with a 500."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        if req_ctx.get().get("user_id") == "mallory":
            raise _StatusError(500)
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any):
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _deepseek_router() -> FixedRouter:
    """The reported route: one live local endpoint, three remote ones at weight 0."""

    def adapter(endpoint_id: str, base_url: str) -> _Engine:
        return _Engine(
            ModelConfig(
                id=_MODEL,
                name=_MODEL,
                provider="sglang",
                base_url=base_url,
                endpoint_id=endpoint_id,
                pricing={"prompt": "0", "completion": "0"},
            )
        )

    router = FixedRouter(health_registry=EndpointHealthRegistry())
    router.register_route(
        _MODEL,
        [
            (adapter(_LOCAL, "http://localhost:8003/v1"), 1.0),
            (adapter(f"{_MODEL}:deepseek-api", "https://api.deepseek.example/v1"), 0.0),
            (adapter(f"{_MODEL}:ollama-api", "https://ollama.example/v1"), 0.0),
            (adapter(f"{_MODEL}:staging-api", "https://staging.example/v1"), 0.0),
        ],
    )
    return router


async def _chat(router: FixedRouter, caller: str) -> dict:
    with _as(caller):
        return await router.chat_completion(_MODEL, _MESSAGES)


async def test_one_callers_retries_no_longer_take_the_model_from_everyone(clock: _Clock) -> None:
    router = _deepseek_router()
    await _chat(router, "alice")

    for _ in range(5):
        clock.now += 1
        with pytest.raises(_StatusError):
            await _chat(router, "mallory")

    # The model stays up for everyone else.
    assert (await _chat(router, "alice"))["choices"][0]["message"]["content"] == "ok"
    assert (await _chat(router, "bob"))["choices"][0]["message"]["content"] == "ok"
    assert _state(router.endpoint_health_registry) == _CircuitState.CLOSED


async def test_with_the_window_off_one_caller_mutes_the_model_as_reported(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """``CIRCUIT_CALLER_WINDOW_SECONDS=0`` is the old breaker, and the reported outage."""
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")
    router = _deepseek_router()
    await _chat(router, "alice")

    for _ in range(3):
        with pytest.raises(_StatusError):
            await _chat(router, "mallory")

    with pytest.raises(AllCircuitsOpenError) as refused:
        await _chat(router, "alice")
    assert str(refused.value) == (
        f"No admissible route for model {_MODEL}: {_LOCAL}=circuit_open, "
        f"{_MODEL}:deepseek-api=weight_0, {_MODEL}:ollama-api=weight_0, "
        f"{_MODEL}:staging-api=weight_0"
    )


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def test_repeat_failures_leave_the_circuit_and_availability_alone(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(10):
            clock.now += 1
            _fail(registry, "mallory")

    snapshot = registry.snapshot()[_LOCAL]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    # Only the first failure was evidence about the endpoint.
    assert snapshot["availability"] == pytest.approx(0.9)
    assert registry.allow_request(_LOCAL) is True
    skips = _skips(caplog)
    assert len(skips) == 9
    assert {r.caller for r in skips} == {_key("mallory")}
    assert {r.status for r in skips} == {500}


def test_several_callers_failing_in_a_row_still_open_the_circuit(clock: _Clock) -> None:
    """An outage on a busy endpoint trips as fast as it always did."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    _fail(registry, "bob")
    _fail(registry, "carol")
    assert _state(registry) == _CircuitState.CLOSED
    _fail(registry, "dave")

    assert _state(registry) == _CircuitState.OPEN


def test_a_lone_caller_still_opens_the_circuit_when_nobody_else_is_served(clock: _Clock) -> None:
    registry = EndpointHealthRegistry()

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_a_lone_callers_failures_count_again_once_nobody_else_has_been_served(
    clock: _Clock,
) -> None:
    """An endpoint with one active caller is detected at most a window later."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    clock.now += 1
    _fail(registry, "mallory")  # counted
    clock.now += 1
    _fail(registry, "mallory")  # alice was served a second ago: about mallory
    assert registry._circuits[_LOCAL].consecutive_failures == 1

    clock.now += 59  # 61s since alice was served, 60s since mallory's counted failure
    _fail(registry, "mallory")
    clock.now += 1
    _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_a_caller_counts_at_most_once_a_window_while_others_are_served(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = EndpointHealthRegistry()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for second in range(180):
            if second % 10 == 0:
                _serve(registry, "alice")
            _fail(registry, "mallory")
            clock.now += 1

    assert _state(registry) == _CircuitState.CLOSED
    # Counted at 0s, 60s and 120s: the endpoint still hears, once a window,
    # that it is failing someone.
    assert len(_skips(caplog)) == 180 - 3


def test_being_served_rearms_a_callers_failures(clock: _Clock) -> None:
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    _fail(registry, "mallory")
    _serve(registry, "mallory")
    _fail(registry, "mallory")

    # The success reset the streak; the failure after it counted again.
    assert registry._circuits[_LOCAL].consecutive_failures == 1
    assert registry.snapshot()[_LOCAL]["availability"] < 0.9


def test_a_success_from_the_same_caller_is_not_somebody_else(clock: _Clock) -> None:
    """Only other callers being served says the endpoint works."""
    registry = EndpointHealthRegistry()
    _serve(registry, "mallory")

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_failures_with_no_caller_always_count(clock: _Clock) -> None:
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    for _ in range(3):
        _fail(registry, None)

    assert _state(registry) == _CircuitState.OPEN


def test_credential_rejections_always_count(clock: _Clock) -> None:
    """A 401 rejects the gateway's own key, so it is never the caller's to own."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    for _ in range(3):
        _fail(registry, "mallory", status=401)

    assert _state(registry) == _CircuitState.OPEN


def test_client_errors_are_still_exempt_before_caller_attribution(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(5):
            _fail(registry, "mallory", status=400)

    assert registry.snapshot()[_LOCAL]["availability"] == 1.0
    assert not _skips(caplog)
    # A later real fault from the same caller is its first counted one.
    _fail(registry, "mallory")
    assert registry._circuits[_LOCAL].consecutive_failures == 1


def test_the_same_callers_retry_cannot_hold_the_circuit_open(clock: _Clock) -> None:
    """A failed half-open probe reopens the circuit only if it is the endpoint's failure.

    The second half of the report: once open, the circuit admits one probe after
    the cooldown, and a caller retrying in a loop is the likeliest to take it.
    Its failure reopened the circuit for another cooldown, every cooldown.
    """
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")
    _fail(registry, "mallory")
    _fail(registry, "bob")
    _fail(registry, "carol")
    assert _state(registry) == _CircuitState.OPEN

    circuit = registry._circuits[_LOCAL]
    circuit.last_opened -= circuit.cooldown_seconds + 1
    clock.now += circuit.cooldown_seconds + 1

    with _as("mallory"):
        claim = registry.begin_dispatch(_LOCAL)
        assert claim is not None
        registry.record_failure(_LOCAL, reason="chat_exception", exc=_StatusError(500))
        registry.end_dispatch(claim)
    assert _state(registry) == _CircuitState.HALF_OPEN

    with _as("alice"):
        claim = registry.begin_dispatch(_LOCAL)
        assert claim is not None
        registry.record_success(_LOCAL)
        registry.end_dispatch(claim)
    assert _state(registry) == _CircuitState.CLOSED


def test_window_zero_counts_every_failure(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    for _ in range(3):
        _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_charged_callers_are_bounded(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setattr(endpoint_health, "_MAX_CHARGED_CALLERS", 4)
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1000")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0")
    registry = EndpointHealthRegistry()

    for n in range(10):
        _fail(registry, f"caller-{n}")
    assert len(registry._circuits[_LOCAL]._charged_callers) == 4

    clock.now += 61
    _fail(registry, "late")
    # Every earlier entry has aged out of the window.
    assert list(registry._circuits[_LOCAL]._charged_callers) == [_key("late")]


def test_the_skipped_caller_reaches_production_logs(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Both formatters emit only allowlisted keys; the skip says nothing without its caller."""
    assert "caller" in _STRUCTURED_LOG_KEYS
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        _fail(registry, "mallory")
        _fail(registry, "mallory")

    (record,) = _skips(caplog)
    rendered = json.loads(JsonFormatter().format(record))
    assert rendered["event"] == "caller_repeat_skip_breaker"
    assert rendered["endpoint_id"] == _LOCAL
    assert rendered["caller"] == _key("mallory")
    assert rendered["status"] == 500
