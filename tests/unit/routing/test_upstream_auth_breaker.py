"""An upstream rejecting the gateway's own credential must be loud.

Regression for a production outage: a local inference proxy behind
``diffusiongemma`` was configured with the wrong ``LOCAL_API_KEY`` and answered
HTTP 401 to 100% of requests for about an hour. Nothing alerted. The 401 landed
in the client-error exemption — "the upstream is healthy and correctly rejected
the USER's bad request" — whose premise cannot hold for auth: the caller never
supplies the upstream credential (client auth is validated by
``serving.servers.auth`` *before* routing), so by the time an adapter runs the
only credential in play is the one this deployment configured. Skipping the
breaker also skipped availability accounting and the circuit-open alert, leaving
one INFO line per request as the sole artifact.
"""

import asyncio
import inspect
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routing import endpoint_health
from routing.endpoint_health import (
    _AUTH_ALERT_SCHEDULE_INTERVAL_SEC,
    EndpointHealthRegistry,
    _CircuitBreaker,
    _CircuitState,
    _is_auth_misconfig,
    _is_client_error,
)
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.key_pool import KeyPoolExhausted
from serving.observability.alerts import (
    _STATE_TRANSITIONS,
    AlertSeverity,
    reset_transition_state,
)
from serving.utils.logging import JsonFormatter

# The endpoint id from the real outage: ``registry._make_provider_id`` stamps
# ``:local-<port>`` for any base_url whose host is in its ``_LOCAL_HOSTS`` set.
_LOCAL_ENDPOINT = "diffusiongemma:local-8002"
_REMOTE_ENDPOINT = "glm-4.6:zai-api"


class _StatusError(Exception):
    """Adapter exception carrying an upstream HTTP status (aiohttp uses .status)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@pytest.fixture(autouse=True)
def _clean_alert_state(monkeypatch):
    """Alert dedupe/breach state is process-global and would leak between tests."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_transition_state()
    reset_dedupe_state()
    yield
    reset_transition_state()
    reset_dedupe_state()
    # ``_ALERT_TASKS`` is process-global too, and every test here schedules into
    # it. A task still pending when this test's event loop is discarded would
    # otherwise stay in the set holding a dead loop, and the next test that reads
    # the set — ``test_circuit_breaker_usage_limit._drain_alert_tasks`` gathers it
    # — dies on "All futures must share the same event loop".
    for task in list(endpoint_health._ALERT_TASKS):
        task.cancel()
    endpoint_health._ALERT_TASKS.clear()


def _auth_alert_key(endpoint_id: str) -> str:
    return f"upstream_auth:{endpoint_id}"


def _auth_alerts(mock_alert, endpoint_id: str) -> list:
    """Return only the upstream-auth pages, ignoring resolutions and other keys."""
    return [
        call
        for call in mock_alert.await_args_list
        if call.kwargs.get("dedupe_key") == _auth_alert_key(endpoint_id)
        and call.kwargs.get("status", "firing") == "firing"
    ]


def _auth_resolutions(mock_alert, endpoint_id: str) -> list:
    """Return only the resolutions that close the upstream-auth incident."""
    return [
        call
        for call in mock_alert.await_args_list
        if call.kwargs.get("dedupe_key") == _auth_alert_key(endpoint_id)
        and call.kwargs.get("status") == "resolved"
    ]


async def _drain_alerts() -> None:
    """Let the fire-and-forget alert tasks run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_auth_statuses_are_never_client_errors():
    """401/407 reject the gateway's credential at every provider, local or not."""
    for code in (401, 407):
        assert _is_auth_misconfig(code) is True, code
        assert _is_client_error(_StatusError(code)) is False, code


