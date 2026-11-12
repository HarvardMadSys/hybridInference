"""Notification-policy restart reconstruction against PostgreSQL."""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from serving.observability import alert_policy
from serving.observability.alert_config import NotificationPolicy
from serving.observability.alert_history import hooks
from serving.observability.alert_history.capture import BackendIdentity
from serving.observability.alert_history.records import HistoryRecord, finalize
from serving.observability.alert_history.runtime import AlertHistoryRuntime
from serving.observability.alerts import AlertSeverity, alert_on_transition
from serving.storage import alert_history_store
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest]


@pytest_asyncio.fixture
async def pool():
    value = await postgres.create_pool(min_size=1, max_size=4)
    namespace = f"aptest-{uuid.uuid4().hex}"
    try:
        yield value, namespace
    finally:
        async with value.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_history_events WHERE source_namespace = $1", namespace
            )
            await conn.execute(
                "DELETE FROM alert_history_sources WHERE source_namespace = $1", namespace
            )
        await value.close()


def _record(
    namespace: str,
    *,
    key: str,
    kind: str,
    event_id: str,
    when: dt.datetime,
    episode_id: str,
    outcome: str | None = None,
    notification: str | None = None,
    tracker_kind: str | None = None,
):
    facts = {"notification": notification} if notification else {}
    if tracker_kind:
        facts["tracker_kind"] = tracker_kind
    return finalize(
        HistoryRecord(
            source="backend",
            source_namespace=namespace,
            source_instance_id="old-instance",
            process_id="old-process",
            producer_seq=None,
            origin_record_id=event_id,
            kind=kind,
            occurred_at=when,
            deployment_id="test-deployment",
            target_environment="test",
            origin="live",
            episode_id=episode_id,
            alert_type=key.split(":", 1)[0],
            alert_key=key,
            outcome=outcome,
            facts=facts,
        )
    )


async def test_preload_restores_open_delivered_incidents_and_their_recovery(pool, monkeypatch):
    db_pool, namespace = pool
    now = dt.datetime.now(dt.UTC)
    open_key, old_open_key, closed_key = (
        "circuit_open:open",
        "circuit_open:old-open",
        "circuit_open:closed",
    )
    records = [
        _record(
            namespace,
            key=open_key,
            kind="condition_triggered",
            event_id="open:start",
            when=now - dt.timedelta(minutes=20),
            episode_id="episode-open",
            tracker_kind="state",
        ),
        # An open incident may outlive the re-notify window. It still needs
        # its original lifecycle restored so a healthy edge after restart can
        # deliver the recovery without first sending a duplicate firing.
        _record(
            namespace,
            key=old_open_key,
            kind="condition_triggered",
            event_id="old-open:start",
            when=now - dt.timedelta(days=3),
            episode_id="episode-old-open",
            tracker_kind="state",
        ),
        _record(
            namespace,
            key=old_open_key,
            kind="delivery_result",
            event_id="old-open:delivery",
            when=now - dt.timedelta(days=3) + dt.timedelta(minutes=1),
            episode_id="episode-old-open",
            outcome="accepted",
            notification="firing",
        ),
        _record(
            namespace,
            key=open_key,
            kind="delivery_result",
            event_id="open:delivery",
            when=now - dt.timedelta(minutes=19),
            episode_id="episode-open",
            outcome="accepted",
            notification="firing",
        ),
        _record(
            namespace,
            key=closed_key,
            kind="condition_triggered",
            event_id="closed:start",
            when=now - dt.timedelta(minutes=10),
            episode_id="episode-closed",
            tracker_kind="state",
        ),
        _record(
            namespace,
            key=closed_key,
            kind="delivery_result",
            event_id="closed:delivery",
            when=now - dt.timedelta(minutes=9),
            episode_id="episode-closed",
            outcome="accepted",
            notification="firing",
        ),
        _record(
            namespace,
            key=closed_key,
            kind="condition_ended",
            event_id="closed:end",
            when=now - dt.timedelta(minutes=1),
            episode_id="episode-closed",
        ),
    ]
    async with db_pool.acquire() as conn, conn.transaction():
        await alert_history_store.write_records(conn, records)

    runtime = AlertHistoryRuntime(
        enabled=True,
        identity=BackendIdentity(
            namespace, "test-deployment", "test", "new-instance", "new-process", now
        ),
        db_config=None,
        fence_secret=postgres.FENCE_SECRET,
        pool=db_pool,
    )
    alert_policy.reset_notification_policy()
    alert_policy.apply_notification_policy(NotificationPolicy(enabled=True))
    hooks.install(runtime.capture)
    monkeypatch.setattr(alert_policy.time, "time", lambda: now.timestamp())
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/test")
    try:
        assert await alert_policy.preload_from_history(runtime) == 2
        assert alert_policy.hold_reason(open_key) == alert_policy.ALREADY_NOTIFIED
        assert alert_policy.hold_reason(old_open_key) == alert_policy.ALREADY_NOTIFIED
        assert alert_policy.hold_reason(closed_key) == alert_policy.BELOW_MIN_DURATION
        assert runtime.capture.open_episode(open_key) == "episode-open"
        assert runtime.capture.open_episode(old_open_key) == "episode-old-open"
        assert runtime.capture.open_episode(closed_key) is None
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as post:
            assert await alert_on_transition(
                key=old_open_key,
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
                now=now.timestamp(),
            )
        post.assert_awaited_once()
        assert "Recovered" in post.await_args.args[1]
        assert runtime.capture.open_episode(old_open_key) is None
    finally:
        hooks.install(None)
        alert_policy.reset_notification_policy()
