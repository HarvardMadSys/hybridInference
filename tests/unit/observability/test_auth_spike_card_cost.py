"""A breached ``auth_failure_spike`` builds its card when it sends, not on every failure.

The card is a full pass over the window, and a source that keeps failing is
what fills that window: at 120,000 failures a minute the pass cost 84 ms, and it
ran for every failure while the rule was breached, on the event loop, although
the cooldown let one message through in ten minutes. These run the real rule
through the real ``alert_on_transition`` and ``alert_slack`` with only the
webhook post stubbed.
"""

from __future__ import annotations

import logging

import pytest

from serving.observability import alert_rules
from serving.observability.alert_config import CountRule
from serving.observability.alerts import SlackSink, reset_dedupe_state, reset_transition_state


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.test/T/B/x")
    reset_dedupe_state()
    reset_transition_state()
    yield
    reset_dedupe_state()
    reset_transition_state()


@pytest.fixture
def posts(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def post(self, message: str, *, dedupe_key: str) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(SlackSink, "post", post)
    monkeypatch.setattr(SlackSink, "post_detailed", None, raising=False)
    # The channel as a published configuration would give it: without one, each
    # lookup rebuilds the configuration from the environment, which is slow.
    sink = SlackSink("https://hooks.slack.test/T/B/x")
    monkeypatch.setattr("serving.observability.alerts.configured_sink", lambda: sink)
    return sent


@pytest.fixture
def card_builds(monkeypatch) -> list[int]:
    """Count the full passes over the window (one per card built)."""
    builds: list[int] = []
    summarize = alert_rules._auth_failure_summary

    def counted(entries):
        builds.append(len(entries))
        return summarize(entries)

    monkeypatch.setattr(alert_rules, "_auth_failure_summary", counted)
    return builds


def _failure(i: int) -> logging.LogRecord:
    record = logging.LogRecord(
        "serving.servers.auth", logging.WARNING, "", 0, "auth_failure", None, None
    )
    record.event = "auth_failure"
    record.remote_ip = f"2001:db8:{i % 65536:x}::1"
    record.peer_ip = "10.0.0.1"
    record.ip_source = "x-forwarded-for"
    record.key_prefix = f"sk-{i % 9999:04d}"
    record.reason = "invalid_key"
    record.path = "/v1/chat/completions"
    return record


async def test_a_sustained_spike_builds_one_card_per_message(posts, card_builds):
    rule = alert_rules.AuthFailureSpikeRule(
        CountRule(enabled=True, window_sec=60, threshold_count=50, cooldown_sec=600)
    )

    for i in range(2_000):
        await rule.on_record(_failure(i))

    # 1,949 breached evaluations, one message: the cooldown let one through,
    # and only that one paid for a pass over the window.
    assert len(posts) == 1
    assert card_builds == [51]
    assert "Auth failure spike" in posts[0]
    assert "51" in posts[0]


async def test_each_message_still_describes_its_own_window(posts, card_builds, monkeypatch):
    """After the cooldown the next message is built afresh, from the window then."""
    clock = [1_000_000.0]
    monkeypatch.setattr(alert_rules.time, "time", lambda: clock[0])
    monotonic = [5_000.0]
    monkeypatch.setattr("serving.observability.alerts._monotonic", lambda: monotonic[0])
    rule = alert_rules.AuthFailureSpikeRule(
        CountRule(enabled=True, window_sec=60, threshold_count=5, cooldown_sec=10)
    )

    for i in range(20):
        await rule.on_record(_failure(i))
    monotonic[0] += 11  # past the cooldown
    for i in range(20, 30):
        await rule.on_record(_failure(i))

    assert len(posts) == 2
    assert card_builds == [6, 21]


async def test_alert_history_gets_the_card_when_the_episode_opens(posts, card_builds, monkeypatch):
    """History reads the card for the breach that opens an episode, and only then."""
    from serving.observability.alert_history import hooks

    episodes: dict[str, str] = {}
    described: list[dict | None] = []

    def breach(key, *, severity, title, kind, context, stale_after_sec=None, now=None):
        described.append(context)
        return episodes.setdefault(key, "ep-1")

    monkeypatch.setattr(hooks, "active", lambda: object())
    monkeypatch.setattr(hooks, "open_episode", episodes.get)
    monkeypatch.setattr(hooks, "breach", breach)
    rule = alert_rules.AuthFailureSpikeRule(
        CountRule(enabled=True, window_sec=60, threshold_count=50, cooldown_sec=600)
    )

    for i in range(300):
        await rule.on_record(_failure(i))

    assert described[0] is not None and described[0]["count"] == 51
    assert described[1:] == [None] * 249
    # The same build served the first message: still one pass over the window.
    assert card_builds == [51]
    assert len(posts) == 1
