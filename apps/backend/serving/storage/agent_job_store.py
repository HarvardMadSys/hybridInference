"""Persistence for cloud agent-sandbox jobs (issue #1041, P0).

A small, self-contained store over the same Postgres pool used by the rest of
the gateway — kept separate from the (cached, auth-hot) ``OperationalStore``
contract, following the ``ResponseStore`` precedent.

Design (adjudicated in issue #1041):

- **State triad separation** — the sandbox/machine can vanish at any time;
  jobs, attempts, and events live here independently.
- **An expired lease is dead immediately**, not once the reaper notices. Every
  fenced write requires ``lease_expires_at > NOW()`` as well as a matching
  generation, so the window between expiry and the reaper's next pass is not a
  window in which a stalled worker can still write, renew, or finish.
- **Attempts + fencing tokens** — each grant of ownership is a new
  ``agent_attempts`` row with a monotonically increasing ``lease_generation``.
  Every write from a worker (heartbeat, events, artifacts, state transitions,
  publish) is compare-and-swapped against ``(attempt_id, lease_generation,
  status='running')`` so a zombie worker whose lease was reaped can never
  write again — the split-brain scenario is structurally impossible.
- **Reaper closes, never resets** — an expired attempt is marked
  ``superseded`` (with an ``attempt_superseded`` control event) and the job is
  requeued as a *new* attempt; history is append-only.
- **Publish is the critical fencing point** — the only external side effect.
  ``running -> publishing`` is a fenced one-shot transition and
  ``complete_publish`` additionally requires ``published_pr_url IS NULL``, so
  at most one attempt can ever publish. A job whose lease expires while
  ``publishing`` is failed (not requeued): the publisher may already have
  created the branch/PR, and re-publishing automatically could duplicate it.
- **Events** carry a global ``BIGSERIAL`` id (the SSE ``Last-Event-ID``
  cursor) plus a per-attempt ``seq`` with ``UNIQUE(attempt_id, seq)``. UI
  rewind after a retry is an ``attempt_superseded`` control event plus the new
  attempt's events — old events are never deleted or rewritten.
"""

from __future__ import annotations

import json
import secrets
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

# Job lifecycle. ``cancel_requested`` is a flag orthogonal to ``state``: it is
# delivered to the running worker via the heartbeat response, and the worker
# performs the fenced transition to ``cancelled``.
QUEUED = "queued"
RUNNING = "running"
PUBLISHING = "publishing"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = (SUCCEEDED, FAILED, CANCELLED)

# Attempt lifecycle.
ATTEMPT_RUNNING = "running"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_FINISHED = "finished"

# Control event appended by the reaper when it supersedes an attempt.
EVENT_ATTEMPT_SUPERSEDED = "attempt_superseded"


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to decode agent job JSON column", exc_info=True)
            return None
    return value


