"""A paused failed-request detector keeps its incident, and the sweep leaves it alone.

With ``SLACK_WEBHOOK_URL`` and ``WECOM_ALERTS_WEBHOOK_URL`` both empty the
detector is paused: it queries nothing and changes nothing. Its open incident
must then not be closed on its behalf -- the stale sweep would otherwise post
"Recovered (no recent samples)" through a remaining generic Slack channel, or,
with no channel at all, retry until it forgot the incident -- and the pause
must not count as healthy settling once the detector resumes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.admin.failed_request_alerter import ALERT_KEY, FailedRequestAlerter
from serving.observability import alerts
from serving.observability.alert_runtime import AlertRuntimeConfig
from serving.observability.alert_transitions import DEFAULT_CLEAR_AFTER_SEC

# A fixture the tests below request by name; imported for pytest, not called.
from tests.unit.observability.test_alert_runtime import (
    SLACK_A,
    SLACK_B,
    config_env,  # noqa: F401
    publish,
)

T0 = 1_800_000_000.0
STALE = 901.0

#: Paused, with the generic alert channel still configured.
GENERIC = AlertRuntimeConfig(slack_alerts_webhook_url=SLACK_A)
#: Paused, with no channel at all.
NO_CHANNEL = AlertRuntimeConfig()

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("config_env")]


def running(**overrides) -> AlertRuntimeConfig:
    values = {
        "slack_webhook_url": SLACK_A,
        "slack_alerts_webhook_url": SLACK_A,
        "failed_request_alert_threshold": 10,
        "failed_request_alert_rate": 0.0,
        "failed_request_alert_cooldown_minutes": 5,
    }
    values.update(overrides)
    return AlertRuntimeConfig(**values)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def time(self) -> float:
        return self.t

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)

    def at(self, offset: float) -> None:
        self.t = T0 + offset


class Channel:
    """The mocked Slack post: records each message's first line, and those accepted."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.urls: list[str] = []
        self.delivered: list[str] = []
        self.accept = True
        self.during: asyncio.Event | None = None
        self.hook = None

    async def post(self, url, message, *, dedupe_key=""):
        self.posted.append(message.splitlines()[0])
        self.urls.append(url)
        if self.hook is not None:
            hook, self.hook = self.hook, None
            result = hook()
            if inspect.isawaitable(result):
                await result
        if self.accept:
            self.delivered.append(message.splitlines()[0])
        return self.accept

    def recoveries(self) -> list[str]:
        """The titles of the recovery cards delivered, without the card's own prefix."""
        prefix = "*Recovered:* "
        return [line.split(prefix, 1)[1] for line in self.delivered if prefix in line]


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(alerts.time, "time", clock.time)
    return clock


@pytest.fixture
def channel(monkeypatch) -> Channel:
    channel = Channel()
    monkeypatch.setattr(alerts, "_post_to_slack", channel.post)
    return channel


def pool(failures: int = 500):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=failures)
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


async def open_incident(clock: Clock, channel: Channel) -> tuple[FailedRequestAlerter, MagicMock]:
    publish(running())
    db = pool()
    alerter = FailedRequestAlerter(db, now_fn=clock.now)
    clock.at(0)
    await alerter.run_check()
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert len(channel.posted) == 1
    return alerter, db


async def sweep_at(clock: Clock, offset: float) -> None:
    clock.at(offset)
    await alerts.sweep_stale_breaches()


@pytest.mark.parametrize("paused", [GENERIC, NO_CHANNEL], ids=["generic-slack", "no-channel"])
async def test_the_sweep_leaves_a_paused_detectors_incident_open(clock, channel, paused) -> None:
    alerter, db = await open_incident(clock, channel)
    state = alerts._TRANSITIONS._firing[ALERT_KEY]
    observed = state.last_observed_at
    publish(paused)

    # At once, before any paused check has run, and again after paused ticks.
    await sweep_at(clock, STALE)
    await alerter.run_check()
    await sweep_at(clock, 2 * STALE)
    for minute in range(1, 15):
        await sweep_at(clock, 2 * STALE + 60 * minute)

    assert alerter.last_state == "paused"
    db.fetchval.assert_awaited_once()
    assert channel.recoveries() == []
    assert len(channel.posted) == 1
    # Nothing was retried on its behalf, so nothing could give up and forget it.
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at == observed
    assert ALERT_KEY not in alerts._STALE_RETRY_ATTEMPTS
    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS
    # The cooldown is kept for the resumption.
    assert alerter._last_alert_at is not None


