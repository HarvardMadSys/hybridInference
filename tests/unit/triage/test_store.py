"""Tests for persistent incident and staged-job state."""

import sqlite3
from datetime import datetime, timezone

import pytest

from serving.triage.models import AlertEvent, TriageAnalysis
from serving.triage.store import TriageStore


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


def analysis() -> TriageAnalysis:
    return TriageAnalysis(
        summary="Likely upstream failure",
        classification="upstream_provider",
        confidence=0.8,
        impact="Requests may fail",
        evidence=["Alert names provider openai"],
        likely_cause="Provider outage",
        recommended_actions=["Check provider status"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )


def test_store_closes_connections_on_context_exit(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")

    with store._connect() as connection:
        connection.execute("CREATE TABLE connection_test (id INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


async def test_store_persists_analysis_before_posting(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")
    await store.initialize()
    await store.create_firing(event(), "171.1")

    incident = await store.get_incident(event().fingerprint)
    assert incident is not None
    assert incident.status == "firing"
    assert incident.slack_thread_ts == "171.1"

    job = await store.claim_next_job()
    assert job is not None
    assert job.stage == "analysis"
    await store.save_analysis(job.id, job.fingerprint, job.event.alert_id, analysis(), "thread-123")

    posting = await store.claim_next_job()
    assert posting is not None
    assert posting.stage == "posting"
    assert posting.result == analysis()
    assert posting.codex_thread_id == "thread-123"

    await store.complete_job(posting.id)
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}
