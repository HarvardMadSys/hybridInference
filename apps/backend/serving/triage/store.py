"""SQLite persistence for incident dedupe and queued triage jobs."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from serving.triage.models import AlertEvent, TriageAnalysis

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class Incident:
    """Current state for one alert fingerprint."""

    fingerprint: str
    alert_id: str
    status: str
    slack_thread_ts: str
    codex_thread_id: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class TriageJob:
    """Persisted unit of analysis or Slack-result delivery work."""

    id: int
    fingerprint: str
    event: AlertEvent
    stage: str
    attempts: int
    result: TriageAnalysis | None
    codex_thread_id: str | None
    slack_thread_ts: str


class TriageStore:
    """Concurrency-safe async facade over a small SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        """Create tables and requeue work interrupted by a process restart."""
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    fingerprint TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    slack_thread_ts TEXT NOT NULL,
                    codex_thread_id TEXT,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS triage_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    slack_thread_ts TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'analysis',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    codex_thread_id TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS triage_jobs_ready
                    ON triage_jobs(status, id);
                """
            )
            connection.execute(
                "UPDATE triage_jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (time.time(),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a transactional connection and always close it on exit."""
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def get_incident(self, fingerprint: str) -> Incident | None:
        """Return the incident for a fingerprint, if one exists."""
        return await asyncio.to_thread(self._get_incident, fingerprint)

    def _get_incident(self, fingerprint: str) -> Incident | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, alert_id, status, slack_thread_ts, codex_thread_id,
                       created_at, updated_at
                FROM incidents
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return Incident(
            fingerprint=row["fingerprint"],
            alert_id=row["alert_id"],
            status=row["status"],
            slack_thread_ts=row["slack_thread_ts"],
            codex_thread_id=row["codex_thread_id"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def create_firing(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """Open or replace a resolved incident and enqueue its analysis."""
        await asyncio.to_thread(self._create_firing, event, slack_thread_ts)

    def _create_firing(self, event: AlertEvent, slack_thread_ts: str) -> None:
        now = time.time()
        event_json = event.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (
                    fingerprint, alert_id, status, slack_thread_ts,
                    codex_thread_id, event_json, created_at, updated_at
                ) VALUES (?, ?, 'firing', ?, NULL, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    alert_id = excluded.alert_id,
                    status = 'firing',
                    slack_thread_ts = excluded.slack_thread_ts,
                    codex_thread_id = NULL,
                    event_json = excluded.event_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    event.fingerprint,
                    event.alert_id,
                    slack_thread_ts,
                    event_json,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO triage_jobs (
                    fingerprint, event_json, slack_thread_ts, stage, status,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, 'analysis', 'queued', 0, ?, ?)
                """,
                (event.fingerprint, event_json, slack_thread_ts, now, now),
            )

    async def mark_resolved(self, fingerprint: str, event: AlertEvent) -> None:
        """Close an active incident after its recovery message is delivered."""
        await asyncio.to_thread(self._mark_resolved, fingerprint, event)

    def _mark_resolved(self, fingerprint: str, event: AlertEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE incidents
                SET status = 'resolved', event_json = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (event.model_dump_json(), time.time(), fingerprint),
            )

    async def claim_next_job(self) -> TriageJob | None:
        """Atomically claim the oldest queued job."""
        return await asyncio.to_thread(self._claim_next_job)

    def _claim_next_job(self) -> TriageJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, fingerprint, event_json, slack_thread_ts, stage, attempts,
                       result_json, codex_thread_id
                FROM triage_jobs
                WHERE status = 'queued'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE triage_jobs
                SET status = 'running', attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempts, time.time(), row["id"]),
            )
        result = (
            TriageAnalysis.model_validate_json(row["result_json"]) if row["result_json"] else None
        )
        return TriageJob(
            id=int(row["id"]),
            fingerprint=row["fingerprint"],
            event=AlertEvent.model_validate_json(row["event_json"]),
            stage=row["stage"],
            attempts=attempts,
            result=result,
            codex_thread_id=row["codex_thread_id"],
            slack_thread_ts=row["slack_thread_ts"],
        )

    async def save_analysis(
        self,
        job_id: int,
        fingerprint: str,
        alert_id: str,
        analysis: TriageAnalysis,
        codex_thread_id: str,
    ) -> None:
        """Persist an analysis before attempting its Slack delivery."""
        await asyncio.to_thread(
            self._save_analysis,
            job_id,
            fingerprint,
            alert_id,
            analysis,
            codex_thread_id,
        )

    def _save_analysis(
        self,
        job_id: int,
        fingerprint: str,
        alert_id: str,
        analysis: TriageAnalysis,
        codex_thread_id: str,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE triage_jobs
                SET stage = 'posting', status = 'queued', attempts = 0,
                    result_json = ?, codex_thread_id = ?, last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (analysis.model_dump_json(), codex_thread_id, now, job_id),
            )
            connection.execute(
                """
                UPDATE incidents
                SET codex_thread_id = ?, updated_at = ?
                WHERE fingerprint = ? AND alert_id = ?
                """,
                (codex_thread_id, now, fingerprint, alert_id),
            )

    async def complete_job(self, job_id: int) -> None:
        """Mark a result as delivered."""
        await asyncio.to_thread(self._complete_job, job_id)

    def _complete_job(self, job_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE triage_jobs
                SET status = 'done', last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), job_id),
            )

    async def retry_or_fail(self, job: TriageJob, error: str, max_attempts: int) -> bool:
        """Requeue a failed stage or mark it final; return True when final."""
        final = job.attempts >= max_attempts
        await asyncio.to_thread(self._retry_or_fail, job.id, error, final)
        return final

    def _retry_or_fail(self, job_id: int, error: str, final: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE triage_jobs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("failed" if final else "queued", error[:2_000], time.time(), job_id),
            )

    async def job_counts(self) -> dict[str, int]:
        """Return queue counts for health reporting."""
        return await asyncio.to_thread(self._job_counts)

    def _job_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM triage_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {status: counts.get(status, 0) for status in ("queued", "running", "done", "failed")}