def test_403_is_never_classified_as_a_credential_rejection():
    """403 is never escalated, whatever the endpoint identifier looks like.

    Remote providers overload 403 for per-request rejections — content policy,
    safety blocks, region/IP restrictions — where the upstream is healthy and
    correctly refused one prompt. Escalating those would reinstate the "one user's
    bad request opens the circuit for everyone" cascade, so escalation would have
    to be scoped to endpoints the deployment owns — and ``endpoint_id`` cannot
    carry that fact in either direction:

    - ``registry._make_provider_id`` mints ``:local-<port>`` only for its four
      ``_LOCAL_HOSTS`` entries, so a gateway-owned box on the LAN
      (``http://10.0.0.5:8000``) becomes ``:10-api`` and reads as remote;
    - the identifier is not authoritative anyway — an admin runtime route's
      ``route_id`` becomes the ``endpoint_id`` verbatim, so a remote route could
      present any ``:local-<port>`` suffix it liked.

    So classification stays on the two statuses that need no ownership signal —
    structurally, by taking no endpoint at all. An endpoint answering 403 to
    everything is still caught: 403 is key-specific for ``KeyPool``, so its keys
    mute and the follow-on ``KeyPoolExhausted`` carries no status, is not exempt,
    and trips the breaker's own page.
    """
    assert _is_auth_misconfig(403) is False
    assert _is_client_error(_StatusError(403)) is True


def test_ordinary_client_errors_are_not_auth_misconfig():
    for code in (400, 402, 403, 404, 413, 422):
        assert _is_auth_misconfig(code) is False, code
    # Failures with no HTTP status at all (timeout / connection reset).
    assert _is_auth_misconfig(None) is False


# ---------------------------------------------------------------------------
# Breaker + availability accounting
# ---------------------------------------------------------------------------


async def test_local_401_records_unavailability_and_trips_breaker(monkeypatch):
    """The outage signal the gateway used to throw away."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    registry.record_success(_LOCAL_ENDPOINT)
    baseline = registry.snapshot()[_LOCAL_ENDPOINT]["availability"]

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        after_one = registry.snapshot()[_LOCAL_ENDPOINT]
        assert after_one["availability"] < baseline
        assert after_one["consecutive_auth_rejections"] == 1
        assert after_one["last_error_status"] == 401
        assert after_one["circuit_state"] == _CircuitState.CLOSED

        for _ in range(2):
            registry.record_failure(
                _LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401)
            )
        await asyncio.sleep(0)

    final = registry.snapshot()[_LOCAL_ENDPOINT]
    assert final["circuit_state"] == _CircuitState.OPEN
    assert final["consecutive_auth_rejections"] == 3
    # An open circuit is what makes the router stop advertising the endpoint.
    assert registry.allow_request(_LOCAL_ENDPOINT) is False


async def test_ordinary_client_error_still_skips_the_breaker(monkeypatch, caplog):
    """No regression: a user's own bad request stays exempt and stays quiet."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    registry.record_success(_LOCAL_ENDPOINT)
    baseline = registry.snapshot()[_LOCAL_ENDPOINT]

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        for code in (400, 422):
            for _ in range(5):
                registry.record_failure(
                    _LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(code)
                )
        await asyncio.sleep(0)

    after = registry.snapshot()[_LOCAL_ENDPOINT]
    assert after["circuit_state"] == _CircuitState.CLOSED
    assert after["availability"] == baseline["availability"]
    # The skip path leaves health accounting untouched entirely.
    assert after["consecutive_auth_rejections"] == 0
    assert after["last_error_status"] is None

    assert [r for r in caplog.records if r.getMessage() == "client_error_skip_breaker"]
    assert not [r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]
    assert mock_alert.await_count == 0


@pytest.mark.parametrize("endpoint_id", (_REMOTE_ENDPOINT, _LOCAL_ENDPOINT))
async def test_403_still_skips_the_breaker_on_any_endpoint(monkeypatch, endpoint_id):
    """A content-policy 403 must not open the circuit — on any endpoint.

    Including one whose identifier carries the ``:local-<port>`` suffix, since a
    remote runtime route can present exactly that (the admin ``route_id`` becomes
    the ``endpoint_id`` verbatim) and reading it as an ownership claim is what
    would let one refused prompt open the shared circuit for everyone.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    registry.record_success(endpoint_id)
    baseline = registry.snapshot()[endpoint_id]["availability"]

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        for _ in range(5):
            registry.record_failure(endpoint_id, reason="chat_exception", exc=_StatusError(403))
        await _drain_alerts()

    status = registry.snapshot()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.CLOSED
    assert status["availability"] == baseline
    assert status["consecutive_auth_rejections"] == 0
    assert mock_alert.await_count == 0


async def test_a_success_clears_the_auth_rejection_run(monkeypatch):
    """Recovery must clear the degraded signal /health/deep reads."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        assert registry.snapshot()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 1

        registry.record_success(_LOCAL_ENDPOINT)
        assert registry.snapshot()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 0
        # ``last_error_status`` is a sticky diagnostic breadcrumb, not liveness.
        assert registry.snapshot()[_LOCAL_ENDPOINT]["last_error_status"] == 401

        # A failure of any other kind is not recovery — it is no evidence the
        # credential was accepted — so the run survives it.
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(502))
        await asyncio.sleep(0)

    status = registry.snapshot()[_LOCAL_ENDPOINT]
    assert status["consecutive_auth_rejections"] == 1
    assert status["last_error_status"] == 502


