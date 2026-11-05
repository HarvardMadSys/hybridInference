"""The notification policy: what reaches the channel, and how often one problem may.

The policy itself is ``serving.observability.alert_policy``; these tests drive
it through ``alert_slack`` and ``alert_on_transition`` as the producers do, so
they also pin the contract it shares with a type mute: a held firing costs no
cooldown, and an incident the policy kept out of the channel closes silently.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from serving.observability import alert_policy
from serving.observability.alert_config import AlertConfig, NotificationPolicy, load_alert_config
from serving.observability.alerts import (
    AlertSeverity,
    alert_on_transition,
    alert_slack,
    reset_dedupe_state,
    reset_transition_state,
)

HOUR = 3600.0
DAY = 24 * HOUR


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    reset_dedupe_state()
    reset_transition_state()
    alert_policy.reset_notification_policy()
    yield
    reset_dedupe_state()
    reset_transition_state()
    alert_policy.reset_notification_policy()


@pytest.fixture
def clock(monkeypatch):
    """The wall clock the policy reads, starting at 1,000,000 s."""
    now = [1_000_000.0]
    monkeypatch.setattr("serving.observability.alert_policy.time.time", lambda: now[0])
    return now


def _enable(**overrides) -> None:
    alert_policy.apply_notification_policy(NotificationPolicy(enabled=True, **overrides))


def _slack(return_value: bool = True):
    return patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=return_value),
    )


def _posted_titles(mock_post) -> list[str]:
    return [call.args[1].splitlines()[0] for call in mock_post.await_args_list]


@contextlib.contextmanager
def _decisions():
    """Collect the reasons alert history is given for not sending."""
    reasons: list[str] = []

    def record(key, *, episode_id, reason, resolution=False):
        reasons.append(reason)

    with patch("serving.observability.alerts._history.decision", side_effect=record):
        yield reasons


async def _metric(breached: bool, now: float, *, key: str = "failed_request_rate_db", **kw):
    return await alert_on_transition(
        key=key,
        breached=breached,
        severity=AlertSeverity.ERROR,
        title="Failed-request rate exceeded (DB-query detector)",
        context=dict,
        cooldown_sec=kw.pop("cooldown_sec", 0),
        now=now,
        **kw,
    )


async def _metric_incident(clock, *, key: str = "failed_request_rate_db", **kw) -> None:
    """One breach followed by a close, at the policy clock's current time."""
    t0 = clock[0]
    await _metric(True, t0, key=key, **kw)
    await _metric(False, t0 + 1.0, key=key, **kw)
    clock[0] = t0 + 200.0
    await _metric(False, t0 + 200.0, key=key, **kw)


async def _circuit(breached: bool, *, key: str = "circuit_open:zai-api") -> bool:
    return await alert_on_transition(
        key=key,
        breached=breached,
        severity=AlertSeverity.ERROR,
        title="Provider circuit opened",
        context=dict,
        cooldown_sec=0,
        kind="state",
    )


# -- off by default -----------------------------------------------------------


async def test_the_policy_is_off_by_default_and_changes_nothing(clock):
    assert AlertConfig().notification_policy.enabled is False
    with _slack() as mock_post:
        assert await alert_slack(AlertSeverity.WARN, "p95", {}, dedupe_key="p95_latency:zhipu")
        assert await _circuit(True)
    assert len(mock_post.await_args_list) == 2
    assert alert_policy._PROBLEMS == {}


# -- destinations -------------------------------------------------------------


async def test_record_and_digest_types_stay_out_of_the_channel(clock):
    _enable()
    with _slack() as mock_post, _decisions() as reasons:
        assert not await alert_slack(AlertSeverity.WARN, "p95", {}, dedupe_key="p95_latency:x")
        assert not await alert_slack(
            AlertSeverity.WARN, "Tracked task", {}, dedupe_key="tracked_task_failure:request_log"
        )
        assert await alert_slack(
            AlertSeverity.ERROR, "Database disconnected", {}, dedupe_key="db_disconnect:postgres"
        )
    assert _posted_titles(mock_post) == ["❌ *Database disconnected*"]
    assert reasons == ["record_only", "digest_only"]


