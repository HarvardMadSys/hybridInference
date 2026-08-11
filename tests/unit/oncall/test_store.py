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


def firing(alert_id: str) -> AlertEvent:
    return event().model_copy(update={"alert_id": alert_id})


async def test_store_single_flight_per_fingerprint(tmp_path):
    """A second firing while an analysis is active does not enqueue another."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()

    await store.create_firing(firing("alert-1"), "171.1")
    await store.create_firing(firing("alert-2"), "171.2")
    counts = await store.job_counts()
    assert counts["queued"] == 1

    job = await store.claim_next_job()
    assert job is not None
    # Still active while claimed/running: no new enqueue either.
    await store.create_firing(firing("alert-3"), "171.3")
    assert (await store.job_counts())["running"] == 1
    assert (await store.job_counts())["queued"] == 0

    await store.complete_job(job.id)
    # With the active job settled, the next firing gets its own analysis.
    await store.create_firing(firing("alert-4"), "171.4")
    assert (await store.job_counts())["queued"] == 1
    # The incident row still tracked every occurrence along the way.
    incident = await store.get_incident(event().fingerprint)
    assert incident is not None and incident.alert_id == "alert-4"


async def test_store_awaiting_lifecycle(tmp_path):
    """mark_awaiting parks the job; not_before gates the next claim."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    job = await store.claim_next_job()
    assert job is not None and job.agent_job_id is None

    await store.mark_awaiting(job.id, "ajob_0007", not_before=0.0, deadline=9e12)
    awaiting = await store.claim_next_job()
    assert awaiting is not None
    assert awaiting.stage == "await_result"
    assert awaiting.agent_job_id == "ajob_0007"
    assert awaiting.deadline == 9e12
    # Attempts reset at the stage boundary: the await stage's budget is the
    # deadline, not the dispatch stage's leftover error count.
    assert awaiting.attempts == 1

    # A deferred poll is invisible until its time arrives.
    await store.defer_poll(awaiting.id, not_before=9e12)
    assert await store.claim_next_job() is None
    await store.defer_poll(awaiting.id, not_before=0.0)
    again = await store.claim_next_job()
    assert again is not None and again.id == awaiting.id

    await store.fail_job(again.id, "deadline exceeded")
    assert (await store.job_counts())["failed"] == 1


async def test_store_migrates_pre_await_schema(tmp_path):
    """A database created before the await stage gains its columns in place."""
    path = tmp_path / "oncall.sqlite3"
    connection = sqlite3.connect(path)
    with connection:
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
                fingerprint, event_json, slack_thread_ts, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 0)
            """,
            (event().fingerprint, event().model_dump_json(), "171.1"),
        )
    connection.close()

    store = OnCallStore(path)
    await store.initialize()

    # The pre-upgrade row is claimable (not_before defaulted to 0) and the
    # await-stage methods work against it.
    job = await store.claim_next_job()
    assert job is not None and job.agent_job_id is None and job.deadline is None
    await store.mark_awaiting(job.id, "ajob_0001", not_before=0.0, deadline=9e12)
    awaiting = await store.claim_next_job()
    assert awaiting is not None and awaiting.agent_job_id == "ajob_0001"
