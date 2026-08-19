"""Tests for persistent incident and queued hand-off state."""

import sqlite3
from datetime import datetime, timezone

import pytest

from serving.oncall.models import AlertEvent
from serving.oncall.store import OnCallStore


def event() -> AlertEvent:
    return AlertEvent(
        alert_id="alert-1",
        fingerprint="source:production:test",
        source="test",
        status="firing",
        severity="error",
        title="Failure",
        environment="production",
        occurred_at=datetime.now(timezone.utc),
        summary="Failure",
        context={"provider": "openai"},
        slack_text="Failure",
    )


def test_store_closes_connections_on_context_exit(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")

    with store._connect() as connection:
        connection.execute("CREATE TABLE connection_test (id INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


async def test_store_queues_dispatch_and_completes(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    incident = await store.get_incident(event().fingerprint)
    assert incident is not None
    assert incident.status == "firing"
    assert incident.slack_thread_ts == "171.1"

    job = await store.claim_next_job()
    assert job is not None
    assert job.stage == "dispatch"
    assert job.attempts == 1
    assert job.slack_thread_ts == "171.1"
    assert job.event.alert_id == "alert-1"

    await store.complete_job(job.id)
    assert await store.claim_next_job() is None
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}


async def test_store_requeues_then_finalizes_failed_dispatch(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    job = await store.claim_next_job()
    assert job is not None
    assert await store.retry_or_fail(job, "github 500", max_attempts=2) is False

    retried = await store.claim_next_job()
    assert retried is not None
    assert retried.attempts == 2
    assert await store.retry_or_fail(retried, "github 500", max_attempts=2) is True

    assert await store.claim_next_job() is None
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 0, "failed": 1}


async def test_store_migrates_an_existing_volume_in_place(tmp_path):
    path = tmp_path / "oncall.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE incidents (
            fingerprint TEXT PRIMARY KEY,
            alert_id TEXT NOT NULL,
            status TEXT NOT NULL,
            slack_thread_ts TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE oncall_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            event_json TEXT NOT NULL,
            slack_thread_ts TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'dispatch',
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO oncall_jobs (
            fingerprint, event_json, slack_thread_ts, stage, status, attempts,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'dispatch', 'queued', 0, 1.0, 1.0)
        """,
        (event().fingerprint, event().model_dump_json(), "171.1"),
    )
    connection.commit()
    connection.close()

    store = OnCallStore(path)
    await store.initialize()

    job = await store.claim_next_job()
    assert job is not None
    assert job.stage == "dispatch"
    assert job.before_run_id is None
    await store.start_confirm(
        job.id, not_before=2.0, deadline=5.0, before_run_id=7, dispatched_at=1.0
    )
    parked = await store.claim_next_confirm(2.0)
    assert parked is not None
    assert parked.before_run_id == 7
    assert parked.dispatched_at == 1.0
    await store.complete_job(parked.id)
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}


async def test_store_parks_dispatched_job_until_confirmation_is_due(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    job = await store.claim_next_job()
    assert job is not None
    assert job.before_run_id is None and job.deadline is None

    await store.start_confirm(
        job.id, not_before=5.0, deadline=10.0, before_run_id=41, dispatched_at=1.0
    )
    assert await store.claim_next_confirm(4.9) is None

    parked = await store.claim_next_confirm(5.0)
    assert parked is not None
    assert parked.stage == "confirm"
    assert parked.attempts == 1
    assert parked.before_run_id == 41
    assert parked.dispatched_at == 1.0
    assert parked.not_before == 5.0
    assert parked.deadline == 10.0

    await store.complete_job(parked.id)
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}


async def test_store_defers_confirmation_poll_and_requeues_on_timeout(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    job = await store.claim_next_job()
    assert job is not None
    await store.start_confirm(
        job.id, not_before=0.0, deadline=10.0, before_run_id=41, dispatched_at=1.0
    )

    parked = await store.claim_next_confirm(0.0)
    assert parked is not None
    await store.defer_confirm(parked.id, not_before=15.0, error="poll failed: timeout")
    assert await store.claim_next_confirm(14.9) is None

    redeferred = await store.claim_next_confirm(15.0)
    assert redeferred is not None
    assert redeferred.last_error == "poll failed: timeout"

    # Deadline passed without a run: the retry path returns the job to the
    # dispatch queue so a fresh repository_dispatch can be attempted.
    assert await store.retry_or_fail(redeferred, "no run appeared", max_attempts=2) is False
    redispatched = await store.claim_next_job()
    assert redispatched is not None
    assert redispatched.stage == "dispatch"
    assert redispatched.attempts == 2
    assert await store.retry_or_fail(redispatched, "no run appeared", max_attempts=2) is True
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 0, "failed": 1}
