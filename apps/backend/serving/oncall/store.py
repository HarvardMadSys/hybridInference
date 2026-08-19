"""SQLite persistence for incident dedupe and queued oncall jobs."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from serving.oncall.models import AlertEvent

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
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class OnCallJob:
    """Persisted unit of analysis hand-off work.

    ``stage`` is ``dispatch`` until the hand-off succeeds. The GitHub backend
    ends there — the workflow owns the outcome. The cloud-agent backend moves
    the job to ``await_result``, where ``agent_job_id`` names the platform job
    the worker polls and ``deadline`` bounds how long it will keep polling.
    """

    id: int
    fingerprint: str
    event: AlertEvent
    stage: str
    attempts: int
    slack_thread_ts: str
    agent_job_id: str | None = None
    deadline: float | None = None


class OnCallStore:
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
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oncall_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    slack_thread_ts TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'dispatch',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    agent_job_id TEXT,
                    not_before REAL NOT NULL DEFAULT 0,
                    deadline REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS oncall_jobs_ready
                    ON oncall_jobs(status, id);
                """
            )
            self._ensure_columns(connection)
            connection.execute(
                "UPDATE oncall_jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (time.time(),),
            )

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        """Add the await-stage columns to a database created before them.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``, and the relay's state
        survives container rebuilds by design — the volume is the whole point
        — so an upgrade has to migrate in place rather than assume a fresh
        file.
        """
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(oncall_jobs)").fetchall()
        }
        additions = {
            "agent_job_id": "ALTER TABLE oncall_jobs ADD COLUMN agent_job_id TEXT",
            "not_before": "ALTER TABLE oncall_jobs ADD COLUMN not_before REAL NOT NULL DEFAULT 0",
            "deadline": "ALTER TABLE oncall_jobs ADD COLUMN deadline REAL",
        }
        for column, statement in additions.items():
            if column not in existing:
                connection.execute(statement)

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
                SELECT fingerprint, alert_id, status, slack_thread_ts,
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
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def create_firing(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """Open or replace a resolved incident and enqueue its hand-off."""
        await asyncio.to_thread(self._create_firing, event, slack_thread_ts)

    def _create_firing(self, event: AlertEvent, slack_thread_ts: str) -> None:
        now = time.time()
        event_json = event.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (
                    fingerprint, alert_id, status, slack_thread_ts,
                    event_json, created_at, updated_at
                ) VALUES (?, ?, 'firing', ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    alert_id = excluded.alert_id,
                    status = 'firing',
                    slack_thread_ts = excluded.slack_thread_ts,
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
            # One analysis in flight per fingerprint. The Actions backend got
            # this from its concurrency group — duplicates queued behind the
            # running analysis and then ran anyway, each re-answering the same
            # incident (nine near-identical analyses for one flapping circuit
            # on 2026-08-05). Skipping the enqueue is strictly better: the
            # alert itself was already posted above, and the analysis of the
            # first firing answers the burst.
            active = connection.execute(
                """
                SELECT 1 FROM oncall_jobs
                WHERE fingerprint = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (event.fingerprint,),
            ).fetchone()
            if active is None:
                connection.execute(
                    """
                    INSERT INTO oncall_jobs (
                        fingerprint, event_json, slack_thread_ts, stage, status,
                        attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, 'dispatch', 'queued', 0, ?, ?)
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
                SET alert_id = ?, status = 'resolved', event_json = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (event.alert_id, event.model_dump_json(), time.time(), fingerprint),
            )

    async def create_resolved(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """Persist a recovery that had no active incident to thread against."""
        await asyncio.to_thread(self._create_resolved, event, slack_thread_ts)

    def _create_resolved(self, event: AlertEvent, slack_thread_ts: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (
                    fingerprint, alert_id, status, slack_thread_ts,
                    event_json, created_at, updated_at
                ) VALUES (?, ?, 'resolved', ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    alert_id = excluded.alert_id,
                    status = 'resolved',
                    slack_thread_ts = excluded.slack_thread_ts,
                    event_json = excluded.event_json,
                    updated_at = excluded.updated_at
                """,
                (
                    event.fingerprint,
                    event.alert_id,
                    slack_thread_ts,
                    event.model_dump_json(),
                    now,
                    now,
                ),
            )

    async def claim_next_job(self) -> OnCallJob | None:
        """Atomically claim the oldest queued job."""
        return await asyncio.to_thread(self._claim_next_job)

    def _claim_next_job(self) -> OnCallJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, fingerprint, event_json, slack_thread_ts, stage, attempts,
                       agent_job_id, deadline
                FROM oncall_jobs
                WHERE status = 'queued' AND not_before <= ?
                ORDER BY id
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE oncall_jobs
                SET status = 'running', attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempts, time.time(), row["id"]),
            )
        return OnCallJob(
            id=int(row["id"]),
            fingerprint=row["fingerprint"],
            event=AlertEvent.model_validate_json(row["event_json"]),
            stage=row["stage"],
            attempts=attempts,
            slack_thread_ts=row["slack_thread_ts"],
            agent_job_id=row["agent_job_id"],
            deadline=float(row["deadline"]) if row["deadline"] is not None else None,
        )

    async def mark_awaiting(
        self,
        job_id: int,
        agent_job_id: str,
        *,
        not_before: float,
        deadline: float,
    ) -> None:
        """Park a dispatched job in the poll stage for its platform result.

        Attempts reset to zero: the dispatch stage spent its error budget
        getting the job created, and the await stage's budget is the
        ``deadline`` — transient poll failures reschedule rather than count.
        """
        await asyncio.to_thread(self._mark_awaiting, job_id, agent_job_id, not_before, deadline)

    def _mark_awaiting(
        self, job_id: int, agent_job_id: str, not_before: float, deadline: float
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
                SET stage = 'await_result', status = 'queued', attempts = 0,
                    agent_job_id = ?, not_before = ?, deadline = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (agent_job_id, not_before, deadline, time.time(), job_id),
            )

    async def defer_poll(self, job_id: int, *, not_before: float) -> None:
        """Requeue an awaiting job for its next poll without spending attempts."""
        await asyncio.to_thread(self._defer_poll, job_id, not_before)

    def _defer_poll(self, job_id: int, not_before: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
                SET status = 'queued', attempts = 0, not_before = ?, updated_at = ?
                WHERE id = ?
                """,
                (not_before, time.time(), job_id),
            )

    async def fail_job(self, job_id: int, error: str) -> None:
        """Mark a job finally failed, recording why."""
        await asyncio.to_thread(self._retry_or_fail, job_id, error, True)

    async def complete_job(self, job_id: int) -> None:
        """Mark a hand-off as delivered."""
        await asyncio.to_thread(self._complete_job, job_id)

    def _complete_job(self, job_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
                SET status = 'done', last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), job_id),
            )

    async def retry_or_fail(self, job: OnCallJob, error: str, max_attempts: int) -> bool:
        """Requeue a failed stage or mark it final; return True when final."""
        final = job.attempts >= max_attempts
        await asyncio.to_thread(self._retry_or_fail, job.id, error, final)
        return final

    def _retry_or_fail(self, job_id: int, error: str, final: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
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
                "SELECT status, COUNT(*) AS count FROM oncall_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {status: counts.get(status, 0) for status in ("queued", "running", "done", "failed")}
