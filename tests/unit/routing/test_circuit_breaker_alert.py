"""Test that circuit-breaker CLOSED→OPEN transition fires a Slack alert."""

import asyncio
import gc
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from routing.endpoint_health import (
    _MAX_TRACKED_CALLERS,
    EndpointHealthRegistry,
    _caller_str,
    _CircuitBreaker,
    _CircuitState,
)
from serving.observability.alerts import AlertSeverity, _format_message, reset_transition_state
from serving.utils.logging import _STRUCTURED_LOG_KEYS, JsonFormatter


@pytest.fixture(autouse=True)
def _clean_transition_state():
    """The breach tracker is shared, so an open circuit would leak between tests."""
    reset_transition_state()
    yield
    reset_transition_state()


async def test_default_settings_keep_circuit_closed_until_third_failure(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    monkeypatch.delenv("ROUTER_HEALTH_EWMA_ALPHA", raising=False)

    registry = EndpointHealthRegistry()
    endpoint_id = "openai"

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(endpoint_id, reason="upstream_500")
        registry.record_failure(endpoint_id, reason="upstream_500")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED

        registry.record_failure(endpoint_id, reason="upstream_500")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
        await asyncio.sleep(0)


async def test_circuit_open_fires_alert(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        assert cb.state == _CircuitState.OPEN
        # alert_slack is fired via asyncio.ensure_future — let pending tasks run
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()


async def test_circuit_open_alert_includes_upstream_error(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail="HTTP 502 from upstream: bad gateway")
        cb.on_failure(reason="stream_exception", detail="HTTP 502 from upstream: bad gateway")
        assert cb.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert context["upstream_error"] == "HTTP 502 from upstream: bad gateway"
        assert context["reason"] == "stream_exception"


async def test_circuit_open_alert_omits_upstream_error_when_absent(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert "upstream_error" not in context


async def test_circuit_open_alert_only_on_first_transition(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")  # CLOSED → OPEN
        cb.on_failure(reason="upstream_500")  # OPEN → OPEN (no alert)
        await asyncio.sleep(0)
        assert mock_alert.await_count == 1


async def test_circuit_open_emits_structured_log(monkeypatch, caplog):
    """The CLOSED→OPEN transition logs a structured ``circuit_open`` record.

    The Slack alert is fire-and-forget and writes no log line, so this record
    is the only thing that makes the event visible in the application logs.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        cb.on_failure(reason="stream_exception", detail="access_terminated_error", caller="dave")
        cb.on_failure(reason="stream_exception", detail="access_terminated_error", caller="dave")
        assert cb.state == _CircuitState.OPEN
        cb.on_success()
        assert cb.state == _CircuitState.CLOSED

    records = [r for r in caplog.records if r.getMessage() == "circuit_open"]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    assert rec.event == "circuit_open"
    assert rec.provider == "openai"
    assert rec.reason == "stream_exception"
    assert rec.consecutive_failures == 2
    assert rec.upstream_error == "access_terminated_error"
    # Structured (queryable) affected-user mapping, not the pre-formatted string.
    assert rec.affected_callers == {"dave": 2}
    # A dropped stream is the endpoint's fault, and the log says so in a token an
    # operator can filter on, so a log reader draws the same conclusion the alert
    # card does.

    closed_records = [r for r in caplog.records if r.getMessage() == "circuit_closed"]
    assert len(closed_records) == 1
    closed_rec = closed_records[0]
    assert closed_rec.levelno == logging.INFO
    assert closed_rec.event == "circuit_closed"
    assert closed_rec.provider == "openai"
    assert closed_rec.duration_ms is not None


async def test_circuit_open_logs_once_per_transition(monkeypatch, caplog):
    """A circuit that is already OPEN does not re-emit the log on each failure."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.WARNING, logger="routing.routers"),
    ):
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")  # CLOSED → OPEN
        cb.on_failure(reason="upstream_500")  # OPEN → OPEN (no new log)

    opens = [r for r in caplog.records if r.getMessage() == "circuit_open"]
    assert len(opens) == 1


async def test_circuit_open_alert_survives_breaker_gc(monkeypatch):
    """The alert task must complete even if the breaker is GC'd immediately.

    Regression guard for the previous ``self._alert_task = ...`` pattern,
    which only kept the task alive for the breaker's lifetime; if the breaker
    was dropped before the task was scheduled, asyncio's weak-ref bookkeeping
    could let the GC cancel the alert mid-flight. The new implementation adds
    the task to a module-level ``_ALERT_TASKS`` set with ``add_done_callback``
    so it stays alive independently.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb = _CircuitBreaker(provider="openai")
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        assert cb.state == _CircuitState.OPEN
        # Drop all local references to the breaker and force collection.
        del cb
        gc.collect()
        # Yield to the loop so the scheduled alert task runs to completion.
        await asyncio.sleep(0.1)
        mock_alert.assert_awaited_once()


# ---------------------------------------------------------------------------
# Affected-caller reporting
# ---------------------------------------------------------------------------


def test_caller_str_prefers_name_and_pins_id():
    """``_caller_str`` reads identity from the request context."""
    from serving.utils import context as req_ctx

    with req_ctx.push(user_id="01ABC", user_name="alice"):
        assert _caller_str() == "alice (01ABC)"
    with req_ctx.push(user_id="01ABC"):
        assert _caller_str() == "01ABC"
    with req_ctx.push(user_name="alice"):
        assert _caller_str() == "alice"
    # No identity in context (e.g. background task) → nobody to name.
    assert _caller_str() is None


def test_caller_str_collapses_whitespace_in_display_name():
    """A newline-laden display name can't forge extra alert lines."""
    from serving.utils import context as req_ctx

    with req_ctx.push(user_id="01ABC", user_name="ev il\nname\t!"):
        rendered = _caller_str()
    assert rendered == "ev il name ! (01ABC)"
    assert "\n" not in rendered


def test_format_affected_callers_escapes_slack_control_characters():
    """A display name with Slack mrkdwn control chars is escaped, not injected."""
    cb = _CircuitBreaker(
        provider="openai", failure_threshold=999, cooldown_seconds=30, min_availability=0.0
    )
    cb.on_failure(reason="err", caller="<!channel> (01)")
    rendered = cb._format_affected_callers()
    assert rendered == "&lt;!channel&gt; (01) x1"
    assert "<" not in rendered and ">" not in rendered


async def test_circuit_open_alert_lists_affected_callers(monkeypatch):
    """An upstream fault names who it hit — and says they are not the cause."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="extension_alias-api")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", caller="bob (02)")
        cb.on_failure(reason="stream_exception", caller="alice (01)")
        cb.on_failure(reason="stream_exception", caller="alice (01)")
        assert cb.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        # Hardest-hit user first; each carries a failure count.
        assert context["affected_callers"] == "alice (01) x2, bob (02) x1"


async def test_circuit_open_alert_omits_affected_callers_when_unattributed(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert "affected_callers" not in context
        # No list to qualify, so no verdict about it either.


def test_affected_callers_cleared_when_streak_breaks():
    """A success resets the streak so the next trip names only newly hit users."""
    cb = _CircuitBreaker(
        provider="openai", failure_threshold=2, cooldown_seconds=30, min_availability=0.7
    )
    cb.on_failure(reason="err", caller="alice (01)")
    cb.on_success()  # streak broken → the affected-user tally is forgotten
    assert cb._affected_callers == {}
    cb.on_failure(reason="err", caller="bob (02)")
    assert cb._format_affected_callers() == "bob (02) x1"


def test_affected_user_tracking_caps_distinct_users():
    """The distinct-user set is bounded, but the rest overflow into '+N more'."""
    cb = _CircuitBreaker(
        provider="openai",
        failure_threshold=100_000,
        cooldown_seconds=30,
        min_availability=0.0,
    )
    for i in range(_MAX_TRACKED_CALLERS + 25):
        cb.on_failure(reason="err", caller=f"user-{i}")
    # Never tracks more than the cap distinct users.
    assert len(cb._affected_callers) == _MAX_TRACKED_CALLERS
    rendered = cb._format_affected_callers()
    assert "+" in rendered and "more" in rendered


async def test_registry_names_affected_user_from_request_context(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state
    from serving.utils import context as req_ctx

    reset_dedupe_state()

    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        with req_ctx.push(user_id="01ABC", user_name="alice"):
            registry.record_failure("extension_alias-api", reason="stream_exception")
            registry.record_failure("extension_alias-api", reason="stream_exception")
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert context["affected_callers"] == "alice (01ABC) x2"


async def test_upstream_fault_trip_does_not_label_its_victims_as_offenders(monkeypatch):
    """An endpoint dropping SSE streams must not page with its users as culprits.

    The production page this pins: a staging endpoint dropped streams mid-response,
    the breaker tripped on ``stream_exception``, and the card named eleven users
    under "Offending Users" — every one of them a casualty of a broken endpoint.
    The Slack label is the context key title-cased (``alerts._format_message``
    builds it as ``k.replace("_", " ").title()``; there is no label table), so the
    key *is* the label and this asserts on the rendered card, not just the dict.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="staging:api.staging.internal:443")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        # A dropped stream carries no usage marker, so nothing here is
        # attributable to what either caller sent.
        cb.on_failure(reason="stream_exception", detail="peer closed connection", caller="alice")
        cb.on_failure(reason="stream_exception", detail="peer closed connection", caller="bob")
        assert cb.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]

    # The blast radius is still reported — who was hit, and how often, is what
    # sizes the incident and is never dropped.
    assert context["affected_callers"] == "alice x1, bob x1"

    rendered = _format_message(AlertSeverity.ERROR, "Provider circuit opened", context)
    assert "• *Affected Callers:* alice x1, bob x1" in rendered
    assert "offending" not in rendered.lower()


async def test_usage_limit_trip_makes_no_accusation_either(monkeypatch):
    """Even a spent plan quota gets the same neutral label and no cause verdict.

    Tempting to accuse here -- a shared quota really is spent by whoever spent it.
    The counter cannot show who that was: ``on_success`` clears it, so every caller
    whose requests *succeeded* against the plan is erased, and what survives is
    whoever arrived after exhaustion. A verdict computed from this list would name
    a set chosen to exclude the actual cause. The reach is reported; the cause is
    left to ``upstream_error``.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    monkeypatch.setattr("routing.endpoint_health._PAGE_ON_USAGE_LIMIT", True)
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    detail = "you (01ABC) have reached your weekly usage limit, upgrade your plan"
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail=detail, caller="alice (01ABC)")
        cb.on_failure(reason="stream_exception", detail=detail, caller="alice (01ABC)")
        assert cb.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]

    assert context["affected_callers"] == "alice (01ABC) x2"
    rendered = _format_message(AlertSeverity.ERROR, "Provider circuit opened", context)
    assert "• *Affected Callers:* alice (01ABC) x2" in rendered
    # One label for every trip cause, so an operator never has to know which of
    # two field names to search for -- and no sentence assigning blame.
    assert "offending" not in rendered.lower()
    assert "caller-driven" not in rendered.lower()
    # The cause reading lives in the upstream error, which is still carried.
    assert "usage limit" in rendered


async def test_affected_callers_survive_json_serialization(monkeypatch, caplog):
    """The renamed field must reach production logs, not just the LogRecord.

    Both formatters emit only keys in ``_STRUCTURED_LOG_KEYS``, so a field set via
    ``extra=`` is silently dropped from plain *and* JSON output unless it is
    whitelisted there. Renaming ``offending_users`` without moving the whitelist
    entry would delete the field from production logs while every assertion made
    on the record alone still passed.
    """
    assert "affected_callers" in _STRUCTURED_LOG_KEYS
    # The old, blame-asserting spelling is gone from the allowlist too, so a
    # stale emitter cannot quietly keep publishing it.
    assert "offending_users" not in _STRUCTURED_LOG_KEYS

    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")

    cb = _CircuitBreaker(provider="openai")

    with (
        patch("serving.observability.alerts.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        cb.on_failure(reason="stream_exception", caller="dave")
        cb.on_failure(reason="stream_exception", caller="dave")
        await asyncio.sleep(0)

    records = [r for r in caplog.records if r.getMessage() == "circuit_open"]
    assert len(records) == 1
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload["affected_callers"] == {"dave": 2}
