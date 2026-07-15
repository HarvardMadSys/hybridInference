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