# ---------------------------------------------------------------------------
# Warning log + page
# ---------------------------------------------------------------------------


async def test_first_401_logs_a_warning_and_pages(monkeypatch, caplog):
    """One rejection is enough: there is no partial version of this failure."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        registry.record_failure(
            _LOCAL_ENDPOINT,
            reason="stream_exception",
            detail="HTTP 401 from upstream: invalid api key",
            exc=_StatusError(401),
        )
        await asyncio.sleep(0)

        auth_calls = _auth_alerts(mock_alert, _LOCAL_ENDPOINT)
        assert len(auth_calls) == 1
        severity, title, context = auth_calls[0].args
        assert severity is AlertSeverity.ERROR
        assert title == "Upstream rejected gateway credential"
        assert context["endpoint_id"] == _LOCAL_ENDPOINT
        assert context["status"] == 401
        assert context["consecutive_auth_rejections"] == 1
        assert context["upstream_error"] == "HTTP 401 from upstream: invalid api key"
        assert auth_calls[0].kwargs["cooldown_sec"] > 0

    # The old artifact was a single INFO line; this must be visible at WARNING.
    records = [r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.event == "upstream_auth_misconfig"
    assert record.endpoint_id == _LOCAL_ENDPOINT
    assert record.status == 401
    assert record.consecutive_auth_rejections == 1
    assert record.upstream_error == "HTTP 401 from upstream: invalid api key"
    assert not [r for r in caplog.records if r.getMessage() == "client_error_skip_breaker"]


async def test_the_warning_survives_json_serialization(monkeypatch, caplog):
    """The detail must reach production logs, not just the LogRecord.

    Both formatters emit only keys in ``_STRUCTURED_LOG_KEYS``, so a field set via
    ``extra=`` is silently dropped from JSON output unless it is whitelisted
    there. ``consecutive_auth_rejections`` is the field that says whether the
    rejection is a one-off or the run /health/deep degrades on, so asserting it
    only on the record would pass with the whitelist entry missing.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.WARNING, logger="routing.routers"),
    ):
        for _ in range(2):
            registry.record_failure(
                _LOCAL_ENDPOINT,
                reason="stream_exception",
                detail="HTTP 401 from upstream: invalid api key",
                exc=_StatusError(401),
            )
        await _drain_alerts()

    records = [r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]
    assert len(records) == 2

    payload = json.loads(JsonFormatter().format(records[-1]))
    assert payload["level"] == "WARNING"
    assert payload["event"] == "upstream_auth_misconfig"
    assert payload["endpoint_id"] == _LOCAL_ENDPOINT
    assert payload["status"] == 401
    assert payload["consecutive_auth_rejections"] == 2
    assert payload["upstream_error"] == "HTTP 401 from upstream: invalid api key"


# ---------------------------------------------------------------------------
# Incident lifecycle: the page must be able to close
# ---------------------------------------------------------------------------


