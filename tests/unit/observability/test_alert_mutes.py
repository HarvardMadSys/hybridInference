"""Per-type alert mutes: how they are stored, and what one does to an incident.

The storage half is ``serving.observability.alert_mutes``. The rest is the
contract ``alerts.py`` keeps around it: a muted type sends no firing, costs no
cooldown, and an incident the mute kept out of the channel closes without a
"Recovered" card -- while one the channel already saw still gets its close.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from serving.observability import alert_mutes
from serving.observability.alerts import (
    _ANNOUNCED,
    _MUTED_UNANNOUNCED,
    _STATE_TRANSITIONS,
    _TRANSITIONS,
    AlertSeverity,
    alert_on_transition,
    alert_slack,
    reset_dedupe_state,
    reset_transition_state,
    sweep_stale_breaches,
)


class FakeStore:
    """In-memory ``site_settings`` with the three calls the mutes use."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.list_calls = 0

    async def list_settings(self):
        self.list_calls += 1
        return [{"key": key, **row} for key, row in sorted(self.rows.items())]

    async def set_setting(self, key, value, value_type, updated_by):
        self.rows[key] = {
            "value": value,
            "value_type": value_type,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": updated_by,
        }

    async def delete_setting(self, key):
        return self.rows.pop(key, None) is not None


@pytest.fixture
def store():
    s = FakeStore()
    alert_mutes.init_alert_mutes(s)
    yield s
    alert_mutes.init_alert_mutes(None)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    reset_dedupe_state()
    reset_transition_state()
    yield
    reset_dedupe_state()
    reset_transition_state()


def _slack(return_value: bool = True):
    return patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=return_value),
    )


def _posted_titles(mock_post) -> list[str]:
    return [call.args[1].splitlines()[0] for call in mock_post.await_args_list]


# -- storage ------------------------------------------------------------------


async def test_nothing_is_muted_without_a_store():
    alert_mutes.init_alert_mutes(None)
    assert await alert_mutes.list_mutes() == {}
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False
    with pytest.raises(RuntimeError):
        await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")


async def test_a_timed_mute_silences_its_type_until_the_deadline(store):
    until = time.time() + 3600
    mute = await alert_mutes.mute_alert_type("auth_ip_blocked", until, "admin@x.com")

    assert mute.until == until
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is True
    assert await alert_mutes.is_alert_type_muted("circuit_open") is False
    row = store.rows["slack_alert_mute:auth_ip_blocked"]
    assert row["value_type"] == "json"
    assert json.loads(row["value"]) == {"until": until}
    assert row["updated_by"] == "admin@x.com"


async def test_a_lapsed_mute_silences_nothing(store):
    await alert_mutes.mute_alert_type("auth_ip_blocked", time.time() - 1, "admin@x.com")

    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False
    # Still stored and listed, just inactive, until a later write replaces it.
    assert "auth_ip_blocked" in await alert_mutes.list_mutes()


async def test_an_indefinite_mute_lasts_until_it_is_lifted(store):
    await alert_mutes.mute_alert_type("circuit_open", None, "admin@x.com")

    assert json.loads(store.rows["slack_alert_mute:circuit_open"]["value"]) == {"until": None}
    assert await alert_mutes.is_alert_type_muted("circuit_open") is True

    assert await alert_mutes.unmute_alert_type("circuit_open") is True
    assert await alert_mutes.is_alert_type_muted("circuit_open") is False
    assert "slack_alert_mute:circuit_open" not in store.rows
    # Lifting a mute that is not there is a no-op, not an error.
    assert await alert_mutes.unmute_alert_type("circuit_open") is False


async def test_the_listing_carries_who_muted_and_when(store):
    await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")

    mute = (await alert_mutes.list_mutes())["auth_ip_blocked"]
    assert mute.muted_by == "admin@x.com"
    assert mute.muted_at is not None and abs(mute.muted_at - time.time()) < 5


async def test_reads_are_cached_but_a_write_is_honored_at_once(store):
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False
    assert store.list_calls == 1

    # Within the TTL of the read above, and still honored immediately.
    await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is True

    await alert_mutes.unmute_alert_type("auth_ip_blocked")
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["until"]),
        json.dumps({}),
        json.dumps({"until": "soon"}),
        json.dumps({"until": True}),
        '{"until": NaN}',
    ],
)
async def test_a_corrupt_row_mutes_nothing(store, raw):
    store.rows["slack_alert_mute:auth_ip_blocked"] = {"value": raw, "value_type": "json"}
    await alert_mutes.mute_alert_type("circuit_open", None, "admin@x.com")

    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False
    # One bad row must not take the others down with it.
    assert await alert_mutes.is_alert_type_muted("circuit_open") is True


