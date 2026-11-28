"""Unit tests for the hourly administrator-activity monitor's pure parts.

What counts as a new address, which audited actions are dangerous, how a card
reads, and that a card posts once and closes without a recovery message. The
SQL, the watermark and the exactly-once reporting are covered by
``tests/integration/test_admin_activity_monitor_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.admin import admin_activity_monitor as monitor
from serving.admin.admin_activity_monitor import (
    DANGEROUS_ACTIONS,
    Finding,
    card,
    dangerous_description,
    dangerous_findings,
    new_address_findings,
)
from serving.observability.alert_config import AdminActivity, alert_config_from_data
from serving.observability.alerts import (
    _STATE_TRANSITIONS,
    reset_dedupe_state,
    reset_transition_state,
)

AT = datetime(2026, 11, 28, 13, 5, tzinfo=UTC)
ROOT = "user:root"


@pytest.fixture(autouse=True)
def _clean_alert_state(monkeypatch):
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    reset_dedupe_state()
    reset_transition_state()
    yield
    reset_dedupe_state()
    reset_transition_state()


def _sign_in(ip: str, *, minutes: int = 0, outcome: str = "success", **row: Any) -> dict:
    return {
        "who": ROOT,
        "label": "root@example.org",
        "ip": ip,
        "at": AT + timedelta(minutes=minutes),
        "outcome": outcome,
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)",
        **row,
    }


def _action(ip: str | None, action: str, *, minutes: int = 0, **row: Any) -> dict:
    return {
        "who": ROOT,
        "label": "root@example.org",
        "ip": ip,
        "at": AT + timedelta(minutes=minutes),
        "action": action,
        **row,
    }


# --- new addresses --------------------------------------------------------------


def test_a_sign_in_from_an_address_the_account_used_is_not_new() -> None:
    assert new_address_findings([_sign_in("203.0.113.9")], [], {ROOT: {"203.0.113.9"}}) == []


def test_a_sign_in_from_a_new_address_is_one_finding_with_its_attempts() -> None:
    findings = new_address_findings(
        [
            _sign_in("198.51.100.20", minutes=2),
            _sign_in("198.51.100.20", outcome="failure"),
            _sign_in("203.0.113.9", minutes=1),
        ],
        [],
        {ROOT: {"203.0.113.9"}},
    )

    assert findings == [
        Finding(
            AT,
            "root@example.org from 198.51.100.20: 1 sign-in, 1 failed sign-in attempt; "
            "first at 2026-11-28 13:05 UTC; Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)",
        )
    ]


def test_failed_attempts_alone_are_reported() -> None:
    [finding] = new_address_findings(
        [_sign_in("192.0.2.50", outcome="failure") for _ in range(3)], [], {}
    )

    assert finding.text.startswith("root@example.org from 192.0.2.50: 3 failed sign-in attempts;")


def test_a_hosts_rotating_ipv6_addresses_are_one_address() -> None:
    known = {ROOT: {"2a00:1450:4001:81c::/64"}}

    assert new_address_findings([_sign_in("2a00:1450:4001:81c::beef")], [], known) == []
    [finding] = new_address_findings([_sign_in("2a00:1450:4001:999::1")], [], known)
    assert finding.text.startswith(
        "root@example.org from 2a00:1450:4001:999::1 (2a00:1450:4001:999::/64): 1 sign-in;"
    )


def test_a_session_acting_from_an_address_with_no_sign_in_is_reported() -> None:
    [finding] = new_address_findings(
        [],
        [
            _action("192.0.2.60", "system.restart", minutes=4),
            _action("192.0.2.60", "config.update"),
            _action("192.0.2.60", "config.update", minutes=1),
        ],
        {ROOT: {"203.0.113.9"}},
    )

    assert finding.text == (
        "root@example.org from 192.0.2.60: 3 actions (config.update, system.restart); "
        "first at 2026-11-28 13:05 UTC; no sign-in from this address"
    )


def test_the_admin_token_from_a_new_address_is_reported() -> None:
    token = {"who": "token", "label": "ADMIN_TOKEN"}
    [finding] = new_address_findings(
        [],
        [_action("192.0.2.77", "config.restore", **token), _action("192.0.2.1", "x", **token)],
        {"token": {"192.0.2.1"}},
    )

    assert finding.text == (
        "ADMIN_TOKEN from 192.0.2.77: 1 action (config.restore); first at 2026-11-28 13:05 UTC"
    )


def test_an_unrecorded_address_is_never_new() -> None:
    assert new_address_findings([_sign_in("unknown")], [_action(None, "delete_user")], {}) == []


def test_each_account_and_address_is_its_own_finding() -> None:
    ops = {"who": "user:ops", "label": "ops@example.org"}
    findings = new_address_findings(
        [_sign_in("192.0.2.50", minutes=1), _sign_in("192.0.2.50", **ops)],
        [_action("192.0.2.51", "delete_user", minutes=2)],
        {},
    )

    assert [finding.text.split(":")[0] for finding in findings] == [
        "ops@example.org from 192.0.2.50",
        "root@example.org from 192.0.2.50",
        "root@example.org from 192.0.2.51",
    ]


# --- dangerous actions ----------------------------------------------------------


@pytest.mark.parametrize(("action", "description"), sorted(DANGEROUS_ACTIONS.items()))
def test_each_listed_action_is_dangerous_whatever_its_details(action, description) -> None:
    assert dangerous_description(action, None) == description


@pytest.mark.parametrize(
    ("details", "description"),
    [
        ({"values": {"role": "admin"}}, "gave an account the admin role"),
        ({"values": {"role": "internal"}}, "gave an account the internal role"),
        ({"values": {"role": "admin"}, "previous_role": "pro"}, "gave an account the admin role"),
        # A save that sent the role the account already had grants nothing.
        ({"values": {"role": "admin"}, "previous_role": "admin"}, None),
        ({"values": {"role": "pro"}}, None),
        ({"values": {"status": "suspended"}}, None),
        (None, None),
    ],
)
def test_granting_a_privileged_role_is_dangerous(details, description) -> None:
    assert dangerous_description("update_user", details) == description


@pytest.mark.parametrize(
    ("action", "details", "description"),
    [
        ("config.update", {"key": "SLACK_WEBHOOK_URL"}, "changed the setting SLACK_WEBHOOK_URL"),
        ("settings.update", {"key": "signup_enabled"}, "changed the setting SIGNUP_ENABLED"),
        ("config.reset", {"key": "trusted_proxies"}, "reset the setting TRUSTED_PROXIES"),
        (
            "config.update",
            {"key": "API_KEY_PEPPER", "changed": True},
            "changed the setting API_KEY_PEPPER",
        ),
        ("config.update", {"key": "MODEL_CATALOG", "old_value": 1, "new_value": 2}, None),
        ("settings.update", {"key": "batch_max_concurrency"}, None),
    ],
)
def test_changing_a_sensitive_or_secret_setting_is_dangerous(action, details, description) -> None:
    assert dangerous_description(action, details) == description


@pytest.mark.parametrize("action", ["approve_user", "create_key", "site_update_create"])
def test_routine_actions_are_not_dangerous(action) -> None:
    assert dangerous_description(action, {"values": {"role": "admin"}}) is None


def test_dangerous_findings_read_as_who_did_what_from_where() -> None:
    rows = [
        {
            "at": AT + timedelta(minutes=10),
            "action": "approve_user",
            "actor_label": "root@example.org",
            "ip": "203.0.113.9",
            "target_label": "u1@example.org",
            "success": True,
            "details": None,
        },
        {
            "at": AT + timedelta(minutes=5),
            "action": "hard_delete_user",
            "actor_label": "root@example.org",
            "ip": "203.0.113.9",
            "target_label": "u1@example.org",
            "success": True,
            "details": None,
        },
        {
            "at": AT,
            "action": "alerts.mute",
            "actor_label": "ADMIN_TOKEN",
            "ip": None,
            "target_label": "alert_type admin_new_address",
            "success": False,
            "details": None,
        },
    ]

    assert [finding.text for finding in dangerous_findings(rows)] == [
        "ADMIN_TOKEN muted an alert type (alert_type admin_new_address), refused, "
        "from an unrecorded address at 2026-11-28 13:05 UTC",
        "root@example.org permanently deleted an account (u1@example.org) "
        "from 203.0.113.9 at 2026-11-28 13:10 UTC",
    ]


# --- the card ---------------------------------------------------------------------


def test_a_card_lists_its_window_and_findings() -> None:
    findings = [Finding(AT, "first"), Finding(AT, "second")]

    assert card(findings, since=AT, until=AT + timedelta(hours=1), lookback_days=30) == {
        "window": "2026-11-28 13:05 UTC to 2026-11-28 14:05 UTC",
        "new": "an address the account had not used in the 30 days before",
        "1": "first",
        "2": "second",
    }


def test_a_long_card_counts_what_it_does_not_list() -> None:
    findings = [Finding(AT, f"line {i}") for i in range(monitor.MAX_LISTED + 3)]

    context = card(findings, since=AT, until=AT)

    assert str(monitor.MAX_LISTED) in context
    assert str(monitor.MAX_LISTED + 1) not in context
    assert context["more"] == "3 more: see the admin audit log and sign-in records"
    assert "new" not in context


@pytest.mark.parametrize("alert_type", [monitor.NEW_ADDRESS_ALERT, monitor.DANGEROUS_ACTION_ALERT])
async def test_a_card_posts_once_and_closes_without_a_recovery(monkeypatch, alert_type) -> None:
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    stamp = "2026-11-28T14:06:00+00:00"
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        sent = await monitor._send(alert_type, stamp, {"1": "root@example.org from 192.0.2.50"})

    assert sent is True
    post.assert_awaited_once()
    message = post.await_args.args[1]
    assert monitor.ALERT_TITLES[alert_type] in message
    assert "192.0.2.50" in message
    assert "Recovered" not in message
    assert not _STATE_TRANSITIONS.is_firing(f"{alert_type}:{stamp}")


async def test_an_undelivered_card_says_so(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=False)):
        sent = await monitor._send(monitor.DANGEROUS_ACTION_ALERT, "stamp", {"1": "x"})

    assert sent is False


# --- running and scheduling -------------------------------------------------------


async def test_an_off_or_channel_less_monitor_reads_nothing(monkeypatch) -> None:
    pool = MagicMock()
    pool.acquire.side_effect = AssertionError("the monitor must not read the database")

    assert await monitor.run_monitor(pool, AdminActivity(enabled=False)) == {}
    assert await monitor.run_monitor(pool, AdminActivity()) == {}  # no channel configured
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    assert await monitor.run_monitor(pool, AdminActivity(enabled=False)) == {}


def test_the_job_runs_hourly_unless_the_rules_turn_it_off() -> None:
    scheduler = MagicMock()

    monitor.register_monitor_job(scheduler, "pool", AdminActivity(lookback_days=7))
    monitor.register_monitor_job(scheduler, "pool", AdminActivity(enabled=False))

    scheduler.add_job.assert_called_once()
    call = scheduler.add_job.call_args
    assert call.args[0] is monitor.monitor_job
    assert call.kwargs["args"] == ["pool", AdminActivity(lookback_days=7)]
    assert call.kwargs["id"] == "admin_activity_monitor"
    assert str(call.kwargs["trigger"].fields[6]) == str(monitor.RUN_MINUTE)  # minute field


def test_the_rules_document_configures_the_monitor() -> None:
    assert alert_config_from_data({}).admin_activity == AdminActivity()
    configured = alert_config_from_data({"admin_activity": {"lookback_days": 7}})
    assert configured.admin_activity == AdminActivity(enabled=True, lookback_days=7)
    with patch("serving.config.documents.get", return_value={"admin_activity": {"enabled": False}}):
        assert monitor.load_config() == AdminActivity(enabled=False)
    with patch("serving.config.documents.get", side_effect=RuntimeError("no document")):
        assert monitor.load_config() == AdminActivity()


async def test_the_scheduled_run_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "run_monitor", AsyncMock(side_effect=RuntimeError("db down")))

    await monitor.monitor_job("pool", AdminActivity())