async def test_the_page_opens_a_closable_incident(monkeypatch):
    """The page goes through the state tracker, not straight to the sink.

    A fire-only ``alert_slack`` would register nothing with ``_STATE_TRANSITIONS``,
    so no resolution edge could ever reach the fingerprint and — state alerts are
    never swept — the incident would stay open forever, holding principal quota
    until it is exhausted and real outages start being suppressed.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()

        assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 1
        assert not _auth_resolutions(mock_alert, _LOCAL_ENDPOINT)
        assert _STATE_TRANSITIONS.is_firing(key) is True


async def test_an_accepted_request_closes_the_auth_incident(monkeypatch, caplog):
    """The healthy edge the breaker already uses for ``circuit_open``.

    ``_ProviderHealth.record(True)`` resets the rejection run on the first
    accepted request — the endpoint just did the thing the page said no caller
    could make it do — so that is where the incident closes.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()

        registry.record_success(_LOCAL_ENDPOINT)
        await _drain_alerts()

        resolutions = _auth_resolutions(mock_alert, _LOCAL_ENDPOINT)
        assert len(resolutions) == 1
        assert resolutions[0].args[1] == "Recovered: Upstream rejected gateway credential"
        # Delivered, so the tracker must have let the incident go.
        assert _STATE_TRANSITIONS.is_firing(key) is False

        # Exactly once per outage: further successes are not transitions.
        registry.record_success(_LOCAL_ENDPOINT)
        await _drain_alerts()
        assert len(_auth_resolutions(mock_alert, _LOCAL_ENDPOINT)) == 1

        # Recovery re-arms the schedule throttle, so a fresh outage pages on its
        # first rejection instead of serving out a window the last one opened.
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()
        assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 2
        assert _STATE_TRANSITIONS.is_firing(key) is True

    assert [r for r in caplog.records if r.getMessage() == "upstream_auth_recovered"]


@pytest.mark.parametrize(
    ("label", "exc"),
    (
        # The steady state of a real all-keys-401 outage: 401 is a key-specific
        # status for ``KeyPool``, so the rejections mute every key and the next
        # request raises from ``acquire()`` before any upstream is contacted.
        ("key_pool_exhausted", KeyPoolExhausted("All 2 keys for provider 'x' are muted")),
        ("connection_reset", ConnectionResetError("Connection reset by peer")),
        ("timeout", asyncio.TimeoutError()),
    ),
)
async def test_a_statusless_failure_keeps_the_auth_incident_open(monkeypatch, label, exc):
    """A failure that never reached the upstream is not proof of recovery.

    Regression: any non-auth failure used to end the rejection run and resolve the
    incident. For a pooled endpoint that closed the page during the *worst* part
    of the outage — every key muted by the 401s means ``KeyPool.acquire`` raises
    ``KeyPoolExhausted``, which carries no HTTP status, on every subsequent
    request. Connection resets and timeouts have the same shape.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()
        assert _STATE_TRANSITIONS.is_firing(key) is True

        for _ in range(3):
            registry.record_failure(_LOCAL_ENDPOINT, reason=label, exc=exc)
        await _drain_alerts()

        assert not _auth_resolutions(mock_alert, _LOCAL_ENDPOINT), label
        assert _STATE_TRANSITIONS.is_firing(key) is True, label

    status = registry.snapshot()[_LOCAL_ENDPOINT]
    # The run is intact, so /health/deep keeps reporting the endpoint degraded.
    assert status["consecutive_auth_rejections"] == 1
    # No status to record, so the breadcrumb still names the real cause.
    assert status["last_error_status"] == 401


async def test_a_statused_non_auth_failure_keeps_the_auth_incident_open(monkeypatch):
    """A different HTTP status is no proof the credential was accepted either.

    Nothing here can distinguish an upstream that authenticated the request and
    then failed from a proxy or load balancer that answered before auth was ever
    evaluated, so a 5xx does not close the credential incident. What is wrong now
    is the breaker's ``circuit_open`` incident to report.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()
        assert _STATE_TRANSITIONS.is_firing(key) is True

        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(502))
        await _drain_alerts()

        assert not _auth_resolutions(mock_alert, _LOCAL_ENDPOINT)
        assert _STATE_TRANSITIONS.is_firing(key) is True

    status = registry.snapshot()[_LOCAL_ENDPOINT]
    assert status["consecutive_auth_rejections"] == 1
    assert status["last_error_status"] == 502


