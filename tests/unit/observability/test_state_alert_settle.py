"""State alerts that settle their healthy edge, and the recoveries nobody was owed.

A circuit breaker closes on one success and, once its availability has sagged,
reopens on the next failure. Closing the incident on each such close made every
cycle a new incident: a "Provider circuit opened" page plus a "Recovered" card,
with the cooldown reset by each delivered recovery. These tests pin the three
fixes: a settled close, a snoozed incident closing silently, and a recovery card
that says "Recovered" once.

The settle period is long, and ``flush_settling`` ends it on demand, so only the
one test that exercises the timer itself waits on a real clock.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from serving.observability import alerts
from serving.observability.alerts import (
    _PENDING_RESOLUTIONS,
    _STATE_TRANSITIONS,
    AlertSeverity,
    alert_on_transition,
    flush_settling,
    interrupt_settle,
    reset_dedupe_state,
    reset_transition_state,
    sweep_stale_breaches,
)

KEY = "circuit_open:glm-4.6:zai-api"
TITLE = "Provider circuit opened"
SETTLE = 300.0
OPENED = "❌ *Provider circuit opened*"
RECOVERED = "✅ *Recovered:* Provider circuit opened"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    reset_dedupe_state()
    reset_transition_state()
    with patch("serving.observability.alerts._type_muted", new=AsyncMock(return_value=False)):
        yield
    reset_dedupe_state()
    reset_transition_state()


def _slack():
    return patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True))


def _snoozed(value: bool = True):
    return patch("serving.observability.alert_snooze.is_snoozed", new=AsyncMock(return_value=value))


async def _transition(
    breached: bool,
    *,
    settle_sec: float = SETTLE,
    cooldown_sec: int = 300,
    now: float | None = None,
):
    return await alert_on_transition(
        key=KEY,
        breached=breached,
        severity=AlertSeverity.ERROR,
        title=TITLE,
        context=lambda: {"provider": "glm-4.6:zai-api"},
        cooldown_sec=cooldown_sec,
        kind="state",
        settle_sec=settle_sec,
        now=now,
    )


async def _settle_elapsed() -> None:
    """End every pending settle period now, as its timer eventually would."""
    await flush_settling()
    while alerts._SETTLE_TASKS:
        await asyncio.gather(*list(alerts._SETTLE_TASKS))


def _headings(mock_post: AsyncMock) -> list[str]:
    return [call.args[1].splitlines()[0] for call in mock_post.await_args_list]


class TestASettledCloseWaitsOutTheFlap:
    async def test_the_healthy_edge_alone_posts_nothing(self):
        with _slack() as mock_post:
            await _transition(True)
            assert await _transition(False) is False
            # Still one open incident, settling.
            assert _STATE_TRANSITIONS.is_firing(KEY)
            assert _headings(mock_post) == ["❌ *Provider circuit opened*"]

    async def test_the_close_is_announced_once_the_period_holds(self):
        with _slack() as mock_post:
            await _transition(True)
            await _transition(False)
            await _settle_elapsed()

        assert _headings(mock_post) == [
            "❌ *Provider circuit opened*",
            "✅ *Recovered:* Provider circuit opened",
        ]
        assert not _STATE_TRANSITIONS.is_firing(KEY)
        assert KEY not in alerts._SETTLE_TIMERS

    async def test_the_timer_itself_confirms_the_close(self):
        with _slack() as mock_post:
            await _transition(True)
            await _transition(False, settle_sec=0.05)
            assert _headings(mock_post) == [OPENED]
            await asyncio.sleep(0.25)
            while alerts._SETTLE_TASKS:
                await asyncio.gather(*list(alerts._SETTLE_TASKS))

        assert _headings(mock_post) == [OPENED, RECOVERED]
        assert not alerts._SETTLE_TIMERS

    async def test_a_flapping_condition_is_one_incident(self):
        """The regression: twenty open/close cycles used to post twenty pairs."""
        with _slack() as mock_post:
            for _ in range(20):
                await _transition(True)
                await _transition(False)
            # Every re-open landed inside the settle period and the cooldown, so
            # it repeated the open incident rather than paging a new one.
            assert _headings(mock_post) == ["❌ *Provider circuit opened*"]
            await _settle_elapsed()

        assert _headings(mock_post) == [
            "❌ *Provider circuit opened*",
            "✅ *Recovered:* Provider circuit opened",
        ]

    async def test_a_breach_during_the_period_cancels_the_pending_close(self):
        with _slack() as mock_post:
            await _transition(True)
            await _transition(False)
            await _transition(True)
            await _settle_elapsed()

        # The old edge's timer had nothing left to confirm.
        assert _headings(mock_post) == ["❌ *Provider circuit opened*"]
        assert _STATE_TRANSITIONS.is_firing(KEY)
        assert _STATE_TRANSITIONS.settling_since(KEY) is None

    async def test_a_recurrence_held_back_by_its_producer_cancels_the_close(self):
        """``interrupt_settle`` stands in for a breaker trip whose page policy held."""
        with _slack() as mock_post:
            await _transition(True)
            await _transition(False)
            interrupt_settle(KEY)
            await _settle_elapsed()

        assert _headings(mock_post) == ["❌ *Provider circuit opened*"]
        assert _STATE_TRANSITIONS.is_firing(KEY)

    async def test_a_reopen_after_an_announced_recovery_pages_again(self):
        with _slack() as mock_post:
            await _transition(True)
            await _transition(False)
            await _settle_elapsed()
            await _transition(True)

        assert _headings(mock_post) == [
            "❌ *Provider circuit opened*",
            "✅ *Recovered:* Provider circuit opened",
            "❌ *Provider circuit opened*",
        ]

    async def test_zero_settle_keeps_closing_on_the_healthy_edge(self):
        with _slack() as mock_post:
            await _transition(True, settle_sec=0)
            assert await _transition(False, settle_sec=0) is True

        assert _headings(mock_post)[-1] == "✅ *Recovered:* Provider circuit opened"
        assert not alerts._SETTLE_TIMERS

    async def test_a_metric_alert_may_not_settle_on_a_timer(self):
        with pytest.raises(ValueError, match="state alerts only"):
            await alert_on_transition(
                key="fivexx_rate",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="5xx rate exceeded",
                context=dict,
                cooldown_sec=300,
                kind="metric",
                settle_sec=1.0,
            )

    async def test_the_episode_ends_at_the_healthy_edge_not_the_confirmation(self):
        ended = []
        with (
            _slack(),
            patch.object(
                alerts._history,
                "condition_ended",
                side_effect=lambda key, *, basis, now=None: ended.append((key, basis, now)),
            ),
        ):
            await _transition(True, now=1_000.0)
            await _transition(False, now=1_010.0)
            await _settle_elapsed()

        assert ended == [(KEY, "observed_healthy", 1_010.0)]

    async def test_a_reset_cancels_pending_timers(self):
        with _slack():
            await _transition(True)
            await _transition(False)
        assert KEY in alerts._SETTLE_TIMERS
        reset_transition_state()
        assert not alerts._SETTLE_TIMERS


class TestAFailedSettledCloseIsNotRetriedOverALiveCondition:
    async def test_a_held_back_recurrence_drops_the_queued_retry(self):
        """Without this the sweep posted "Recovered" while the circuit was open."""
        posts = AsyncMock(side_effect=lambda url, message, **_: "Recovered" not in message)
        with patch("serving.observability.alerts._post_to_slack", new=posts):
            await _transition(True)
            await _transition(False)
            await _settle_elapsed()
            # The card failed, so its close was queued for the sweep to retry.
            assert KEY in _PENDING_RESOLUTIONS
            assert _STATE_TRANSITIONS.is_firing(KEY)

            interrupt_settle(KEY)
            assert KEY not in _PENDING_RESOLUTIONS
            posts.side_effect = None
            posts.return_value = True
            await sweep_stale_breaches()

        assert _headings(posts) == [OPENED, RECOVERED]  # the failed attempt only
        assert posts.await_count == 2
        assert _STATE_TRANSITIONS.is_firing(KEY)

    async def test_a_breach_during_a_failed_close_keeps_its_own_incident(self):
        async def post(url, message, **_):
            if "Recovered" in message:
                # The condition recurs while the recovery is being sent.
                await _transition(True)
                return False
            return True

        with patch(
            "serving.observability.alerts._post_to_slack", new=AsyncMock(side_effect=post)
        ) as posts:
            await _transition(True)
            await _transition(False)
            await _settle_elapsed()

            # The live breach owns the key: no retry of the stale recovery is
            # queued, and the incident is open rather than re-armed over it.
            assert KEY not in _PENDING_RESOLUTIONS
            assert _STATE_TRANSITIONS.is_firing(KEY)
            assert _STATE_TRANSITIONS.settling_since(KEY) is None
            await sweep_stale_breaches()

        assert posts.await_count == 2


async def test_alert_history_shutdown_waits_for_a_started_settle_confirmation():
    """A confirmation the timer just started is not inside an alert call yet."""
    from serving.observability.alert_history import runtime

    release = asyncio.Event()

    async def confirm():
        await release.wait()

    alerts._start_settle_confirm(KEY, 1.0, confirm)
    (task,) = alerts._SETTLE_TASKS
    assert task in runtime._producer_tasks()
    release.set()
    await task


class TestASnoozedIncidentClosesSilently:
    async def test_an_incident_that_opened_under_a_snooze_closes_without_a_card(self):
        with _slack() as mock_post, _snoozed():
            await _transition(True, settle_sec=0)
            assert await _transition(False, settle_sec=0) is False

        mock_post.assert_not_awaited()
        assert not _STATE_TRANSITIONS.is_firing(KEY)
        assert KEY not in alerts._MUTED_UNANNOUNCED

    async def test_flapping_through_a_snooze_posts_nothing(self):
        """Before: every close posted 'Recovered' while every open was snoozed."""
        with _slack() as mock_post, _snoozed():
            for _ in range(10):
                await _transition(True, settle_sec=0, cooldown_sec=0)
                await _transition(False, settle_sec=0, cooldown_sec=0)

        mock_post.assert_not_awaited()

    async def test_an_incident_announced_before_the_snooze_still_gets_its_close(self):
        with _slack() as mock_post:
            await _transition(True, settle_sec=0)
            with _snoozed():
                assert await _transition(False, settle_sec=0) is True

        assert _headings(mock_post) == [
            "❌ *Provider circuit opened*",
            "✅ *Recovered:* Provider circuit opened",
        ]

    async def test_a_breach_delivered_after_the_snooze_lifts_is_owed_its_close(self):
        with _slack() as mock_post:
            with _snoozed():
                await _transition(True, settle_sec=0, cooldown_sec=0)
            await _transition(True, settle_sec=0, cooldown_sec=0)
            assert await _transition(False, settle_sec=0) is True

        assert _headings(mock_post) == [
            "❌ *Provider circuit opened*",
            "✅ *Recovered:* Provider circuit opened",
        ]


@pytest.mark.parametrize(
    ("variable", "poster", "heading"),
    [
        (
            "SLACK_ALERTS_WEBHOOK_URL",
            "_post_to_slack",
            "✅ *Recovered:* Provider circuit opened",
        ),
        (
            "WECOM_ALERTS_WEBHOOK_URL",
            "_post_to_wecom",
            "✅ **Recovered:** Provider circuit opened",
        ),
    ],
)
async def test_a_recovery_card_says_recovered_once(monkeypatch, variable, poster, heading):
    """Was "✅ Recovered: Recovered: Provider circuit opened" on both sinks."""
    monkeypatch.delenv("SLACK_ALERTS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv(variable, "https://hooks.example.test/x")
    with patch(
        f"serving.observability.alerts.{poster}", new=AsyncMock(return_value=True)
    ) as mock_post:
        await _transition(True)
        await _transition(False)
        await _settle_elapsed()

    recovery = mock_post.await_args_list[-1].args[1].splitlines()[0]
    assert recovery == heading
    assert recovery.count("Recovered") == 1