def _job_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an ``agent_jobs`` row to a plain dict."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "repo": row["repo"],
        "base_sha": row["base_sha"],
        "task_prompt": row["task_prompt"],
        "runtime": row["runtime"],
        "model": row["model"],
        "state": row["state"],
        "cancel_requested": row["cancel_requested"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "detail": row["detail"],
        "budget_usd": float(row["budget_usd"]) if row["budget_usd"] is not None else None,
        "metadata": _load_json(row["metadata"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_JOB_COLUMNS = (
    "id, user_id, repo, base_sha, task_prompt, runtime, model, state, "
    "cancel_requested, current_attempt_id, published_pr_url, detail, budget_usd, metadata, "
    "created_at, updated_at"
)


class AgentJobStore:
    """Postgres-backed store for agent jobs, attempts, events, and artifacts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap a shared asyncpg pool."""
        self._pool = pool

    async def initialize(self) -> None:
        """Create the agent job tables and indexes (idempotent)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    base_sha TEXT,
                    task_prompt TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    current_attempt_id BIGINT,
                    published_pr_url TEXT,
                    detail TEXT,
                    budget_usd NUMERIC(12, 6),
                    metadata JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            # Idempotent column migrations for databases created by an earlier
            # revision (CREATE TABLE IF NOT EXISTS never adds columns).
            await conn.execute(
                "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS budget_usd NUMERIC(12, 6)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_jobs_queued "
                "ON agent_jobs(created_at) WHERE state = 'queued'"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_jobs_user "
                "ON agent_jobs(user_id, created_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES agent_jobs(id),
                    attempt_no INTEGER NOT NULL,
                    lease_owner TEXT NOT NULL,
                    lease_generation BIGINT NOT NULL,
                    lease_expires_at TIMESTAMPTZ NOT NULL,
                    sandbox_id TEXT,
                    base_sha TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    UNIQUE (job_id, attempt_no)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_attempts_expiry "
                "ON agent_attempts(lease_expires_at) WHERE status = 'running'"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_job_events (
                    id BIGSERIAL PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    attempt_id BIGINT NOT NULL REFERENCES agent_attempts(id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (attempt_id, seq)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_job_events_job "
                "ON agent_job_events(job_id, id)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_job_artifacts (
                    id BIGSERIAL PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    attempt_id BIGINT NOT NULL REFERENCES agent_attempts(id),
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (job_id, attempt_id, kind)
                )
                """
            )

    # ── Jobs ───────────────────────────────────────────────────────────

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str,
        task_prompt: str,
        runtime: str,
        model: str,
        base_sha: str | None = None,
        budget_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it."""
        job_id = _new_job_id()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_jobs
                    (id, user_id, repo, base_sha, task_prompt, runtime, model,
                     budget_usd, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                RETURNING {_JOB_COLUMNS}
                """,
                job_id,
                user_id,
                repo,
                base_sha,
                task_prompt,
                runtime,
                model,
                Decimal(str(budget_usd)) if budget_usd is not None else None,
                json.dumps(metadata) if metadata is not None else None,
            )
        return _job_row_to_dict(row)

    async def resolve_model_credential(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        """Resolve a worker token into the identity its model calls run as.

        This is what lets the *same* capability token the sandbox already holds
        also authorize model traffic, so no second credential ever enters the
        sandbox. It returns ``None`` — meaning "reject" — unless the fence is
        still live, which makes revocation automatic: the moment the reaper
        supersedes the attempt or the job reaches a terminal state, the token
        stops buying inference. There is no separate key to remember to revoke.

        Returns ``{"user_id", "job_id", "budget_usd", "model"}``; the caller
        bills the job's owner and enforces the budget.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.user_id, j.id AS job_id, j.budget_usd, j.model, j.state
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1
                  AND a.lease_generation = $2
                  AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                  AND j.id = $3
                  AND j.current_attempt_id = a.id
                  AND j.state IN ('running', 'publishing')
                """,
                attempt_id,
                lease_generation,
                job_id,
            )
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "job_id": row["job_id"],
            "budget_usd": float(row["budget_usd"]) if row["budget_usd"] is not None else None,
            "model": row["model"],
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Fetch one job by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1", job_id
            )
        return _job_row_to_dict(row) if row else None

    async def list_jobs(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """List a user's jobs, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs "
                "WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
                user_id,
                limit,
            )
        return [_job_row_to_dict(row) for row in rows]

    # ── Claim / lease ──────────────────────────────────────────────────

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued job, creating a new fenced attempt.

        Returns the job dict extended with ``attempt_id``, ``attempt_no`` and
        ``lease_generation``, or ``None`` when the queue is empty. Concurrent
        workers are safe via ``FOR UPDATE SKIP LOCKED``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            job_row = await conn.fetchrow(
                f"""
                SELECT {_JOB_COLUMNS} FROM agent_jobs
                WHERE state = 'queued' AND cancel_requested = FALSE
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            if job_row is None:
                return None
            job_id = job_row["id"]
            next_no = await conn.fetchval(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
            attempt = await conn.fetchrow(
                """
                INSERT INTO agent_attempts
                    (job_id, attempt_no, lease_owner, lease_generation,
                     lease_expires_at, base_sha)
                VALUES ($1, $2, $3, $4, NOW() + make_interval(secs => $5), $6)
                RETURNING id, attempt_no, lease_generation
                """,
                job_id,
                next_no,
                worker_id,
                next_no,
                lease_ttl_seconds,
                job_row["base_sha"],
            )
            await conn.execute(
                "UPDATE agent_jobs SET state = 'running', current_attempt_id = $2, "
                "updated_at = NOW() WHERE id = $1",
                job_id,
                attempt["id"],
            )
        claim = _job_row_to_dict(job_row)
        claim["state"] = RUNNING
        claim["current_attempt_id"] = attempt["id"]
        claim["attempt_id"] = attempt["id"]
        claim["attempt_no"] = attempt["attempt_no"]
        claim["lease_generation"] = attempt["lease_generation"]
        return claim

    async def heartbeat(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation.

        Returns ``{"ok": False}`` when the attempt is no longer the owner
        (superseded, finished, or generation mismatch) — the worker must stop
        immediately. On success also returns ``cancel_requested`` so workers
        learn about cancellation without extra polling.
        """
        async with self._pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                UPDATE agent_attempts
                SET lease_expires_at = NOW() + make_interval(secs => $3)
                WHERE id = $1 AND lease_generation = $2 AND status = 'running'
                  AND lease_expires_at > NOW()
                RETURNING job_id
                """,
                attempt_id,
                lease_generation,
                lease_ttl_seconds,
            )
            if job_id is None:
                return {"ok": False}
            row = await conn.fetchrow(
                "SELECT state, cancel_requested FROM agent_jobs WHERE id = $1", job_id
            )
        return {
            "ok": True,
            "job_id": job_id,
            "state": row["state"],
            "cancel_requested": row["cancel_requested"],
        }

    # ── Fenced state transitions ───────────────────────────────────────

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease.

        Terminal transitions also close the attempt (``finished``). Returns
        False when the caller lost the lease or the job is not in
        ``from_states`` — the caller must stop.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs j
                SET state = $4, detail = COALESCE($5, j.detail), updated_at = NOW()
                WHERE j.id = $1
                  AND j.state = ANY($3::text[])
                  AND j.current_attempt_id = $2
                  AND EXISTS (
                        SELECT 1 FROM agent_attempts a
                        WHERE a.id = $2 AND a.lease_generation = $6
                          AND a.status = 'running'
                          AND a.lease_expires_at > NOW()
                  )
                RETURNING j.id
                """,
                job_id,
                attempt_id,
                list(from_states),
                to_state,
                detail,
                lease_generation,
            )
            if updated is None:
                return False
            if to_state in TERMINAL_STATES:
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1 AND status = 'running'",
                    attempt_id,
                )
        return True

    async def begin_publish(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Enter the one-shot publish phase (``running -> publishing``)."""
        return await self.transition(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            from_states=(RUNNING,),
            to_state=PUBLISHING,
        )

    async def complete_publish(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        pr_url: str,
    ) -> bool:
        """Record the published PR and finish the job (exactly once).

        Requires ``state = 'publishing'``, the live fenced lease, and
        ``published_pr_url IS NULL`` — so no second publish can ever land.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs j
                SET state = 'succeeded', published_pr_url = $4, updated_at = NOW()
                WHERE j.id = $1
                  AND j.state = 'publishing'
                  AND j.current_attempt_id = $2
                  AND j.published_pr_url IS NULL
                  AND EXISTS (
                        SELECT 1 FROM agent_attempts a
                        WHERE a.id = $2 AND a.lease_generation = $3
                          AND a.status = 'running'
                          AND a.lease_expires_at > NOW()
                  )
                RETURNING j.id
                """,
                job_id,
                attempt_id,
                lease_generation,
                pr_url,
            )
            if updated is None:
                return False
            await conn.execute(
                "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                "WHERE id = $1 AND status = 'running'",
                attempt_id,
            )
        return True

    # ── Events ─────────────────────────────────────────────────────────

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """Append one event, fenced by the live lease.

        Returns the global event id (the SSE ``Last-Event-ID`` cursor), or
        ``None`` when the caller lost the lease. The row lock on the attempt
        serializes ``seq`` assignment for the single legitimate writer.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT job_id, status, lease_generation, "
                "lease_expires_at <= NOW() AS expired "
                "FROM agent_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["status"] != ATTEMPT_RUNNING
                or attempt["lease_generation"] != lease_generation
                or attempt["expired"]
            ):
                return None
            return await self._insert_event(
                conn,
                job_id=attempt["job_id"],
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
            )

    async def list_events_after(
        self,
        *,
        job_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return events for a job with a global id strictly greater than the cursor."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, job_id, attempt_id, seq, event_type, payload, created_at
                FROM agent_job_events
                WHERE job_id = $1 AND id > $2
                ORDER BY id
                LIMIT $3
                """,
                job_id,
                after_id,
                limit,
            )
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "attempt_id": row["attempt_id"],
                "seq": row["seq"],
                "event_type": row["event_type"],
                "payload": _load_json(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def _insert_event(
        self,
        conn: Any,
        *,
        job_id: str,
        attempt_id: int,
        event_type: str,
        payload: dict[str, Any] | None,
    ) -> int:
        """Insert one event row inside the caller's transaction."""
        return await conn.fetchval(
            """
            INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)
            VALUES (
                $1, $2,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_job_events WHERE attempt_id = $2),
                $3, $4::jsonb
            )
            RETURNING id
            """,
            job_id,
            attempt_id,
            event_type,
            json.dumps(payload) if payload is not None else None,
        )

    # ── Artifacts ──────────────────────────────────────────────────────

    async def save_artifact(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        kind: str,
        content: str,
    ) -> int | None:
        """Store (or replace) one artifact for an attempt, fenced by the lease."""
        async with self._pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT job_id, status, lease_generation, "
                "lease_expires_at <= NOW() AS expired "
                "FROM agent_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["status"] != ATTEMPT_RUNNING
                or attempt["lease_generation"] != lease_generation
                or attempt["expired"]
            ):
                return None
            return await conn.fetchval(
                """
                INSERT INTO agent_job_artifacts (job_id, attempt_id, kind, content)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (job_id, attempt_id, kind)
                DO UPDATE SET content = EXCLUDED.content, created_at = NOW()
                RETURNING id
                """,
                attempt["job_id"],
                attempt_id,
                kind,
                content,
            )

    async def get_artifact(self, *, job_id: str, kind: str) -> dict[str, Any] | None:
        """Fetch the most recent artifact of a kind for a job."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, job_id, attempt_id, kind, content, created_at
                FROM agent_job_artifacts
                WHERE job_id = $1 AND kind = $2
                ORDER BY attempt_id DESC
                LIMIT 1
                """,
                job_id,
                kind,
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "kind": row["kind"],
            "content": row["content"],
            "created_at": row["created_at"],
        }

    # ── Cancellation ───────────────────────────────────────────────────

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running/publishing jobs get the flag
        set — the worker observes it on its next heartbeat and performs the
        fenced transition. Terminal jobs are a no-op. ``user_id`` scopes the
        cancel to the owner. Returns ``None`` for an unknown job.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT state FROM agent_jobs WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) FOR UPDATE",
                job_id,
                user_id,
            )
            if row is None:
                return None
            state = row["state"]
            if state == QUEUED:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                return CANCELLED
            if state in (RUNNING, PUBLISHING):
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                    "WHERE id = $1",
                    job_id,
                )
            return state

    # ── Platform-side publishing ───────────────────────────────────────
    #
    # Publishing is driven by the trusted server, not by the worker: the
    # sandbox holds no git credential, so it can only hand over a patch. These
    # three methods are therefore *not* lease-fenced — the fence exists to stop
    # zombie workers, and no worker is involved here. Exactly-once is enforced
    # instead by ``published_pr_url IS NULL`` plus ``SKIP LOCKED``, so two
    # gateway processes can run the publisher loop without double-publishing.

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one finished job that has a patch and no PR yet.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. Returns the job plus its patch, or ``None``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT j.id, j.user_id, j.repo, j.base_sha, j.task_prompt, a.content AS patch
                FROM agent_jobs j
                JOIN agent_job_artifacts a
                  ON a.job_id = j.id AND a.kind = 'patch'
                WHERE j.state = 'succeeded'
                  AND j.published_pr_url IS NULL
                ORDER BY j.updated_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """
            )
            if row is None:
                return None
            await conn.execute(
                "UPDATE agent_jobs SET state = 'publishing', updated_at = NOW() WHERE id = $1",
                row["id"],
            )
            job_id = row["id"]
            await self._insert_event(
                conn,
                job_id=job_id,
                attempt_id=await conn.fetchval(
                    "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
                ),
                event_type="lifecycle",
                payload={"phase": "publishing"},
            )
        return {
            "job_id": row["id"],
            "user_id": row["user_id"],
            "repo": row["repo"],
            "base_sha": row["base_sha"],
            "task_prompt": row["task_prompt"],
            "patch": row["patch"],
        }

    async def record_publish(self, *, job_id: str, pr_url: str) -> bool:
        """Record the published PR exactly once, returning the job to succeeded."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET state = 'succeeded', published_pr_url = $2, updated_at = NOW()
                WHERE id = $1 AND published_pr_url IS NULL
                RETURNING id
                """,
                job_id,
                pr_url,
            )
            if updated is None:
                return False
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="lifecycle",
                    payload={"phase": "published", "pr_url": pr_url},
                )
        return True

    async def fail_publish(self, *, job_id: str, detail: str) -> None:
        """Mark a job whose patch could not be published.

        The job is failed rather than left ``publishing``: a rejected patch
        (a blocked ``.github/`` change, a leaked credential, a conflict) is a
        human-review situation, and silently retrying it would either spam the
        repository or hide the rejection.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_jobs SET state = 'failed', detail = $2, updated_at = NOW() "
                "WHERE id = $1 AND published_pr_url IS NULL",
                job_id,
                detail[:2000],
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1", job_id
            )
            if attempt_id is not None:
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="error",
                    payload={"phase": "publish_rejected", "detail": detail[:2000]},
                )

    # ── Reaper ─────────────────────────────────────────────────────────

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Jobs caught in
        ``publishing`` are failed rather than requeued: the publisher may have
        already created the branch/PR, and a second publish must never happen
        automatically. Returns a summary per reaped attempt.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            expired = await conn.fetch(
                """
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no,
                       j.state, j.cancel_requested, j.current_attempt_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.status = 'running' AND a.lease_expires_at < NOW()
                ORDER BY a.id
                FOR UPDATE OF a, j SKIP LOCKED
                """
            )
            for row in expired:
                attempt_id = row["attempt_id"]
                job_id = row["job_id"]
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() "
                    "WHERE id = $1",
                    attempt_id,
                )
                await self._insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type=EVENT_ATTEMPT_SUPERSEDED,
                    payload={"attempt_no": row["attempt_no"], "reason": "lease_expired"},
                )
                if row["current_attempt_id"] != attempt_id or row["state"] in TERMINAL_STATES:
                    # Stale attempt of an already-moved-on job: closing it is enough.
                    action = "closed_stale"
                elif row["state"] == PUBLISHING:
                    action = FAILED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                        "updated_at = NOW() WHERE id = $1",
                        job_id,
                        "publish attempt lease expired; manual review required",
                    )
                elif row["cancel_requested"]:
                    action = CANCELLED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'cancelled', updated_at = NOW() "
                        "WHERE id = $1",
                        job_id,
                    )
                elif row["attempt_no"] >= max_attempts:
                    action = FAILED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                        "updated_at = NOW() WHERE id = $1",
                        job_id,
                        f"exhausted {max_attempts} attempts (lease expired)",
                    )
                else:
                    action = QUEUED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() WHERE id = $1",
                        job_id,
                    )
                actions.append({"job_id": job_id, "attempt_id": attempt_id, "action": action})
        return actions
