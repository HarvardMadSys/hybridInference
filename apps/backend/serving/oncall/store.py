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
    """Persisted unit of GitHub Actions hand-off work."""

    id: int
    fingerprint: str
    event: AlertEvent
    stage: str
    attempts: int
    slack_thread_ts: str
    # Delivery-confirmation bookkeeping, populated once the job parks in the
    # "confirm" stage: before_run_id is the newest Actions run id observed
    # before the dispatch, dispatched_at when the dispatch happened,
    # not_before when the next confirmation poll is due, and deadline by
    # which a run must have appeared.
    before_run_id: int | None = None
    dispatched_at: float | None = None
    not_before: float | None = None
    deadline: float | None = None
    last_error: str | None = None


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
                    not_before REAL,
                    deadline REAL,
                    before_run_id INTEGER,
                    dispatched_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS oncall_jobs_ready
                    ON oncall_jobs(status, id);
                """
            )
            self._ensure_columns(
                connection,
                "oncall_jobs",
                {
                    "not_before": "REAL",
                    "deadline": "REAL",
                    "before_run_id": "INTEGER",
                    "dispatched_at": "REAL",
                },
            )
            # Created after the column migration: on an existing volume the
            # columns do not exist yet when the schema script above runs.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS oncall_jobs_confirm "
                "ON oncall_jobs(status, stage, not_before)"
            )
            connection.execute(
                "UPDATE oncall_jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (time.time(),),
            )

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        """Add missing columns to an existing table in place.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``, so volumes created by an
        older relay are migrated column by column here instead of being
        dropped and rebuilt.
        """
        present = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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
                       before_run_id, dispatched_at, not_before, deadline, last_error
                FROM oncall_jobs
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
                UPDATE oncall_jobs
                SET status = 'running', attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempts, time.time(), row["id"]),
            )
        return self._job_from_row(row, attempts=attempts)

    async def start_confirm(
        self,
        job_id: int,
        *,
        not_before: float,
        deadline: float,
        before_run_id: int | None,
        dispatched_at: float,
    ) -> None:
        """Park a dispatched job in the confirm stage until a run appears."""
        await asyncio.to_thread(
            self._start_confirm, job_id, not_before, deadline, before_run_id, dispatched_at
        )

    def _start_confirm(
        self,
        job_id: int,
        not_before: float,
        deadline: float,
        before_run_id: int | None,
        dispatched_at: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
                SET stage = 'confirm', not_before = ?, deadline = ?, before_run_id = ?,
                    dispatched_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (not_before, deadline, before_run_id, dispatched_at, time.time(), job_id),
            )

    async def claim_next_confirm(self, now: float) -> OnCallJob | None:
        """Return the confirm-stage job whose next poll is due, if any.

        Poll scheduling is driven by ``not_before``: every call hands back the
        oldest parked job that is due at ``now``, so the worker needs no
        separate timer and a restart simply resumes polling.
        """
        return await asyncio.to_thread(self._claim_next_confirm, now)

    def _claim_next_confirm(self, now: float) -> OnCallJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, fingerprint, event_json, slack_thread_ts, stage, attempts,
                       before_run_id, dispatched_at, not_before, deadline, last_error
                FROM oncall_jobs
                WHERE status = 'running' AND stage = 'confirm' AND not_before <= ?
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE oncall_jobs SET updated_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
        return self._job_from_row(row)

    async def defer_confirm(self, job_id: int, *, not_before: float, error: str | None) -> None:
        """Reschedule a confirmation poll and record why the last one failed."""
        await asyncio.to_thread(self._defer_confirm, job_id, not_before, error)

    def _defer_confirm(self, job_id: int, not_before: float, error: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oncall_jobs
                SET not_before = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (not_before, error, time.time(), job_id),
            )

    @staticmethod
    def _job_from_row(row: sqlite3.Row, *, attempts: int | None = None) -> OnCallJob:
        """Build a job from a row that selected the full job column set.

        ``attempts`` defaults to the stored value; pass the post-increment
        value when the row was just claimed.
        """
        return OnCallJob(
            id=int(row["id"]),
            fingerprint=row["fingerprint"],
            event=AlertEvent.model_validate_json(row["event_json"]),
            stage=row["stage"],
            attempts=attempts if attempts is not None else int(row["attempts"]),
            slack_thread_ts=row["slack_thread_ts"],
            before_run_id=row["before_run_id"],
            dispatched_at=(
                float(row["dispatched_at"]) if row["dispatched_at"] is not None else None
            ),
            not_before=float(row["not_before"]) if row["not_before"] is not None else None,
            deadline=float(row["deadline"]) if row["deadline"] is not None else None,
            last_error=row["last_error"],
        )

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
            if final:
                connection.execute(
                    """
                    UPDATE oncall_jobs
                    SET status = 'failed', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:2_000], time.time(), job_id),
                )
            else:
                # Requeue for a fresh dispatch attempt. A job that timed out in
                # the confirm stage restarts from dispatch, since a second
                # repository_dispatch is the only way to get a new run.
                connection.execute(
                    """
                    UPDATE oncall_jobs
                    SET status = 'queued', stage = 'dispatch', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:2_000], time.time(), job_id),
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