async def test_a_statusless_failure_never_opens_an_auth_incident(monkeypatch, caplog):
    """The inverse hazard: pool exhaustion on its own is not a credential fault.

    It carries no status, so it must not be classified as an auth rejection, page,
    or degrade /health/deep — the breaker's own availability and circuit signals
    cover it.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.WARNING, logger="routing.routers"),
    ):
        for _ in range(3):
            registry.record_failure(
                _LOCAL_ENDPOINT,
                reason="key_pool_exhausted",
                exc=KeyPoolExhausted("All 2 keys for provider 'x' are muted"),
            )
        await _drain_alerts()

    assert not _auth_alerts(mock_alert, _LOCAL_ENDPOINT)
    assert _STATE_TRANSITIONS.is_firing(_auth_alert_key(_LOCAL_ENDPOINT)) is False
    assert not [r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]
    status = registry.snapshot()[_LOCAL_ENDPOINT]
    assert status["consecutive_auth_rejections"] == 0
    assert status["last_error_status"] is None


async def test_a_success_still_closes_an_incident_that_outlived_other_failures(monkeypatch):
    """Keeping the run through statusless failures must not wedge it open.

    An operator fixing the key is observable as one accepted request, and that
    still closes the incident even after a stretch of pool exhaustion.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        for _ in range(3):
            registry.record_failure(
                _LOCAL_ENDPOINT,
                reason="key_pool_exhausted",
                exc=KeyPoolExhausted("All 2 keys for provider 'x' are muted"),
            )
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(502))
        await _drain_alerts()
        assert _STATE_TRANSITIONS.is_firing(key) is True

        registry.record_success(_LOCAL_ENDPOINT)
        await _drain_alerts()

        assert len(_auth_resolutions(mock_alert, _LOCAL_ENDPOINT)) == 1
        assert _STATE_TRANSITIONS.is_firing(key) is False

    assert registry.snapshot()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 0


async def test_one_endpoints_recovery_does_not_close_anothers_incident(monkeypatch):
    """The incident key is per endpoint, and so is its resolution."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        registry.record_failure(_REMOTE_ENDPOINT, reason="chat_exception", exc=_StatusError(401))
        await _drain_alerts()

        registry.record_success(_REMOTE_ENDPOINT)
        await _drain_alerts()

    assert len(_auth_resolutions(mock_alert, _REMOTE_ENDPOINT)) == 1
    assert not _auth_resolutions(mock_alert, _LOCAL_ENDPOINT)
    assert _STATE_TRANSITIONS.is_firing(_auth_alert_key(_LOCAL_ENDPOINT)) is True
    assert _STATE_TRANSITIONS.is_firing(_auth_alert_key(_REMOTE_ENDPOINT)) is False


async def test_auth_page_is_throttled_but_every_rejection_is_logged(monkeypatch, caplog):
    """A per-request storm must not become a page storm.

    The condition fails 100% of requests, so scheduling a page per request would
    pay ``alert_slack``'s snooze lookup on every one only to be dropped by its
    cooldown. The log line stays per-request — it is the audit trail.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.WARNING, logger="routing.routers"),
    ):
        for _ in range(25):
            registry.record_failure(
                _LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401)
            )
        await asyncio.sleep(0)

        assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 1
        keys = {call.kwargs.get("dedupe_key") for call in mock_alert.await_args_list}
        assert keys == {f"upstream_auth:{_LOCAL_ENDPOINT}"}

    logged = [r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]
    assert len(logged) == 25
    assert logged[-1].consecutive_auth_rejections == 25


async def test_auth_page_reschedules_after_the_throttle_window(monkeypatch):
    """A persisting outage keeps paging; the throttle only spaces the attempts."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        # Pretend the throttle window elapsed rather than sleeping through it.
        registry._auth_alert_at[_LOCAL_ENDPOINT] -= _AUTH_ALERT_SCHEDULE_INTERVAL_SEC + 1
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await asyncio.sleep(0)

    assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 2


async def test_remote_401_also_escalates(monkeypatch):
    """A remote provider revoking the gateway's key is the same class of fault."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    registry.record_success(_REMOTE_ENDPOINT)
    baseline = registry.snapshot()[_REMOTE_ENDPOINT]["availability"]

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        registry.record_failure(_REMOTE_ENDPOINT, reason="chat_exception", exc=_StatusError(401))
        await asyncio.sleep(0)

    assert len(_auth_alerts(mock_alert, _REMOTE_ENDPOINT)) == 1
    assert registry.snapshot()[_REMOTE_ENDPOINT]["availability"] < baseline


# ---------------------------------------------------------------------------
# Firing and recovery must not race each other to the sink
# ---------------------------------------------------------------------------


