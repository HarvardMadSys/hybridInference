"""The administrator-activity monitor against real tables.

Seeds administrators' sign-ins and audited actions in a schema of their own
and runs :func:`serving.admin.admin_activity_monitor.run_monitor` with the
channel's post patched: what each card says, the ``admin_activity.alerted``
audit rows that record what was reported, and that each finding is reported
once, again after an undelivered card, and never while its type is muted.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from serving.admin import admin_activity_monitor as monitor
from serving.observability.alert_config import AdminActivity
from serving.observability.alerts import reset_dedupe_state, reset_transition_state
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

NOW = datetime.now(UTC).replace(microsecond=0)
HOME = "203.0.113.9"
NEW = "198.51.100.20"


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


async def _seed(db) -> None:
    await db.executemany(
        "INSERT INTO users (id, email, password_hash, role, status) "
        "VALUES ($1, $2, 'fixture', $3, 'active')",
        [
            ("root", "root@example.org", "admin"),
            ("ops", "ops@example.org", "admin"),
            ("u1", "u1@example.org", "free"),
        ],
    )
    await db.executemany(
        "INSERT INTO login_events (created_at, user_id, email, outcome, ip, user_agent) "
        "VALUES ($1, $2, $3, $4, $5, 'Mozilla/5.0')",
        [
            # root's usual address, in the lookback and in the window again.
            (ago(days=15), "root", "root@example.org", "success", HOME),
            (ago(minutes=30), "root", "root@example.org", "success", HOME),
            # root from a new address, after a failed attempt.
            (ago(minutes=22), "root", "root@example.org", "failure", NEW),
            (ago(minutes=20), "root", "root@example.org", "success", NEW),
            # Someone trying ops's password from an address ops never used.
            *[(ago(minutes=10), "ops", "ops@example.org", "failure", "192.0.2.50")] * 3,
            # Not an administrator: never reported.
            (ago(minutes=10), "u1", "u1@example.org", "success", "192.0.2.99"),
            # Long before the lookback: does not make NEW known.
            (ago(days=45), "root", "root@example.org", "success", NEW),
        ],
    )
    audit = [
        # The token's usual address, ten days ago.
        (ago(days=10), "admin_token", None, "192.0.2.1", "approve_user", None, True, None),
        # In the window.
        (ago(minutes=15), "admin", "root", NEW, "hard_delete_user", "u1", True, None),
        (
            ago(minutes=14),
            "admin",
            "root",
            NEW,
            "update_user",
            "u1",
            True,
            {"updated_fields": ["role"], "values": {"role": "admin"}},
        ),
        (
            ago(minutes=13),
            "admin",
            "root",
            HOME,
            "settings.update",
            None,
            True,
            {"key": "slack_webhook_url", "old_value": "a", "new_value": "b"},
        ),
        (ago(minutes=12), "admin", "root", HOME, "approve_user", "u1", True, None),
        (ago(minutes=11), "admin_token", None, "192.0.2.77", "config.restore", None, False, None),
        (ago(minutes=5), "admin", "ops", "192.0.2.60", "export_requests", None, True, None),
        # Not an administrator's action: never reported.
        (ago(minutes=4), "anonymous", None, "192.0.2.99", "create_user", "u1", True, None),
        (ago(minutes=3), "system", None, None, "hard_delete_user", "u1", True, None),
    ]
    await db.executemany(
        "INSERT INTO admin_audit_log "
        "(timestamp, admin_ip, actor_type, actor_id, source_ip, action, target_user_id, "
        " target_type, target_id, success, details) "
        "VALUES ($1, COALESCE($4, 'unknown'), $2, $3, $4, $5, $6, "
        " CASE WHEN $6::text IS NULL THEN NULL ELSE 'user' END, $6, $7, $8::jsonb)",
        [
            (at, actor_type, actor_id, ip, action, target, success, json.dumps(details))
            for at, actor_type, actor_id, ip, action, target, success, details in audit
        ],
    )


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    monkeypatch.setattr(monitor, "_SCANNED_THROUGH", {})
    reset_dedupe_state()
    reset_transition_state()
    schema = "admin_activity_" + uuid.uuid4().hex
    conn = await postgres.connect()
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as db:
            for table in ("users", "login_events", "admin_audit_log"):
                await db.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            await _seed(db)
        yield pool
    finally:
        await pool.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()
        reset_dedupe_state()
        reset_transition_state()


def _posted(post: AsyncMock) -> dict[str, str]:
    """The messages posted, keyed by the alert's title."""
    messages = {}
    for call in post.await_args_list:
        message = call.args[1]
        for title in monitor.ALERT_TITLES.values():
            if title in message:
                messages[title] = message
    return messages


async def _alerted_rows(pool, action: str = "admin_activity.alerted") -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor_type, target_type, target_id, details FROM admin_audit_log "
            "WHERE action = $1 ORDER BY target_id",
            action,
        )
    return [{**dict(row), "details": json.loads(row["details"])} for row in rows]