async def test_a_type_the_policy_does_not_list_goes_to_the_channel(clock):
    _enable(destinations={})
    with _slack() as mock_post:
        assert await alert_slack(AlertSeverity.WARN, "p95", {}, dedupe_key="p95_latency:x")
    mock_post.assert_awaited_once()


async def test_a_held_incident_opens_and_closes_without_a_word(clock):
    _enable()
    with _slack() as mock_post:
        await _metric_incident(clock, key="fivexx_rate")
    mock_post.assert_not_awaited()


async def test_a_held_firing_costs_no_cooldown(clock):
    """Turning the policy off mid-incident pages at the next breach."""
    _enable()
    with _slack() as mock_post:
        assert not await _metric(True, clock[0], key="fivexx_rate", cooldown_sec=3600)
        alert_policy.apply_notification_policy(NotificationPolicy())
        assert await _metric(True, clock[0] + 60, key="fivexx_rate", cooldown_sec=3600)
    mock_post.assert_awaited_once()


async def test_a_type_mute_still_wins(clock):
    from serving.observability import alert_mutes

    _enable()
    with (
        patch.object(alert_mutes, "is_alert_type_muted", new=AsyncMock(return_value=True)),
        _slack() as mock_post,
        _decisions() as reasons,
    ):
        assert not await alert_slack(AlertSeverity.WARN, "p95", {}, dedupe_key="p95_latency:x")
    mock_post.assert_not_awaited()
    assert reasons == ["type_muted"]


# -- one message per problem ----------------------------------------------------


async def test_a_sustained_breach_posts_once_whatever_the_cooldown(clock):
    _enable()
    with _slack() as mock_post, _decisions() as reasons:
        for minute in range(5):
            await _metric(True, clock[0] + minute * 60, cooldown_sec=0)
    mock_post.assert_awaited_once()
    assert set(reasons) == {"already_notified"}


async def test_a_recurrence_inside_the_window_is_the_same_problem(clock):
    _enable(renotify_on_recurrence=[])
    with _slack() as mock_post:
        await _metric_incident(clock)
        clock[0] += 6 * HOUR
        await _metric_incident(clock)
    firing, recovery = _posted_titles(mock_post)
    assert firing.startswith("❌")
    assert recovery.startswith("✅ *Recovered:*")


async def test_a_recurrence_after_the_window_is_a_new_problem(clock):
    _enable(renotify_on_recurrence=[])
    with _slack() as mock_post:
        await _metric_incident(clock)
        clock[0] += DAY + 1
        await _metric_incident(clock)
    assert len(mock_post.await_args_list) == 4


async def test_a_flapping_key_keeps_the_problem_open_past_the_window(clock):
    """The window counts from the last close, not the first firing."""
    _enable(renotify_on_recurrence=[])
    with _slack() as mock_post:
        for _ in range(6):
            await _metric_incident(clock)
            clock[0] += 8 * HOUR
    assert len(mock_post.await_args_list) == 2


async def test_listed_keys_post_every_recurrence(clock):
    _enable(renotify_on_recurrence=["failed_request_rate_db", "db_disconnect:*"])
    with _slack() as mock_post:
        await _metric_incident(clock)
        clock[0] += HOUR
        await _metric_incident(clock)
    assert len(mock_post.await_args_list) == 4
    assert alert_policy._renotifies_on_recurrence("db_disconnect:postgres")
    assert not alert_policy._renotifies_on_recurrence("failed_request_rate_db:glm-5.3")


# -- minimum open time ----------------------------------------------------------


async def test_a_circuit_posts_only_once_it_has_been_open_long_enough(clock):
    _enable(min_open_sec={"circuit_open": 2 * HOUR})
    with _slack() as mock_post, _decisions() as reasons:
        assert not await _circuit(True)
        clock[0] += HOUR
        assert not await _circuit(True)
        clock[0] += HOUR
        assert await _circuit(True)
        clock[0] += HOUR
        assert not await _circuit(True)
    mock_post.assert_awaited_once()
    assert reasons == ["below_min_duration", "below_min_duration", "already_notified"]