async def _post_until(predicate, *, timeout: float = 5.0) -> None:
    """Yield to the loop until ``predicate`` holds, so alert tasks can progress."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "alert deliveries did not settle"
        await asyncio.sleep(0)


async def test_recovery_cannot_overtake_the_page_it_closes(monkeypatch):
    """A recovery delivered before its page leaves the incident open forever.

    Driven through the real ``alert_slack`` rather than a mock, because the
    ordering guarantee lives there: it registers the send as in-flight before it
    awaits anything, and a resolution waits on that marker. The regression this
    covers is any await slipping in ahead of that registration — the admin snooze
    lookup was one, and it is skipped entirely for a resolution, so a firing
    parked in it published nothing for the recovery to wait on. The recovery then
    reached the sink first and the page landed after it, opening an incident whose
    only healthy edge is already spent: the rejection run is over, so nothing will
    resolve it again, and state alerts are never swept.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)
    posted: list[str] = []
    lookup_reached = asyncio.Event()
    release_lookup = asyncio.Event()

    async def _stalled_is_snoozed() -> bool:
        # Stands in for any await inside alert_slack that precedes the in-flight
        # registration: the snooze lookup is the one on the firing path today.
        lookup_reached.set()
        await release_lookup.wait()
        return False

    async def _record_post(_webhook_url: str, message: str) -> bool:
        # The plain-webhook sink has only the text, so that is where the recovery
        # is distinguishable — see ``alerts._format_message``.
        posted.append("resolved" if "*Recovered:*" in message else "firing")
        return True

    with (
        patch("serving.observability.alert_snooze.is_snoozed", new=_stalled_is_snoozed),
        patch("serving.observability.alerts._post_to_slack", new=_record_post),
    ):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await asyncio.wait_for(lookup_reached.wait(), timeout=5)
        assert not posted, "the page must still be mid-delivery for this to be the race"

        # The operator fixes the key while that page is in flight.
        registry.record_success(_LOCAL_ENDPOINT)
        await _drain_alerts()
        assert posted != ["resolved"], "the recovery was delivered before the page it closes"

        release_lookup.set()
        await _post_until(lambda: len(posted) >= 2)

    assert posted == ["firing", "resolved"]
    # Delivered in order, so the incident the page opened is the one that closed.
    assert _STATE_TRANSITIONS.is_firing(key) is False


async def test_a_recovery_scheduled_before_the_page_runs_still_closes_it(monkeypatch):
    """The other order: both edges are queued before either has been delivered.

    Serializing must not swallow one of them — the page still opens the incident
    and the recovery still closes it, in that order, so nothing is left open and
    nothing is announced as recovered that never fired.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    key = _auth_alert_key(_LOCAL_ENDPOINT)
    statuses: list[str] = []

    async def _record(severity, title, context, **kwargs):
        statuses.append(kwargs.get("status", "firing"))
        return True

    with patch("serving.observability.alerts.alert_slack", new=_record):
        # No loop turn in between, so neither delivery has started yet.
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        registry.record_success(_LOCAL_ENDPOINT)
        await _post_until(lambda: len(statuses) >= 2)

    assert statuses == ["firing", "resolved"]
    assert _STATE_TRANSITIONS.is_firing(key) is False


async def test_one_endpoints_stalled_page_does_not_delay_anothers(monkeypatch):
    """Serialization is per endpoint, not global.

    A single unreachable endpoint's page can sit in the sink's timeouts for tens
    of seconds; queueing every other endpoint's outage behind it would hide the
    next real one. One dedupe key per endpoint is what keeps the sink's ordering
    scoped that way — see ``_auth_alert_key``.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    stalled = asyncio.Event()
    delivered: list[str] = []

    async def _record(severity, title, context, **kwargs):
        endpoint_id = str(context.get("endpoint_id"))
        if endpoint_id == _LOCAL_ENDPOINT:
            await stalled.wait()
        delivered.append(endpoint_id)
        return True

    with patch("serving.observability.alerts.alert_slack", new=_record):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        registry.record_failure(_REMOTE_ENDPOINT, reason="chat_exception", exc=_StatusError(401))
        await _post_until(lambda: delivered == [_REMOTE_ENDPOINT])

        stalled.set()
        await _post_until(lambda: len(delivered) == 2)

    assert delivered == [_REMOTE_ENDPOINT, _LOCAL_ENDPOINT]


