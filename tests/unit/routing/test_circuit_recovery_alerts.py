"""A flapping circuit is one incident: one page, one recovery once it holds.

Reproduces the production symptom end to end through the real breaker and alert
path. Once an endpoint's availability EWMA sits under ``CIRCUIT_MIN_AVAILABILITY``
a single failure reopens the circuit, and any success closes it again. Before
the settle, each of those cycles was a new incident: a "Provider circuit opened"
page and a "Recovered: Recovered: Provider circuit opened" card, the cooldown
reset by every delivered recovery.

The settle period here is long on purpose and is ended explicitly with
``flush_settling``, so no test races a real timer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pydantic
import pytest

from routing import endpoint_health
from routing.completion_outcome import CompletionOutcome
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from serving.observability import alerts, state_alert_policy
from serving.observability.alert_config import CircuitOpenStateChange
from serving.observability.alerts import (
    _STATE_TRANSITIONS,
    flush_settling,
    reset_dedupe_state,
    reset_transition_state,
)

ENDPOINT = "glm-4.6:zai-api"
KEY = f"circuit_open:{ENDPOINT}"
SETTLE = 300.0
OPENED = "❌ *Provider circuit opened*"
RECOVERED = "✅ *Recovered:* Provider circuit opened"


def _set_policy(monkeypatch, **update) -> None:
    monkeypatch.setattr(
        state_alert_policy,
        "_CIRCUIT_OPEN",
        state_alert_policy.circuit_open_policy().model_copy(update=update),
        raising=True,
    )


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    # Production defaults, pinned so a developer's shell cannot change the flap.
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    monkeypatch.delenv("ROUTER_HEALTH_EWMA_ALPHA", raising=False)
    _set_policy(monkeypatch, settle_sec=SETTLE, cooldown_sec=300, enabled=True)
    reset_dedupe_state()
    reset_transition_state()
    with (
        patch("serving.observability.alerts._type_muted", new=AsyncMock(return_value=False)),
        patch("serving.observability.alert_snooze.is_snoozed", new=AsyncMock(return_value=False)),
    ):
        yield
    reset_dedupe_state()
    reset_transition_state()


async def _drain() -> None:
    """Run the breaker's fire-and-forget alert tasks and any settle confirmations."""
    for _ in range(20):
        pending = [
            task
            for task in (*endpoint_health._ALERT_TASKS, *alerts._SETTLE_TASKS)
            if not task.done()
        ]
        if not pending:
            await asyncio.sleep(0)
            return
        await asyncio.gather(*pending)


async def _settle_elapsed() -> None:
    """End every pending settle period now, as its timer eventually would."""
    await flush_settling()
    await _drain()


def _headings(mock_post: AsyncMock) -> list[str]:
    return [call.args[1].splitlines()[0] for call in mock_post.await_args_list]


def _state(registry: EndpointHealthRegistry) -> str:
    return registry.snapshot()[ENDPOINT]["circuit_state"]


def _slack():
    return patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True))


async def _trip(registry: EndpointHealthRegistry) -> None:
    for _ in range(3):
        registry.record_failure(ENDPOINT, reason="upstream_500")
    await _drain()
    assert _state(registry) == _CircuitState.OPEN


async def test_a_flapping_endpoint_posts_one_page_and_one_recovery():
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        for _ in range(15):
            # Any success closes the circuit -- routing is unchanged ...
            registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
            await _drain()
            assert _state(registry) == _CircuitState.CLOSED
            # ... and with availability under the floor one failure reopens it.
            registry.record_failure(ENDPOINT, reason="upstream_500")
            await _drain()
            assert _state(registry) == _CircuitState.OPEN
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()
        # Closed, but the incident is still settling: nothing posted yet.
        assert _headings(mock_post) == [OPENED]
        assert _STATE_TRANSITIONS.is_firing(KEY)

        await _settle_elapsed()

    # Before: 16 pages and 16 "Recovered: Recovered:" cards.
    assert _headings(mock_post) == [OPENED, RECOVERED]
    assert not _STATE_TRANSITIONS.is_firing(KEY)


