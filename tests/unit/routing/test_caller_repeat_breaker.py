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
did, but it can tell callers apart: an endpoint that served other callers after
failing one, and then fails that one again, has said something about that
caller. A success from *before* the failure says nothing of the kind -- an
outage that starts right after it serves nobody -- so it excuses nothing.

That left the quiet endpoint: with nobody else being served, the same caller's
three retries still opened the circuit. Nor, then, can one caller open a circuit
on its own: its failures count, and run up the streak, but the circuit opens
only once a failure from somebody else joins them -- or one no request could
have caused, such as a refused connection.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey

from routing import endpoint_health
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.exceptions import UpstreamStreamIdleError
from serving.observability.alerts import reset_dedupe_state, reset_transition_state
from serving.utils import context as req_ctx
from serving.utils.logging import _STRUCTURED_LOG_KEYS, JsonFormatter

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """The reported shape: one caller retrying while the model goes on serving others.

    The old breaker never saw three of mallory's failures in a row here -- every
    success in between reset the streak -- but its availability floor did: the
    fifth retry took the average under 0.7 and opened the circuit for everyone.
    """
    router = _deepseek_router()

    for _ in range(8):
        assert (await _chat(router, "alice"))["choices"][0]["message"]["content"] == "ok"
        clock.now += 1
        with pytest.raises(_StatusError):
            await _chat(router, "mallory")
        clock.now += 1

    assert (await _chat(router, "bob"))["choices"][0]["message"]["content"] == "ok"
    assert _state(router.endpoint_health_registry) == _CircuitState.CLOSED