async def test_other_settings_are_not_read_as_mutes(store):
    store.rows["slack_alerts_snooze_until"] = {"value": "9999999999", "value_type": "float"}
    store.rows["slack_alert_mute:"] = {"value": json.dumps({"until": None})}

    assert await alert_mutes.list_mutes() == {}


async def test_an_infinite_deadline_is_refused(store):
    with pytest.raises(ValueError):
        await alert_mutes.mute_alert_type("auth_ip_blocked", float("inf"), "admin@x.com")
    assert store.rows == {}


async def test_a_failed_read_keeps_serving_the_last_snapshot(store, monkeypatch):
    await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is True

    async def down():
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(store, "list_settings", down)
    monkeypatch.setattr(alert_mutes, "_CACHE_TTL", 0.0)
    # An admin's mute stays honored while the store is down.
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is True


async def test_a_failed_first_read_mutes_nothing(store, monkeypatch):
    async def down():
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(store, "list_settings", down)
    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is False


async def test_a_read_in_flight_cannot_undo_a_later_write(store, monkeypatch):
    """A slow read that started before a mute must not publish its older view."""
    released = asyncio.Event()
    before_the_write = await store.list_settings()

    async def slow_list():
        await released.wait()
        return before_the_write

    monkeypatch.setattr(store, "list_settings", slow_list)
    reader = asyncio.create_task(alert_mutes.list_mutes())
    await asyncio.sleep(0)

    monkeypatch.setattr(store, "list_settings", FakeStore.list_settings.__get__(store))
    await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")
    released.set()
    await reader

    assert await alert_mutes.is_alert_type_muted("auth_ip_blocked") is True


# -- alert_slack --------------------------------------------------------------


async def test_a_muted_type_sends_nothing(store):
    await alert_mutes.mute_alert_type("auth_ip_blocked", None, "admin@x.com")
    with _slack() as mock_post:
        sent = await alert_slack(
            AlertSeverity.WARN,
            "Auth-failure blocklist refusing a source",
            {},
            dedupe_key="auth_ip_blocked",
        )
    assert sent is False
    mock_post.assert_not_awaited()


async def test_a_mute_covers_every_key_of_its_type_and_nothing_else(store):
    await alert_mutes.mute_alert_type("circuit_open", None, "admin@x.com")
    with _slack() as mock_post:
        assert not await alert_slack(
            AlertSeverity.ERROR, "Provider circuit opened", {}, dedupe_key="circuit_open:zhipu"
        )
        assert not await alert_slack(
            AlertSeverity.ERROR, "Provider circuit opened", {}, dedupe_key="circuit_open:kimi"
        )
        assert await alert_slack(
            AlertSeverity.ERROR, "Database disconnected", {}, dedupe_key="db_disconnect:postgres"
        )
    assert _posted_titles(mock_post) == ["❌ *Database disconnected*"]


async def test_a_muted_firing_costs_no_cooldown(store):
    """Once the mute lifts, a breach that is still live pages at once."""
    await alert_mutes.mute_alert_type("fivexx_rate", None, "admin@x.com")
    with _slack() as mock_post:
        assert not await alert_slack(
            AlertSeverity.ERROR,
            "5xx rate exceeded",
            {},
            dedupe_key="fivexx_rate",
            cooldown_sec=3600,
        )
        await alert_mutes.unmute_alert_type("fivexx_rate")
        assert await alert_slack(
            AlertSeverity.ERROR,
            "5xx rate exceeded",
            {},
            dedupe_key="fivexx_rate",
            cooldown_sec=3600,
        )
    mock_post.assert_awaited_once()