async def test_a_reopen_whose_page_the_policy_holds_still_cancels_the_recovery(monkeypatch):
    """A trip that never reaches the alert path must not let the timer post "Recovered"."""
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()
        assert _STATE_TRANSITIONS.settling_since(KEY) is not None

        # The breaker's own gate holds the next page back entirely.
        _set_policy(monkeypatch, enabled=False)
        registry.record_failure(ENDPOINT, reason="upstream_500")
        await _drain()
        assert _state(registry) == _CircuitState.OPEN
        assert _STATE_TRANSITIONS.settling_since(KEY) is None
        assert KEY not in alerts._SETTLE_TIMERS

        await _settle_elapsed()
        # The circuit is open again, so no recovery may be announced.
        assert _headings(mock_post) == [OPENED]
        assert _STATE_TRANSITIONS.is_firing(KEY)

        # The next close settles afresh rather than closing on the old edge.
        _set_policy(monkeypatch, enabled=True)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()
        assert _headings(mock_post) == [OPENED]
        assert _STATE_TRANSITIONS.is_firing(KEY)

        await _settle_elapsed()

    assert _headings(mock_post) == [OPENED, RECOVERED]


async def test_a_held_back_reopen_before_the_close_is_reported_cannot_start_a_settle(
    monkeypatch,
):
    """Success and a held-back failure in one loop turn, before the report runs.

    ``interrupt_settle`` finds nothing settling, because the close's report is
    still queued; the report's fence is what refuses it, so the circuit is not
    announced as recovered while it is open.
    """
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        _set_policy(monkeypatch, enabled=False)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        registry.record_failure(ENDPOINT, reason="upstream_500")
        assert _state(registry) == _CircuitState.OPEN
        await _drain()

        assert _STATE_TRANSITIONS.settling_since(KEY) is None
        assert not alerts._SETTLE_TIMERS
        await _settle_elapsed()

    assert _headings(mock_post) == [OPENED]
    assert _STATE_TRANSITIONS.is_firing(KEY)


async def test_a_reopen_while_the_page_is_still_sending_settles_the_next_close_afresh(
    monkeypatch,
):
    """A settle shorter than the page's delivery must not confirm a stale close.

    The confirmation waits for the page before taking the close, so a held-back
    reopen during that wait still cancels it, and the close that follows settles
    on its own healthy edge instead of being announced at once.
    """
    page_posting = asyncio.Event()
    release_page = asyncio.Event()

    async def post(url, message, **_):
        if "Recovered" not in message:
            page_posting.set()
            await release_page.wait()
        return True

    registry = EndpointHealthRegistry()
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(side_effect=post)
    ) as mock_post:
        for _ in range(3):
            registry.record_failure(ENDPOINT, reason="upstream_500")
        await page_posting.wait()
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        for _ in range(5):
            await asyncio.sleep(0)
        first_edge = _STATE_TRANSITIONS.settling_since(KEY)
        assert first_edge is not None

        # The settle period runs out while the page is still being delivered.
        confirmation = asyncio.create_task(flush_settling())
        for _ in range(5):
            await asyncio.sleep(0)
        _set_policy(monkeypatch, enabled=False)
        registry.record_failure(ENDPOINT, reason="upstream_500")
        _set_policy(monkeypatch, enabled=True)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        for _ in range(5):
            await asyncio.sleep(0)

        release_page.set()
        await confirmation
        await _drain()
        assert _headings(mock_post) == [OPENED]
        new_edge = _STATE_TRANSITIONS.settling_since(KEY)
        assert new_edge is not None and new_edge != first_edge

        await _settle_elapsed()

    assert _headings(mock_post) == [OPENED, RECOVERED]


async def test_a_shutdown_flush_does_not_announce_a_circuit_open_again(monkeypatch):
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()
        # Reopened by a path that reported nothing: only the fence can tell.
        registry._circuits[ENDPOINT].state = _CircuitState.OPEN
        await _settle_elapsed()

    assert _headings(mock_post) == [OPENED]
    assert _STATE_TRANSITIONS.is_firing(KEY)


async def test_the_settle_timer_itself_announces_the_close(monkeypatch):
    _set_policy(monkeypatch, settle_sec=0.05)
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()
        await asyncio.sleep(0.25)
        await _drain()

    assert _headings(mock_post) == [OPENED, RECOVERED]


async def test_zero_settle_restores_closing_on_the_first_success(monkeypatch):
    _set_policy(monkeypatch, settle_sec=0.0)
    registry = EndpointHealthRegistry()
    with _slack() as mock_post:
        await _trip(registry)
        registry.record_success(ENDPOINT, outcome=CompletionOutcome.PROGRESS)
        await _drain()

    assert _headings(mock_post) == [OPENED, RECOVERED]


def test_settle_defaults_to_two_minutes_and_rejects_negatives():
    assert CircuitOpenStateChange().settle_sec == 120.0
    with pytest.raises(pydantic.ValidationError):
        CircuitOpenStateChange(settle_sec=-1)