async def test_with_the_window_off_one_caller_mutes_the_model_as_reported(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """``CIRCUIT_CALLER_WINDOW_SECONDS=0`` is the old breaker, and the reported outage."""
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")
    router = _deepseek_router()

    with pytest.raises(AllCircuitsOpenError) as refused:
        for _ in range(8):
            await _chat(router, "alice")
            with pytest.raises(_StatusError):
                await _chat(router, "mallory")
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
    # Somebody else's failure, so that mallory's first is not one caller's alone
    # (which stays out of availability too; see the lone-caller tests below).
    _fail(registry, "bob")

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(10):
            clock.now += 1
            _fail(registry, "mallory")
            clock.now += 1
            _serve(registry, "alice")

    snapshot = registry.snapshot()[_LOCAL]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    # Only mallory's first failure reached availability; the ten successes after it did.
    assert snapshot["availability"] == pytest.approx(1 - 0.1 * 0.9**10)
    skips = _skips(caplog)
    assert len(skips) == 9
    assert {r.caller for r in skips} == {_key("mallory")}
    assert {r.status for r in skips} == {500}


def test_a_success_from_before_a_callers_failure_excuses_nothing(clock: _Clock) -> None:
    """A success says the endpoint was up then, not that it is up now."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    # Every one counted. Being one caller's alone, they open the circuit only
    # once somebody else's failure joins them.
    assert registry._circuits[_LOCAL].consecutive_failures == 3
    _fail(registry, "bob")
    assert _state(registry) == _CircuitState.OPEN


def test_a_success_stamped_with_the_same_instant_excuses_nothing(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """A tie cannot be told apart from a success just before the failure."""
    registry = EndpointHealthRegistry()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(3):
            _fail(registry, "mallory")
            _serve(registry, "alice")

    # Each success reset the streak, so read what was skipped instead: nothing.
    assert not _skips(caplog)


def test_an_outage_seen_by_two_callers_opens_the_circuit_at_once(clock: _Clock) -> None:
    """Fewer distinct callers than the threshold still trip on their repeats."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    clock.now += 1
    _fail(registry, "bob")
    _fail(registry, "carol")
    assert _state(registry) == _CircuitState.CLOSED
    clock.now += 1
    _fail(registry, "bob")

    assert _state(registry) == _CircuitState.OPEN


def test_several_callers_failing_in_a_row_still_open_the_circuit(clock: _Clock) -> None:
    """An outage on a busy endpoint trips as fast as it always did."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")

    _fail(registry, "bob")
    _fail(registry, "carol")
    assert _state(registry) == _CircuitState.CLOSED
    _fail(registry, "dave")

    assert _state(registry) == _CircuitState.OPEN


def test_a_caller_counts_at_most_once_a_window_while_others_are_served(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = EndpointHealthRegistry()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _second in range(180):
            _fail(registry, "mallory")
            clock.now += 0.5
            _serve(registry, "alice")
            clock.now += 0.5

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


def test_a_success_from_the_same_caller_is_not_somebody_else(clock: _Clock) -> None:
    """Only other callers being served says the endpoint works."""
    registry = EndpointHealthRegistry()
    _serve(registry, "mallory")

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    # None was skipped as a repeat.
    assert registry._circuits[_LOCAL].consecutive_failures == 3


def test_failures_with_no_caller_always_count(clock: _Clock) -> None:
    registry = EndpointHealthRegistry()

    for _ in range(3):
        _fail(registry, None)
        _serve(registry, "alice")
    assert registry._circuits[_LOCAL].consecutive_failures == 0
    for _ in range(3):
        _fail(registry, None)

    assert _state(registry) == _CircuitState.OPEN


def test_credential_rejections_always_count(clock: _Clock) -> None:
    """A 401 rejects the gateway's own key, so it is never the caller's to own."""
    registry = EndpointHealthRegistry()

    for _ in range(3):
        _serve(registry, "alice")
        _fail(registry, "mallory", status=401)
    for _ in range(2):
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


# ---------------------------------------------------------------------------
# A caller alone
# ---------------------------------------------------------------------------


def _holds(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "lone_caller_hold_breaker"]


def _refused() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("localhost", 8003, False, True, None, None, None, None)
    return aiohttp.ClientConnectorError(key, ConnectionRefusedError(111, "Connection refused"))


async def test_a_lone_callers_retries_cannot_take_a_quiet_model_from_everyone(
    clock: _Clock,
) -> None:
    """The reported shape with nobody else being served while it happens.

    No success came between mallory's retries, so each one counted, and the
    third used to open the circuit: whoever came next met ``AllCircuitsOpenError``.
    """
    router = _deepseek_router()

    for _ in range(10):
        # The engine's own failure every time, never a refusal at the router.
        with pytest.raises(_StatusError):
            await _chat(router, "mallory")
        clock.now += 5

    assert _state(router.endpoint_health_registry) == _CircuitState.CLOSED
    assert (await _chat(router, "alice"))["choices"][0]["message"]["content"] == "ok"


async def test_with_the_window_off_a_lone_caller_opens_the_circuit_as_before(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")
    router = _deepseek_router()

    for _ in range(3):
        with pytest.raises(_StatusError):
            await _chat(router, "mallory")

    with pytest.raises(AllCircuitsOpenError):
        await _chat(router, "alice")


def test_a_lone_caller_runs_up_the_streak_but_opens_nothing(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = EndpointHealthRegistry()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(5):
            clock.now += 1
            _fail(registry, "mallory")

    snapshot = registry.snapshot()[_LOCAL]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    # Kept out of availability too, or its floor would open the circuit instead.
    assert snapshot["availability"] == 1.0
    assert registry._circuits[_LOCAL].consecutive_failures == 5
    holds = _holds(caplog)
    assert [r.consecutive_failures for r in holds] == [1, 2, 3, 4, 5]
    assert {r.caller for r in holds} == {_key("mallory")}
    assert not _skips(caplog)


def test_the_first_failure_from_anyone_else_opens_the_circuit_at_once(clock: _Clock) -> None:
    """An endpoint that fails everyone who tries is down, however few try."""
    registry = EndpointHealthRegistry()
    for _ in range(5):
        clock.now += 1
        _fail(registry, "mallory")

    clock.now += 1
    _fail(registry, "bob")

    assert _state(registry) == _CircuitState.OPEN
    # The page names the whole run, the held failures included.
    assert dict(registry._circuits[_LOCAL]._affected_callers) == {
        "mallory (mallory)": 5,
        "bob (bob)": 1,
    }


def test_a_failure_with_no_caller_joins_a_lone_callers_run(clock: _Clock) -> None:
    registry = EndpointHealthRegistry()
    _fail(registry, "mallory")
    _fail(registry, "mallory")
    assert _state(registry) == _CircuitState.CLOSED

    _fail(registry, None)

    assert _state(registry) == _CircuitState.OPEN


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_refused, id="connection-refused"),
        pytest.param(lambda: ConnectionRefusedError(111, "Connection refused"), id="refused-raw"),
        # A black-holed host, past ``serving.http.connect_bounded``.
        pytest.param(
            lambda: aiohttp.ConnectionTimeoutError("Connection timeout to host"),
            id="connect-timeout",
        ),
        pytest.param(lambda: _StatusError(429), id="rate-limited"),
        pytest.param(lambda: _StatusError(401), id="credential-rejected"),
    ],
)
def test_a_failure_no_request_could_cause_opens_the_circuit_for_a_lone_caller(
    clock: _Clock, error: Callable[[], BaseException]
) -> None:
    """An engine that is down still leaves rotation, whoever is calling."""
    registry = EndpointHealthRegistry()

    for _ in range(3):
        clock.now += 1
        with _as("mallory"):
            registry.record_failure(_LOCAL, reason="chat_exception", exc=error())

    assert _state(registry) == _CircuitState.OPEN


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: _StatusError(500), id="engine-5xx"),
        pytest.param(lambda: aiohttp.ServerDisconnectedError(), id="dropped"),
        # Sent, then longer than the timeout: a long generation outruns it too.
        pytest.param(TimeoutError, id="timeout"),
        pytest.param(lambda: UpstreamStreamIdleError(120, frames=0), id="stream-idle"),
    ],
)
def test_a_failure_the_request_may_have_caused_is_held_for_a_lone_caller(
    clock: _Clock, error: Callable[[], BaseException]
) -> None:
    """Only a failure no request could cause speaks for the endpoint on its own."""
    registry = EndpointHealthRegistry()

    for _ in range(3):
        clock.now += 1
        with _as("mallory"):
            registry.record_failure(_LOCAL, reason="chat_exception", exc=error())

    assert _state(registry) == _CircuitState.CLOSED


def test_a_lone_callers_run_leaves_the_endpoint_no_easier_to_open(clock: _Clock) -> None:
    """Out of availability, the run leaves no hair trigger behind it.

    Had its ten failures reached availability they would have sunk it to about
    0.35, and after one success anybody's single failure would have taken it back
    under the floor and opened the circuit.
    """
    registry = EndpointHealthRegistry()
    for _ in range(10):
        clock.now += 1
        _fail(registry, "mallory")
    clock.now += 1
    _serve(registry, "alice")

    clock.now += 1
    _fail(registry, "bob")

    assert _state(registry) == _CircuitState.CLOSED


def test_another_callers_failure_within_the_window_stands_behind_a_lone_caller(
    clock: _Clock,
) -> None:
    """An endpoint failing a share of everyone's requests is failing, success or not."""
    registry = EndpointHealthRegistry()
    _fail(registry, "bob")
    clock.now += 1
    _serve(registry, "alice")

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_a_failure_before_both_the_last_success_and_the_window_stands_behind_nothing(
    clock: _Clock,
) -> None:
    registry = EndpointHealthRegistry()
    _fail(registry, "bob")
    clock.now += 1
    _serve(registry, "alice")
    clock.now += 60

    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.CLOSED


def test_a_failure_in_the_same_run_stands_behind_a_lone_caller_however_old(
    clock: _Clock,
) -> None:
    """With nobody served since, an hour-old failure belongs to the same outage."""
    registry = EndpointHealthRegistry()
    _fail(registry, "bob")
    clock.now += 3600

    _fail(registry, "mallory")
    assert _state(registry) == _CircuitState.CLOSED
    _fail(registry, "mallory")

    assert _state(registry) == _CircuitState.OPEN


def test_the_held_caller_reaches_production_logs(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Both formatters emit only allowlisted keys; the hold needs its caller and run."""
    registry = EndpointHealthRegistry()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        _fail(registry, "mallory")

    (record,) = _holds(caplog)
    rendered = json.loads(JsonFormatter().format(record))
    assert rendered["event"] == "lone_caller_hold_breaker"
    assert rendered["endpoint_id"] == _LOCAL
    assert rendered["caller"] == _key("mallory")
    assert rendered["status"] == 500
    assert rendered["consecutive_failures"] == 1
    assert rendered["threshold"] == 3


# ---------------------------------------------------------------------------
# The half-open probe
# ---------------------------------------------------------------------------


def _elapse_cooldown(registry: EndpointHealthRegistry, clock: _Clock) -> None:
    circuit = registry._circuits[_LOCAL]
    circuit.last_opened -= circuit.cooldown_seconds + 1
    clock.now += circuit.cooldown_seconds + 1


def _probe(registry: EndpointHealthRegistry, caller: str, *, fails: bool) -> None:
    with _as(caller):
        claim = registry.begin_dispatch(_LOCAL)
        assert claim is not None
        if fails:
            registry.record_failure(_LOCAL, reason="chat_exception", exc=_StatusError(500))
        else:
            registry.record_success(_LOCAL)
        registry.end_dispatch(claim)


def test_a_failed_probe_during_an_outage_reopens_the_circuit(clock: _Clock) -> None:
    """Nobody has been served since the trip, so the probe's failure is the endpoint's."""
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")
    clock.now += 1
    _fail(registry, "bob")
    _fail(registry, "carol")
    _fail(registry, "dave")
    assert _state(registry) == _CircuitState.OPEN

    _elapse_cooldown(registry, clock)
    _probe(registry, "bob", fails=True)

    assert _state(registry) == _CircuitState.OPEN


def test_the_same_callers_retry_cannot_hold_the_circuit_open(clock: _Clock) -> None:
    """A failed probe reopens the circuit only if it is the endpoint's failure.

    The second half of the report: once open, the circuit admits one probe after
    the cooldown, and a caller retrying in a loop is the likeliest to take it.
    When that caller is one the endpoint had served others around -- mallory,
    failing while alice was served -- its failed probe says nothing about the
    endpoint, and the next caller's probe decides.
    """
    registry = EndpointHealthRegistry()
    _serve(registry, "alice")
    clock.now += 1
    _fail(registry, "mallory")
    clock.now += 1
    _serve(registry, "alice")
    clock.now += 1
    _fail(registry, "bob")
    _fail(registry, "carol")
    _fail(registry, "dave")
    assert _state(registry) == _CircuitState.OPEN

    _elapse_cooldown(registry, clock)
    _probe(registry, "mallory", fails=True)
    assert _state(registry) == _CircuitState.HALF_OPEN

    _probe(registry, "alice", fails=False)
    assert _state(registry) == _CircuitState.CLOSED


def test_a_lone_callers_failed_probe_leaves_the_circuit_half_open(clock: _Clock) -> None:
    """What one caller could not open, it cannot hold open either.

    Bob's failure came before alice was served, so it stood behind mallory's
    failures only while it was inside the window -- long enough to open the
    circuit. By the probe it is older than that, and mallory's failed probe
    stands alone: no reopening, so no new cooldown, and the next probe decides.
    """
    registry = EndpointHealthRegistry()
    _fail(registry, "bob")
    clock.now += 1
    _serve(registry, "alice")
    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")
    assert _state(registry) == _CircuitState.OPEN

    clock.now += 60
    _elapse_cooldown(registry, clock)
    _probe(registry, "mallory", fails=True)
    assert _state(registry) == _CircuitState.HALF_OPEN

    _probe(registry, "alice", fails=False)
    assert _state(registry) == _CircuitState.CLOSED


def test_a_failed_probe_with_somebody_else_behind_it_reopens_the_circuit(clock: _Clock) -> None:
    """The same probe, while bob's failure is still in the window, reopens it."""
    registry = EndpointHealthRegistry()
    _fail(registry, "bob")
    clock.now += 1
    _serve(registry, "alice")
    for _ in range(3):
        clock.now += 1
        _fail(registry, "mallory")

    _elapse_cooldown(registry, clock)
    _probe(registry, "mallory", fails=True)

    assert _state(registry) == _CircuitState.OPEN


# ---------------------------------------------------------------------------
# Configuration, bounds and logs
# ---------------------------------------------------------------------------


def test_window_zero_counts_every_failure(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")
    registry = EndpointHealthRegistry()

    for _ in range(4):
        _serve(registry, "alice")
        _fail(registry, "mallory")

    # Every failure reached availability: 1.0 -> 0.9 -> 0.91 -> 0.819 -> ...
    assert registry.snapshot()[_LOCAL]["availability"] < 0.76


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

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        _fail(registry, "mallory")
        clock.now += 1
        _serve(registry, "alice")
        clock.now += 1
        _fail(registry, "mallory")

    (record,) = _skips(caplog)
    rendered = json.loads(JsonFormatter().format(record))
    assert rendered["event"] == "caller_repeat_skip_breaker"
    assert rendered["endpoint_id"] == _LOCAL
    assert rendered["caller"] == _key("mallory")
    assert rendered["status"] == 500
