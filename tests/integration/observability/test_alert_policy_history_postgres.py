"""Notification-policy restart reconstruction against PostgreSQL."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from serving.observability import alert_policy, alerts as alerts_module
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
    stale_after_sec: float | None = None,
):
    facts = {"notification": notification} if notification else {}
    if tracker_kind:
        facts["tracker_kind"] = tracker_kind
    if stale_after_sec is not None:
        facts["stale_after_sec"] = stale_after_sec
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
    metric_key = "failed_request_rate_db"
    records = [
        _record(
            namespace,
            key=metric_key,
            kind="condition_triggered",
            event_id="metric:start",
            when=now - dt.timedelta(minutes=20),
            episode_id="episode-metric",
            tracker_kind="metric",
            stale_after_sec=1200.0,
        ),
        _record(
            namespace,
            key=metric_key,
            kind="delivery_result",
            event_id="metric:delivery",
            when=now - dt.timedelta(minutes=19),
            episode_id="episode-metric",
            outcome="accepted",
            notification="firing",
        ),
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
        assert await alert_policy.preload_from_history(runtime) == 3
        assert alert_policy.hold_reason(open_key) == alert_policy.ALREADY_NOTIFIED
        assert alert_policy.hold_reason(old_open_key) == alert_policy.ALREADY_NOTIFIED
        assert alert_policy.hold_reason(closed_key) == alert_policy.BELOW_MIN_DURATION
        assert runtime.capture.open_episode(open_key) == "episode-open"
        assert runtime.capture.open_episode(old_open_key) == "episode-old-open"
        assert runtime.capture.open_episode(closed_key) is None
        assert alerts_module._TRANSITIONS.sweep(now=now.timestamp() + 1199) == []
        assert alerts_module._TRANSITIONS.sweep(now=now.timestamp() + 1200) == [metric_key]
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


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


async def test_preload_uses_scoped_indexes_on_substantial_history(pool):
    """The startup query stays selective as unrelated history accumulates."""
    db_pool, namespace = pool
    async with db_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO alert_history_events (
                record_id, source, source_namespace, origin_record_id, payload_digest,
                schema_version, deployment_id, target_environment, process_id, episode_id,
                occurred_at, kind, origin, alert_type, alert_key
            )
            SELECT md5('preload-bg-' || n)::uuid, 'backend', $1,
                   'preload-bg-' || n, md5('preload-bg-payload-' || n), 1,
                   'unrelated-deployment', 'test', 'synthetic-process',
                   'unrelated-episode-' || n,
                   NOW() - (n::text || ' seconds')::interval,
                   'condition_triggered', 'live', 'synthetic', 'unrelated-' || n
            FROM generate_series(1, 30000) AS n
            """,
            namespace,
        )
        await conn.execute(
            """
            INSERT INTO alert_history_events (
                record_id, source, source_namespace, origin_record_id, payload_digest,
                schema_version, deployment_id, target_environment, process_id, episode_id,
                occurred_at, kind, origin, alert_type, alert_key, facts
            )
            SELECT md5('preload-open-' || n)::uuid, 'backend', $1,
                   'preload-open-' || n, md5('preload-open-payload-' || n), 1,
                   'test-deployment', 'test', 'synthetic-process',
                   'open-episode-' || n,
                   CASE WHEN n = 1 THEN TIMESTAMPTZ '2000-01-01 00:00:00+00'
                        ELSE NOW() - INTERVAL '2 minutes' END,
                   'condition_triggered', 'live', 'synthetic', 'open-' || n,
                   jsonb_build_object('tracker_kind', 'state')
            FROM generate_series(1, 250) AS n
            """,
            namespace,
        )
        await conn.execute(
            """
            INSERT INTO alert_history_events (
                record_id, source, source_namespace, origin_record_id, payload_digest,
                schema_version, deployment_id, target_environment, process_id, episode_id,
                occurred_at, kind, origin, alert_type, alert_key, facts, outcome
            )
            SELECT md5('preload-delivered-' || n)::uuid, 'backend', $1,
                   'preload-delivered-' || n, md5('preload-delivered-payload-' || n), 1,
                   'test-deployment', 'test', 'synthetic-process',
                   'open-episode-' || n,
                   CASE WHEN n = 1 THEN TIMESTAMPTZ '2000-01-01 00:01:00+00'
                        ELSE NOW() - INTERVAL '1 minute' END,
                   'delivery_result', 'live', 'synthetic', 'open-' || n,
                   jsonb_build_object('notification', 'firing'), 'accepted'
            FROM generate_series(1, 250) AS n
            """,
            namespace,
        )
        await conn.execute("ANALYZE alert_history_events")

        explanation = await conn.fetchval(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + alert_policy._PRELOAD_SQL,
            "backend",
            namespace,
            "test-deployment",
            "test",
        )
        plan_document = json.loads(explanation) if isinstance(explanation, str) else explanation
        nodes = list(_plan_nodes(plan_document[0]["Plan"]))
        indexes = {node.get("Index Name") for node in nodes}
        restored = await conn.fetch(
            alert_policy._PRELOAD_SQL,
            "backend",
            namespace,
            "test-deployment",
            "test",
        )

    assert len(restored) == 250
    assert any(
        row["alert_key"] == "open-1" and row["opened_at"] < 1_000_000_000 for row in restored
    )
    assert "idx_alert_history_events_preload_condition" in indexes
    assert "idx_alert_history_events_preload_delivery" in indexes