# ---------------------------------------------------------------------------
# Through the router, so the exception really reaches health accounting
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal adapter stand-in: FixedRouter only touches ``config`` and the two
    completion methods."""

    def __init__(self, endpoint_id: str, *, error: BaseException | None = None) -> None:
        self.config = SimpleNamespace(
            id="diffusiongemma",
            provider="compat",
            base_url="http://localhost:8002/v1",
            endpoint_id=endpoint_id,
            input_modalities=["text"],
        )
        self.error = error

    async def chat_completion(self, messages, **params):
        if self.error is not None:
            raise self.error
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def stream_chat_completion(self, messages, **params):
        if self.error is not None:
            raise self.error
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'


async def test_streaming_401_through_fixed_router_escalates(monkeypatch, caplog):
    """The outage shape end to end: one local route, streaming, HTTP 401."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    router = FixedRouter()
    router.register_route(
        "diffusiongemma", [(_FakeAdapter(_LOCAL_ENDPOINT, error=_StatusError(401)), 1.0)]
    )

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert,
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        for _ in range(3):
            with pytest.raises(_StatusError):
                async for _chunk in router.stream_chat_completion("diffusiongemma", []):
                    pass
        await asyncio.sleep(0)

    status = router.get_provider_status()[_LOCAL_ENDPOINT]
    assert status["circuit_state"] == _CircuitState.OPEN
    assert status["consecutive_auth_rejections"] == 3
    assert status["last_error_status"] == 401
    assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 1
    assert len([r for r in caplog.records if r.getMessage() == "upstream_auth_misconfig"]) == 3
    assert not [r for r in caplog.records if r.getMessage() == "client_error_skip_breaker"]

    # With the only route's circuit open the router now fails loudly instead of
    # advertising the model as healthy.
    with pytest.raises(AllCircuitsOpenError):
        async for _chunk in router.stream_chat_completion("diffusiongemma", []):
            pass


async def test_401_on_primary_still_falls_over_to_a_healthy_backup(monkeypatch):
    """Escalating auth must not cost the request its fallback."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    # Deterministic weighted selection: 0.0 always lands on the first adapter,
    # so the failing local route is the primary and the backup is the fallback.
    monkeypatch.setattr("routing.routers.random.random", lambda: 0.0)

    primary = _FakeAdapter(_LOCAL_ENDPOINT, error=_StatusError(401))
    backup = _FakeAdapter(_REMOTE_ENDPOINT)
    router = FixedRouter()
    router.register_route("diffusiongemma", [(primary, 0.5), (backup, 0.5)])

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        chunks = [
            chunk
            async for chunk in router.stream_chat_completion("diffusiongemma", [])
            if '"content"' in chunk
        ]
        await asyncio.sleep(0)

    assert chunks, "the request must be served by the surviving route"
    assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 1
    assert router.get_provider_status()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 1
    assert router.get_provider_status()[_REMOTE_ENDPOINT]["consecutive_auth_rejections"] == 0


def test_auth_escalation_without_a_running_loop_does_not_raise(monkeypatch):
    """A sync caller (teardown, script) must not blow up or leak a coroutine."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))

    assert registry.snapshot()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 1


# ---------------------------------------------------------------------------
# Alert scheduling, shared by the auth page and the breaker's own circuit_open
# ---------------------------------------------------------------------------


#: The two shapes "off the event loop" comes in. Only the first is detectable by
#: letting ``asyncio.ensure_future`` raise, which is why the scheduler probes for
#: a *running* loop instead:
#:
#: - ``no current loop`` — ``ensure_future`` raises ``RuntimeError``.
#: - ``idle current loop`` — a loop object is current but is not running.
#:   ``ensure_future`` *succeeds* there (on 3.12 it will even create the loop
#:   itself, with a "There is no current event loop" DeprecationWarning), handing
#:   back a task attached to something nothing will ever run. Reporting that as a
#:   scheduled delivery is a silent drop, and it strands whatever state the caller
#:   set up for the send.
_OFF_LOOP_SHAPES = ("no current loop", "idle current loop")