async def test_a_failed_mute_lookup_fails_open(store):
    with (
        patch(
            "serving.observability.alert_mutes.is_alert_type_muted",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        _slack() as mock_post,
    ):
        assert await alert_slack(
            AlertSeverity.WARN,
            "Auth-failure blocklist refusing a source",
            {},
            dedupe_key="auth_ip_blocked",
        )
    mock_post.assert_awaited_once()


async def test_alert_slack_itself_never_drops_a_resolution(store):
    """Whether a close is announced is decided by the incident, upstream of the sink."""
    await alert_mutes.mute_alert_type("fivexx_rate", None, "admin@x.com")
    with _slack() as mock_post:
        assert await alert_slack(
            AlertSeverity.INFO,
            "Recovered: 5xx rate exceeded",
            {},
            dedupe_key="fivexx_rate",
            status="resolved",
        )
    mock_post.assert_awaited_once()


# -- incidents ----------------------------------------------------------------


async def _metric(breached: bool, now: float, *, key: str = "p95_latency:zhipu", **kwargs):
    return await alert_on_transition(
        key=key,
        breached=breached,
        severity=AlertSeverity.WARN,
        title="p95 latency exceeded for provider zhipu",
        context=dict,
        cooldown_sec=kwargs.pop("cooldown_sec", 0),
        now=now,
        **kwargs,
    )


async def _metric_incident_closes(t0: float, **kwargs) -> bool:
    """Clear a metric that breached at ``t0``: healthy, then healthy past the settling period."""
    await _metric(False, t0 + 1.0, **kwargs)
    return await _metric(False, t0 + 200.0, **kwargs)


async def _circuit(breached: bool) -> bool:
    return await alert_on_transition(
        key="circuit_open:zhipu",
        breached=breached,
        severity=AlertSeverity.ERROR,
        title="Provider circuit opened",
        context=dict,
        cooldown_sec=0,
        kind="state",
    )


async def test_a_muted_metric_incident_opens_and_closes_without_a_word(store):
    await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
    with _slack() as mock_post:
        await _metric(True, 1_000.0)
        closed = await _metric_incident_closes(1_000.0)

    assert closed is False
    mock_post.assert_not_awaited()
    # Closed, not stranded: no firing state and nothing a dynamic key would leak.
    assert not _TRANSITIONS._firing
    assert not _TRANSITIONS._bounds
    assert not _MUTED_UNANNOUNCED and not _ANNOUNCED


async def test_a_muted_state_incident_closes_on_its_one_healthy_edge_silently(store):
    await alert_mutes.mute_alert_type("circuit_open", None, "admin@x.com")
    with _slack() as mock_post:
        await _circuit(True)
        closed = await _circuit(False)

    assert closed is False
    mock_post.assert_not_awaited()
    assert not _STATE_TRANSITIONS._firing


async def test_an_incident_announced_before_the_mute_still_gets_its_close(store):
    with _slack() as mock_post:
        await _metric(True, 1_000.0)
        await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
        await _metric(True, 1_060.0)
        closed = await _metric_incident_closes(1_060.0)

    assert closed is True
    firing, recovery = _posted_titles(mock_post)
    assert firing == "⚠️ *p95 latency exceeded for provider zhipu*"
    assert recovery.startswith("✅ *Recovered:*")


async def test_lifting_the_mute_mid_incident_pages_at_the_next_breach(store):
    await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
    with _slack() as mock_post:
        await _metric(True, 1_000.0, cooldown_sec=3600)
        await alert_mutes.unmute_alert_type("p95_latency")
        # Inside the cooldown the muted firing never armed.
        await _metric(True, 1_060.0, cooldown_sec=3600)
        closed = await _metric_incident_closes(1_060.0, cooldown_sec=3600)

    assert closed is True
    assert len(mock_post.await_args_list) == 2


async def test_the_stale_sweep_closes_a_muted_incident_silently(store, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])
    await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
    with _slack() as mock_post:
        await _metric(True, clock[0], stale_after=600.0)
        clock[0] += 601.0
        await sweep_stale_breaches()
        clock[0] += 601.0
        await sweep_stale_breaches()

    mock_post.assert_not_awaited()
    assert not _TRANSITIONS._firing
    assert not _MUTED_UNANNOUNCED


async def test_a_mute_under_the_snooze_still_closes_silently(store):
    """Muted while everything was snoozed: the channel never heard of it either way."""
    await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
    with (
        patch(
            "serving.observability.alert_snooze.is_snoozed",
            new=AsyncMock(return_value=True),
        ),
        _slack() as mock_post,
    ):
        await _metric(True, 1_000.0)
    await alert_mutes.unmute_alert_type("p95_latency")
    with _slack() as mock_post:
        closed = await _metric_incident_closes(1_000.0)

    assert closed is False
    mock_post.assert_not_awaited()


async def test_the_next_incident_on_the_key_starts_fresh(store):
    """A silent close must not leave the key silent, or announced, for the next one."""
    await alert_mutes.mute_alert_type("p95_latency", None, "admin@x.com")
    with _slack() as mock_post:
        await _metric(True, 1_000.0)
        await _metric_incident_closes(1_000.0)
        await alert_mutes.unmute_alert_type("p95_latency")

        await _metric(True, 2_000.0)
        closed = await _metric_incident_closes(2_000.0)

    assert closed is True
    assert len(mock_post.await_args_list) == 2