async def test_a_run_reports_new_addresses_and_dangerous_actions(pool):
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        reported = await monitor.run_monitor(pool, AdminActivity(), now=NOW)

    assert reported == {"admin_new_address": 4, "admin_dangerous_action": 5}
    messages = _posted(post)
    access = messages["Administrator access from a new address"]
    assert f"root@example.org from {NEW}: 1 sign-in, 1 failed sign-in attempt, 2 actions" in access
    assert "ops@example.org from 192.0.2.50: 3 failed sign-in attempts" in access
    assert "ops@example.org from 192.0.2.60: 1 action (export_requests)" in access
    assert "no sign-in from this address" in access
    assert "ADMIN_TOKEN from 192.0.2.77: 1 action (config.restore)" in access
    assert HOME not in access
    assert "u1@example.org" not in access

    actions = messages["Dangerous administrator actions"]
    assert f"root@example.org permanently deleted an account (u1@example.org) from {NEW}" in actions
    assert "root@example.org gave an account the admin role (u1@example.org)" in actions
    assert "root@example.org changed the setting SLACK_WEBHOOK_URL" in actions
    assert "ADMIN_TOKEN restored a configuration backup, refused, from 192.0.2.77" in actions
    assert "ops@example.org exported request logs from 192.0.2.60" in actions
    assert "approve_user" not in actions
    assert actions.count("permanently deleted") == 1  # the system's own row is not reported

    rows = await _alerted_rows(pool)
    assert [(row["actor_type"], row["target_type"], row["target_id"]) for row in rows] == [
        ("system", "alert_type", "admin_dangerous_action"),
        ("system", "alert_type", "admin_new_address"),
    ]
    through = (NOW - monitor.SETTLE).isoformat()
    assert {row["details"]["through"] for row in rows} == {through}
    assert {row["details"]["delivered"] for row in rows} == {True}
    assert [row["details"]["findings"] for row in rows] == [5, 4]
    # The first run ever records where monitoring started, an hour back.
    started = await _alerted_rows(pool, "admin_activity.started")
    assert [row["target_id"] for row in started] == ["admin_dangerous_action", "admin_new_address"]
    assert {row["details"]["through"] for row in started} == {
        (NOW - monitor.SETTLE - monitor.FIRST_WINDOW).isoformat()
    }


async def test_each_finding_is_reported_once_across_runs_and_processes(pool):
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)):
        await monitor.run_monitor(pool, AdminActivity(), now=NOW)

    # Another process (no local state) an hour later: the audit rows say where
    # the last report ended, and nothing new happened since.
    monitor._SCANNED_THROUGH.clear()
    reset_transition_state()
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        reported = await monitor.run_monitor(pool, AdminActivity(), now=NOW + timedelta(hours=1))

    assert reported == {"admin_new_address": 0, "admin_dangerous_action": 0}
    post.assert_not_awaited()
    assert len(await _alerted_rows(pool)) == 2


async def test_an_undelivered_card_is_reported_by_the_next_run(pool):
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=False)):
        first = await monitor.run_monitor(pool, AdminActivity(), now=NOW)

    assert first == {"admin_new_address": 0, "admin_dangerous_action": 0}
    assert await _alerted_rows(pool) == []

    # The next run lands on another process, which knows only the audit rows.
    monitor._SCANNED_THROUGH.clear()
    reset_dedupe_state()
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        second = await monitor.run_monitor(pool, AdminActivity(), now=NOW + timedelta(hours=1))

    assert second == {"admin_new_address": 4, "admin_dangerous_action": 5}
    assert post.await_count == 2
    rows = await _alerted_rows(pool)
    # The second run covered both hours: it started where the first one did.
    assert {row["details"]["since"] for row in rows} == {
        (NOW - monitor.SETTLE - monitor.FIRST_WINDOW).isoformat()
    }


async def test_a_muted_type_is_recorded_without_a_card(pool, monkeypatch):
    async def muted(alert_type: str) -> bool:
        return alert_type == monitor.DANGEROUS_ACTION_ALERT

    monkeypatch.setattr(monitor, "_type_muted", muted)
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        reported = await monitor.run_monitor(pool, AdminActivity(), now=NOW)

    assert reported == {"admin_new_address": 4, "admin_dangerous_action": 0}
    assert list(_posted(post)) == ["Administrator access from a new address"]
    rows = {row["target_id"]: row["details"] for row in await _alerted_rows(pool)}
    assert rows["admin_dangerous_action"]["delivered"] is False
    assert rows["admin_dangerous_action"]["reason"] == "muted"
    assert rows["admin_dangerous_action"]["findings"] == 5


async def test_a_shorter_lookback_forgets_old_addresses(pool):
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        await monitor.run_monitor(pool, AdminActivity(lookback_days=7), now=NOW)

    access = _posted(post)["Administrator access from a new address"]
    # root's usual address was last used 15 days before the window, outside a
    # week's lookback; the sign-in 30 minutes ago from it is new again.
    assert f"root@example.org from {HOME}: 1 sign-in" in access
    assert "an address the account had not used in the 7 days before" in access


async def test_a_second_replica_waits_for_the_lock(pool):
    async with pool.acquire() as holder:
        await holder.fetchval(monitor._LOCK_SQL)
        try:
            assert await monitor.run_monitor(pool, AdminActivity(), now=NOW) is None
        finally:
            await holder.execute(monitor._UNLOCK_SQL)