def _run_off_the_loop(call, *, shape: str) -> None:
    """Run ``call`` on a worker thread with no *running* loop, in one ``shape``.

    A worker thread is used for both shapes so the surrounding test's own running
    loop is never disturbed; the main thread would additionally be unreliable,
    since whether it still carries a current loop depends on what ran before.
    """
    failure: list[BaseException] = []

    def target() -> None:
        loop = asyncio.new_event_loop() if shape == "idle current loop" else None
        if loop is not None:
            asyncio.set_event_loop(loop)
        try:
            call()
        except BaseException as exc:
            failure.append(exc)
        finally:
            if loop is not None:
                asyncio.set_event_loop(None)
                loop.close()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the alert path must not block the caller"
    if failure:
        raise failure[0]


@pytest.mark.parametrize("shape", _OFF_LOOP_SHAPES)
def test_circuit_open_scheduling_closes_its_coroutine_off_the_loop(monkeypatch, shape):
    """The circuit-open page must not leak a never-awaited coroutine.

    ``on_failure`` builds ``_send_circuit_alert(...)`` before handing it to the
    scheduler, so when there is no loop to run it on the coroutine has to be
    closed explicitly — otherwise it surfaces as a ``RuntimeWarning: coroutine ...
    was never awaited`` in sync callers and tests. This is the same hazard
    ``_fire_and_forget`` was added for, so the path goes through that helper.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    coroutines: list[Any] = []
    original = _CircuitBreaker._send_circuit_alert

    def _capture(self, *args, **kwargs):
        coro = original(self, *args, **kwargs)
        coroutines.append(coro)
        return coro

    monkeypatch.setattr(_CircuitBreaker, "_send_circuit_alert", _capture)
    breaker = _CircuitBreaker(_LOCAL_ENDPOINT)

    _run_off_the_loop(
        lambda: breaker.on_failure(availability=0.0, reason="stream_exception"), shape=shape
    )

    assert breaker.state == _CircuitState.OPEN
    assert len(coroutines) == 1, "the circuit-open page must still be attempted"
    assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED


@pytest.mark.parametrize("shape", _OFF_LOOP_SHAPES)
def test_auth_page_scheduling_closes_its_coroutine_off_the_loop(monkeypatch, shape):
    """Same contract on the auth page, the helper's other caller.

    The rejection is still accounted for — only the delivery is impossible — and
    nothing is left holding a coroutine no loop will drive.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    coroutines: list[Any] = []
    original = endpoint_health.alert_on_transition

    def _capture(**kwargs):
        coro = original(**kwargs)
        coroutines.append(coro)
        return coro

    monkeypatch.setattr(endpoint_health, "alert_on_transition", _capture)
    registry = EndpointHealthRegistry()

    _run_off_the_loop(
        lambda: registry.record_failure(
            _LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401)
        ),
        shape=shape,
    )

    assert len(coroutines) == 1
    assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED
    assert registry.snapshot()[_LOCAL_ENDPOINT]["consecutive_auth_rejections"] == 1


@pytest.mark.parametrize("shape", _OFF_LOOP_SHAPES)
async def test_a_page_that_could_not_be_scheduled_does_not_mute_later_ones(monkeypatch, shape):
    """An impossible delivery must cost one page, never the endpoint's later ones.

    A scheduler that mistook an idle current loop for a live one would report the
    send as scheduled and leave any per-endpoint bookkeeping it set up in place —
    silently muting every later page for that endpoint for the rest of the
    process's life, which is strictly worse than losing the single page whose loop
    was missing.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        _run_off_the_loop(
            lambda: registry.record_failure(
                _LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401)
            ),
            shape=shape,
        )
        assert not _auth_alerts(mock_alert, _LOCAL_ENDPOINT), "no loop, so nothing was delivered"

        # Pretend the throttle window elapsed rather than sleeping through it.
        registry._auth_alert_at[_LOCAL_ENDPOINT] -= _AUTH_ALERT_SCHEDULE_INTERVAL_SEC + 1
        registry.record_failure(_LOCAL_ENDPOINT, reason="stream_exception", exc=_StatusError(401))
        await _drain_alerts()

    assert len(_auth_alerts(mock_alert, _LOCAL_ENDPOINT)) == 1