async def test_a_short_circuit_closes_silently(clock):
    _enable(min_open_sec={"circuit_open": 2 * HOUR})
    with _slack() as mock_post:
        await _circuit(True)
        clock[0] += 60
        assert not await _circuit(False)
    mock_post.assert_not_awaited()


async def test_open_time_adds_up_across_flaps(clock):
    _enable(min_open_sec={"circuit_open": 2 * HOUR})
    with _slack() as mock_post:
        for _ in range(3):
            await _circuit(True)
            clock[0] += 50 * 60
            await _circuit(False)
            clock[0] += 10 * 60
        # Two and a half hours open in total by now.
        assert await _circuit(True)
    mock_post.assert_awaited_once()


# -- restarts -------------------------------------------------------------------


async def test_a_problem_announced_before_a_restart_is_not_announced_again(clock):
    _enable()
    alert_policy.seed_delivered("upstream_auth:zai-api", clock[0] - HOUR)
    with _slack() as mock_post:
        assert not await _metric(True, clock[0], key="upstream_auth:zai-api")
    mock_post.assert_not_awaited()


async def test_a_listed_key_still_posts_after_a_restart(clock):
    """History cannot say whether it recovered in between, and a lost page costs more."""
    _enable()
    alert_policy.seed_delivered("failed_request_rate_db", clock[0] - HOUR)
    with _slack() as mock_post:
        assert await _metric(True, clock[0])
    mock_post.assert_awaited_once()


async def test_a_seeded_problem_expires_with_the_window(clock):
    _enable()
    alert_policy.seed_delivered("db_disconnect:postgres", clock[0] - DAY - 1)
    with _slack() as mock_post:
        assert await alert_slack(
            AlertSeverity.ERROR, "Database disconnected", {}, dedupe_key="db_disconnect:postgres"
        )
    mock_post.assert_awaited_once()


class _Conn:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.args: tuple = ()

    async def fetch(self, sql, *args):
        self.args = args
        if self.error is not None:
            raise self.error
        return self.rows


class _Runtime:
    def __init__(self, conn: _Conn) -> None:
        from types import SimpleNamespace

        self.conn = conn
        self.capture = SimpleNamespace(
            identity=SimpleNamespace(
                source_namespace="default", deployment_id="prod", target_environment="production"
            )
        )

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_preload_seeds_the_keys_history_says_were_announced(clock):
    _enable()
    conn = _Conn(rows=[{"alert_key": "circuit_open:zai-api", "posted_at": clock[0] - 60}])
    assert await alert_policy.preload_from_history(_Runtime(conn)) == 1
    assert conn.args[:4] == ("backend", "default", "prod", "production")
    assert alert_policy.hold_reason("circuit_open:zai-api") == "already_notified"


async def test_preload_is_best_effort(clock):
    _enable()
    runtime = _Runtime(_Conn(error=OSError("database down")))
    assert await alert_policy.preload_from_history(runtime) == 0
    assert await alert_policy.preload_from_history(None) == 0


async def test_preload_does_nothing_while_the_policy_is_off(clock):
    conn = _Conn(rows=[{"alert_key": "x", "posted_at": clock[0]}])
    assert await alert_policy.preload_from_history(_Runtime(conn)) == 0
    assert conn.args == ()


# -- configuration --------------------------------------------------------------


def test_the_block_is_read_from_alerts_yaml(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "notification_policy": {
                    "enabled": True,
                    "destinations": {"p95_latency": "digest"},
                    "min_open_sec": {"circuit_open": 600},
                }
            }
        )
    )
    policy = load_alert_config(path).notification_policy
    assert policy.enabled is True
    assert policy.destinations == {"p95_latency": "digest"}
    assert policy.min_open_sec == {"circuit_open": 600}
    assert policy.renotify_after_sec == DAY


def test_an_unknown_destination_is_rejected():
    with pytest.raises(ValueError, match="destinations"):
        NotificationPolicy(destinations={"p95_latency": "email"})


def test_every_default_destination_names_a_known_alert_type():
    from serving.observability.alert_types import ALERT_TYPES

    known = {t.id for t in ALERT_TYPES}
    policy = NotificationPolicy()
    assert set(policy.destinations) <= known
    assert set(policy.min_open_sec) <= known