async def test_an_unrelated_metric_is_still_swept_meanwhile(clock, channel) -> None:
    alerter, _db = await open_incident(clock, channel)
    await alerts.alert_on_transition(
        key="other_metric",
        breached=True,
        severity=alerts.AlertSeverity.ERROR,
        title="Other",
        context=dict,
        cooldown_sec=0,
    )
    publish(GENERIC)

    await sweep_at(clock, STALE)

    assert channel.recoveries() == ["Recovered (no recent samples): other_metric"]
    assert not alerts._TRANSITIONS.is_firing("other_metric")
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert alerter.last_state == "running"


async def test_a_queued_recovery_waits_out_the_pause_then_reconciles(clock, channel) -> None:
    alerter, db = await open_incident(clock, channel)
    db.fetchval = AsyncMock(return_value=0)
    clock.at(60)
    await alerter.run_check()
    channel.accept = False
    clock.at(60 + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()  # The healthy edge: its recovery fails to send.
    pending = alerts._PENDING_RESOLUTIONS[ALERT_KEY]
    assert pending.attempts == 0
    channel.accept = True
    sent_before = len(channel.posted)

    publish(GENERIC)
    for minute in range(1, 40):
        await sweep_at(clock, 200 + 60 * minute)

    # Kept as it was: no retry was spent, it did not age out, nothing was sent.
    assert alerts._PENDING_RESOLUTIONS.get(ALERT_KEY) is pending
    assert pending.attempts == 0
    assert len(channel.posted) == sent_before
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    # Resumed: still nothing until a check has evaluated under it.
    publish(running())
    await sweep_at(clock, 3000)
    assert len(channel.posted) == sent_before
    assert alerts._PENDING_RESOLUTIONS.get(ALERT_KEY) is pending

    # Healthy now: the incident closes with one observed recovery, never with
    # a no-samples one.
    for offset in (3000, 3060, 3120, 3180):
        clock.at(offset)
        await alerter.run_check()
        await alerts.sweep_stale_breaches()
    assert len(channel.recoveries()) == 1
    assert channel.recoveries()[0].startswith("Recovered: Failed-request rate exceeded")
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS


async def test_a_queued_recovery_is_superseded_by_a_breach_on_resumption(clock, channel) -> None:
    alerter, db = await open_incident(clock, channel)
    db.fetchval = AsyncMock(return_value=0)
    clock.at(60)
    await alerter.run_check()
    channel.accept = False
    clock.at(60 + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()
    assert ALERT_KEY in alerts._PENDING_RESOLUTIONS
    channel.accept = True

    publish(GENERIC)
    await sweep_at(clock, 1000)
    publish(running())
    db.fetchval = AsyncMock(return_value=500)
    clock.at(1000)
    await alerter.run_check()
    await alerts.sweep_stale_breaches()

    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert channel.recoveries() == []


async def test_resuming_healthy_settles_afresh(clock, channel) -> None:
    alerter, db = await open_incident(clock, channel)
    db.fetchval = AsyncMock(return_value=0)
    clock.at(60)
    await alerter.run_check()  # Settling starts.
    publish(GENERIC)
    clock.at(90)
    await alerter.run_check()

    publish(running())
    # Resume, then an immediate sweep: the incident is long unobserved, and
    # still not closed before a check has evaluated it.
    await sweep_at(clock, 2000)
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    clock.at(2000)
    await alerter.run_check()
    # The pause was not healthy settling: the first check settles afresh.
    assert channel.recoveries() == []
    clock.at(2000 + DEFAULT_CLEAR_AFTER_SEC - 1)
    await alerter.run_check()
    assert channel.recoveries() == []
    clock.at(2000 + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()
    assert channel.recoveries() == ["Recovered: Failed-request rate exceeded (DB-query detector)"]
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_resuming_breached_keeps_the_incident_and_its_cooldown(clock, channel) -> None:
    alerter, _db = await open_incident(clock, channel)
    fired_at = alerter._last_alert_at
    publish(GENERIC)
    clock.at(30)
    await alerter.run_check()
    publish(running())

    # Within the cooldown: no repeat, the same incident, nothing swept.
    clock.at(120)
    await alerter.run_check()
    assert len(channel.posted) == 1
    assert alerter._last_alert_at == fired_at
    await sweep_at(clock, 280)
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    # The cooldown over: the incident's repeat, still one incident.
    clock.at(301)
    await alerter.run_check()
    assert len(channel.posted) == 2
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    await sweep_at(clock, 301 + 600)
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert channel.recoveries() == []


async def test_resuming_after_a_long_pause_then_sweeping_sends_no_false_recovery(
    clock, channel
) -> None:
    alerter, _db = await open_incident(clock, channel)
    publish(NO_CHANNEL)
    await sweep_at(clock, STALE)
    publish(running())
    await sweep_at(clock, 2 * STALE)
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    clock.at(2 * STALE)
    await alerter.run_check()  # Still breached; the cooldown is long over.
    assert channel.posted == [channel.posted[0]] * 2
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_superseded_first_check_does_not_release_the_incident(clock, channel) -> None:
    alerter, db = await open_incident(clock, channel)
    publish(GENERIC)
    publish(running())

    async def changed_during_query(*_args):
        publish(running(failed_request_alert_threshold=20))
        return 0

    db.fetchval = AsyncMock(side_effect=changed_during_query)
    clock.at(1000)
    await alerter.run_check()
    assert alerter.superseded_checks == 1

    await alerts.sweep_stale_breaches()
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_pause_during_a_recovery_send_keeps_the_incident_for_a_fresh_check(
    clock, channel
) -> None:
    alerter, db = await open_incident(clock, channel)
    db.fetchval = AsyncMock(return_value=0)
    clock.at(60)
    await alerter.run_check()
    # The recovery's post is under way when the detector is paused; it is
    # refused, and the generation fence keeps it from being queued.
    channel.accept = False
    channel.hook = lambda: publish(GENERIC)
    clock.at(60 + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()
    assert channel.posted[-1].endswith(
        "Recovered: Failed-request rate exceeded (DB-query detector)"
    )
    assert channel.recoveries() == []
    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    channel.accept = True

    await sweep_at(clock, 2000)
    await sweep_at(clock, 3000)
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    publish(running())
    for offset in (3000, 3060, 3120):
        clock.at(offset)
        await alerter.run_check()
    assert channel.recoveries() == ["Recovered: Failed-request rate exceeded (DB-query detector)"]
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_pause_during_a_breach_send_keeps_its_cooldown_and_incident(clock, channel) -> None:
    publish(running())
    alerter = FailedRequestAlerter(pool(), now_fn=clock.now)
    channel.hook = lambda: publish(GENERIC)
    clock.at(0)
    await alerter.run_check()
    # Delivered under the configuration that admitted it: the cooldown stands.
    assert len(channel.posted) == 1
    assert alerter._last_alert_at is not None
    await sweep_at(clock, STALE)
    await sweep_at(clock, 2 * STALE)
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert channel.recoveries() == []


# --- a pause while the sweep is awaiting ---------------------------------------
#
# The sweep decides which keys it may close before its first send, then awaits
# each send in turn. A pause during one of those awaits must reach every key
# the sweep has not sent for yet, and a recovery's own admission.

DETECTOR_RECOVERY = "Recovered: Failed-request rate exceeded (DB-query detector)"


async def open_metric(key: str) -> None:
    """Open an incident on another metric rule; it is swept like any other."""
    await alerts.alert_on_transition(
        key=key,
        breached=True,
        severity=alerts.AlertSeverity.ERROR,
        title="Other",
        context=dict,
        cooldown_sec=0,
    )


async def queue_detector_recovery(clock: Clock, channel: Channel, alerter, db):
    """Settle the detector's incident healthy, with its recovery card refused."""
    db.fetchval = AsyncMock(return_value=0)
    clock.at(60)
    await alerter.run_check()
    channel.accept = False
    clock.at(60 + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()
    channel.accept = True
    return alerts._PENDING_RESOLUTIONS[ALERT_KEY]


async def yield_a_few() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def post_concurrently(channel: Channel, release: asyncio.Event) -> asyncio.Task:
    """Start a send on the detector's key and return once it is posting, until *release*."""
    posted = len(channel.posted)
    channel.hook = release.wait
    task = asyncio.create_task(
        alerts.alert_slack(
            alerts.AlertSeverity.ERROR, "Concurrent", {}, dedupe_key=ALERT_KEY, cooldown_sec=0
        )
    )
    for _ in range(100):
        if len(channel.posted) > posted:
            return task
        await asyncio.sleep(0)
    task.cancel()
    raise AssertionError("the concurrent send never reached the channel")


def ended_conditions(monkeypatch) -> list[str]:
    """The keys whose episode the alert history is told has ended, in order."""
    ended: list[str] = []
    real = alerts._history.condition_ended

    def record(key, **kwargs):
        ended.append(key)
        return real(key, **kwargs)

    monkeypatch.setattr(alerts._history, "condition_ended", record)
    return ended


async def test_a_pause_during_an_earlier_stale_close_fences_the_sweep(
    clock, channel, monkeypatch
) -> None:
    _alerter, _db = await open_incident(clock, channel)
    await open_metric("a_metric")  # Sorts first, so its close is sent first.
    observed = alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at
    ended = ended_conditions(monkeypatch)
    channel.hook = lambda: publish(GENERIC)

    await sweep_at(clock, STALE)

    assert channel.recoveries() == ["Recovered (no recent samples): a_metric"]
    # Put back as it was: the liveness clock kept, no retry counted, and no
    # no-samples end recorded for a close that did not happen.
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at == observed
    assert ALERT_KEY not in alerts._STALE_RETRY_ATTEMPTS
    assert ended == ["a_metric"]

    # Then held as usual: while paused, and once resumed until a check runs.
    await sweep_at(clock, 2 * STALE)
    publish(running())
    await sweep_at(clock, 3 * STALE)
    assert channel.recoveries() == ["Recovered (no recent samples): a_metric"]
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_pause_resume_and_check_during_an_earlier_close_still_fences(
    clock, channel, monkeypatch
) -> None:
    alerter, db = await open_incident(clock, channel)
    await open_metric("a_metric")
    db.fetchval = AsyncMock(return_value=0)
    ended = ended_conditions(monkeypatch)

    async def pause_resume_and_check() -> None:
        publish(GENERIC)
        publish(running())
        await alerter.run_check()

    channel.hook = pause_resume_and_check
    await sweep_at(clock, STALE)

    # The hold reads clear again by now; the generation still tells the sweep
    # that what it decided no longer stands.
    assert not alerter._holds_incident()
    assert channel.recoveries() == ["Recovered (no recent samples): a_metric"]
    # The check landed on the open incident, and nothing overwrote it.
    state = alerts._TRANSITIONS._firing[ALERT_KEY]
    assert (state.last_observed_at, state.clear_started_at) == (T0 + STALE, T0 + STALE)
    assert ended == ["a_metric"]

    # It settles from that check, under the resumed configuration, and closes
    # observed -- not as "no recent samples".
    for offset in (STALE + 60, STALE + DEFAULT_CLEAR_AFTER_SEC - 1):
        clock.at(offset)
        await alerter.run_check()
        await alerts.sweep_stale_breaches()
    assert channel.recoveries() == ["Recovered (no recent samples): a_metric"]
    clock.at(STALE + DEFAULT_CLEAR_AFTER_SEC)
    await alerter.run_check()
    assert channel.recoveries()[1:] == [DETECTOR_RECOVERY]
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


@pytest.mark.parametrize("resumed", [False, True], ids=["paused", "paused-resumed-checked"])
async def test_a_pause_during_an_earlier_retry_fences_a_queued_recovery(
    clock, channel, resumed
) -> None:
    alerter, db = await open_incident(clock, channel)
    alerts._PENDING_RESOLUTIONS["a_state"] = alerts._PendingResolution(
        kind="state", title="Other", queued_at=clock.t
    )
    pending = await queue_detector_recovery(clock, channel, alerter, db)
    assert list(alerts._PENDING_RESOLUTIONS) == ["a_state", ALERT_KEY]

    async def pause() -> None:
        publish(GENERIC)
        if resumed:
            # The check settles afresh; the queued card is from before the pause.
            publish(running())
            await alerter.run_check()

    channel.hook = pause

    await sweep_at(clock, 240)

    assert channel.recoveries() == ["Recovered: Other"]
    assert alerts._PENDING_RESOLUTIONS.get(ALERT_KEY) is pending
    assert pending.attempts == 0
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)

    # Resumed on another channel: the recovery is delivered there, once.
    publish(running(slack_webhook_url=SLACK_B, slack_alerts_webhook_url=SLACK_B))
    for offset in (300, 360, 420):
        clock.at(offset)
        await alerter.run_check()
        await alerts.sweep_stale_breaches()
    assert channel.recoveries() == ["Recovered: Other", DETECTOR_RECOVERY]
    assert channel.urls[-1] == SLACK_B
    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_pause_while_a_retry_awaits_admission_costs_no_attempt(clock, channel) -> None:
    alerter, db = await open_incident(clock, channel)
    pending = await queue_detector_recovery(clock, channel, alerter, db)
    posted = len(channel.posted)

    # A send already posting on the key: the retry waits for it to finish.
    release = asyncio.Event()
    concurrent = await post_concurrently(channel, release)
    clock.at(240)
    sweep = asyncio.create_task(alerts.sweep_stale_breaches())
    await yield_a_few()
    publish(GENERIC)
    release.set()
    await concurrent
    await sweep

    assert len(channel.posted) == posted + 1
    assert channel.recoveries() == []
    assert alerts._PENDING_RESOLUTIONS.get(ALERT_KEY) is pending
    assert pending.attempts == 0
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_pause_while_a_stale_close_awaits_admission_keeps_the_incident(
    clock, channel, monkeypatch
) -> None:
    _alerter, _db = await open_incident(clock, channel)
    observed = alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at
    posted = len(channel.posted)
    ended = ended_conditions(monkeypatch)

    release = asyncio.Event()
    clock.at(STALE)
    concurrent = await post_concurrently(channel, release)
    sweep = asyncio.create_task(alerts.sweep_stale_breaches())
    await yield_a_few()
    publish(GENERIC)
    release.set()
    await concurrent
    await sweep

    assert len(channel.posted) == posted + 1
    assert channel.recoveries() == []
    assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    assert alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at == observed
    assert ALERT_KEY not in alerts._STALE_RETRY_ATTEMPTS
    # Fenced before the episode's end was recorded, not after.
    assert ended == []


@pytest.mark.parametrize("failures", [0, 500], ids=["healthy", "breached"])
async def test_a_check_during_the_wait_lands_on_the_incident(
    clock, channel, monkeypatch, failures
) -> None:
    alerter, db = await open_incident(clock, channel)
    ended = ended_conditions(monkeypatch)

    release = asyncio.Event()
    clock.at(STALE)
    concurrent = await post_concurrently(channel, release)
    sweep = asyncio.create_task(alerts.sweep_stale_breaches())
    await yield_a_few()
    # The incident is still the tracker's while the sweep waits, so the check
    # observes it rather than finding nothing and ending its episode.
    db.fetchval = AsyncMock(return_value=failures)
    await alerter.run_check()
    release.set()
    await concurrent
    await sweep

    assert channel.recoveries() == []
    assert ended == []
    state = alerts._TRANSITIONS._firing[ALERT_KEY]
    assert state.last_observed_at == T0 + STALE
    assert state.clear_started_at == (T0 + STALE if failures == 0 else None)
    assert ALERT_KEY not in alerts._STALE_RETRY_ATTEMPTS

    if failures == 0:
        # And it settles from that check, then closes observed.
        clock.at(STALE + DEFAULT_CLEAR_AFTER_SEC)
        await alerter.run_check()
        assert channel.recoveries() == [DETECTOR_RECOVERY]
        assert ended == [ALERT_KEY]


class StuckSend(asyncio.Event):
    """An in-flight marker that counts the waits made on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    async def wait(self):
        self.waits += 1
        return await super().wait()


@pytest.fixture
def short_wait(monkeypatch) -> None:
    monkeypatch.setattr(alerts, "_RESOLUTION_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(alerts, "_RESOLUTION_WAIT_SEC", 0.01)


@pytest.mark.parametrize("paused", [False, True], ids=["then-finished", "then-paused"])
async def test_a_wait_that_runs_out_defers_the_close_untouched(
    clock, channel, monkeypatch, short_wait, paused
) -> None:
    _alerter, _db = await open_incident(clock, channel)
    observed = alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at
    ended = ended_conditions(monkeypatch)
    stuck = StuckSend()
    alerts._IN_FLIGHT[ALERT_KEY] = stuck

    await sweep_at(clock, STALE)

    # One bound, not a second inside the send; nothing recorded or counted.
    assert stuck.waits == 1
    assert channel.recoveries() == []
    assert ended == []
    assert alerts._TRANSITIONS._firing[ALERT_KEY].last_observed_at == observed
    assert ALERT_KEY not in alerts._STALE_RETRY_ATTEMPTS

    if paused:
        publish(GENERIC)
    alerts._IN_FLIGHT.pop(ALERT_KEY)
    stuck.set()
    await sweep_at(clock, STALE + 60)
    if paused:
        assert channel.recoveries() == []
        assert ended == []
        assert alerts._TRANSITIONS.is_firing(ALERT_KEY)
    else:
        # Deferred, not dropped: the next sweep closes it.
        assert channel.recoveries() == ["Recovered (no recent samples): failed_request_rate_db"]
        assert ended == [ALERT_KEY]
        assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_retry_whose_wait_runs_out_costs_no_attempt(clock, channel, short_wait) -> None:
    alerter, db = await open_incident(clock, channel)
    pending = await queue_detector_recovery(clock, channel, alerter, db)
    stuck = StuckSend()
    alerts._IN_FLIGHT[ALERT_KEY] = stuck

    await sweep_at(clock, 240)

    assert stuck.waits == 1
    assert channel.recoveries() == []
    assert alerts._PENDING_RESOLUTIONS.get(ALERT_KEY) is pending
    assert pending.attempts == 0

    alerts._IN_FLIGHT.pop(ALERT_KEY)
    stuck.set()
    await sweep_at(clock, 300)
    assert channel.recoveries() == [DETECTOR_RECOVERY]
    assert ALERT_KEY not in alerts._PENDING_RESOLUTIONS
    assert not alerts._TRANSITIONS.is_firing(ALERT_KEY)


async def test_a_breach_during_an_earlier_stale_close_is_not_swept(
    clock, channel, monkeypatch
) -> None:
    publish(running())
    clock.at(0)
    await open_metric("a_metric")
    await open_metric("b_metric")
    ended = ended_conditions(monkeypatch)
    channel.hook = lambda: open_metric("b_metric")

    await sweep_at(clock, STALE)

    # The breach opened a new incident on b_metric; this sweep took the old one
    # and must neither end the new episode nor announce it as recovered.
    assert channel.recoveries() == ["Recovered (no recent samples): a_metric"]
    assert alerts._TRANSITIONS.is_firing("b_metric")
    assert alerts._TRANSITIONS._firing["b_metric"].last_observed_at == T0 + STALE
    assert ended == ["a_metric"]


async def test_a_claim_fails_once_anything_touched_the_key() -> None:
    from serving.observability.alert_transitions import ThresholdTransitionTracker

    tracker = ThresholdTransitionTracker(stale_after_sec=10.0)
    tracker.observe("k", breached=True, now=0.0)
    assert tracker.stale(20.0) == ["k"] and tracker.is_firing("k")

    for touch in (
        lambda: tracker.observe("k", breached=False, now=15.0),
        lambda: tracker.observe("k", breached=True, now=15.0),
        lambda: tracker.rearm("k", 0.0),
        lambda: (tracker.forget("k"), tracker.observe("k", breached=True, now=0.0)),
    ):
        tracker.forget("k")
        tracker.observe("k", breached=True, now=0.0)
        mark = tracker.mark("k")
        touch()
        assert not tracker.claim("k", mark)
        assert tracker.is_firing("k")

    tracker.forget("k")
    tracker.observe("k", breached=True, now=0.0)
    mark = tracker.mark("k")
    assert tracker.claim("k", mark) and not tracker.is_firing("k")
    tracker.reinstate("k", mark)
    assert tracker.is_firing("k") and tracker.stale(20.0) == ["k"]
    # Never over a new incident.
    tracker.forget("k")
    tracker.observe("k", breached=True, now=19.0)
    tracker.reinstate("k", mark)
    assert tracker.stale(20.0) == []
