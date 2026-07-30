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
from typing import Any

import asyncpg

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# Job lifecycle. ``cancel_requested`` is a flag orthogonal to ``state``: it is
# delivered to the running worker via the heartbeat response, and the worker
# performs the fenced transition to ``cancelled``.
QUEUED = "queued"
WAITING = "waiting"
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
EVENT_ATTEMPT_ABORTED = "attempt_aborted"

# An attempt the platform gave up on before the agent ever started — the job
# never got its turn, so it must not count toward the retry budget.
ATTEMPT_ABORTED = "aborted"


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


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
        "thread_id": row["thread_id"],
        "parent_job_id": row["parent_job_id"],
        "turn_no": row["turn_no"],
        "user_id": row["user_id"],
        "repo": row["repo"],
        "base_sha": row["base_sha"],
        "task_prompt": row["task_prompt"],
        "setup_script": row["setup_script"],
        "runtime": row["runtime"],
        "model": row["model"],
        "state": row["state"],
        "cancel_requested": row["cancel_requested"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "published_commit_sha": row["published_commit_sha"],
        "detail": row["detail"],
        "budget_usd": float(row["budget_usd"]) if row["budget_usd"] is not None else None,
        "metadata": _load_json(row["metadata"]),
        "fork_source_job_id": row["fork_source_job_id"],
        # Resolved once, at creation, against the deployment registry — not
        # re-read per attempt. A job runs with the tool surface it was created
        # with, so removing a server from the registry stops new jobs from
        # asking for it without changing what a running one was promised.
        "mcp_servers": list(row["mcp_servers"] or []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_JOB_COLUMNS = (
    "id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha, task_prompt, "
    "setup_script, runtime, model, state, "
    "cancel_requested, current_attempt_id, published_pr_url, published_commit_sha, detail, "
    "budget_usd, metadata, fork_source_job_id, mcp_servers, "
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
                CREATE TABLE IF NOT EXISTS agent_threads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    title TEXT NOT NULL,
                    archived_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                "ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    parent_job_id TEXT,
                    turn_no INTEGER NOT NULL DEFAULT 1,
                    user_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    base_sha TEXT,
                    task_prompt TEXT NOT NULL,
                    setup_script TEXT,
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    current_attempt_id BIGINT,
                    published_pr_url TEXT,
                    published_commit_sha TEXT,
                    detail TEXT,
                    budget_usd NUMERIC(12, 6),
                    metadata JSONB,
                    mcp_servers TEXT[],
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
            await conn.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS setup_script TEXT")
            await conn.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS thread_id TEXT")
            await conn.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS parent_job_id TEXT")
            await conn.execute(
                "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS published_commit_sha TEXT"
            )
            await conn.execute(
                "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS turn_no INTEGER NOT NULL DEFAULT 1"
            )
            # Set only on turns copied by fork_thread: which original turn this
            # row duplicates, and where follow_up_context finds the source's
            # still-unpublished patch.
            await conn.execute(
                "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS fork_source_job_id TEXT"
            )
            # Nullable rather than DEFAULT '{}': a job created before MCP
            # existed asked for no servers, and NULL says that without claiming
            # someone chose it.
            await conn.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS mcp_servers TEXT[]")
            # Existing P0 jobs predate conversations. Give each one a one-turn
            # thread so old links and history immediately participate in the
            # new UI instead of becoming a second, legacy product surface.
            await conn.execute(
                """
                INSERT INTO agent_threads (id, user_id, repo, title, created_at, updated_at)
                SELECT id, user_id, repo,
                       LEFT(SPLIT_PART(task_prompt, E'\n', 1), 160), created_at, updated_at
                FROM agent_jobs
                WHERE thread_id IS NULL
                ON CONFLICT (id) DO NOTHING
                """
            )
            await conn.execute(
                "UPDATE agent_jobs SET thread_id = id, turn_no = 1 WHERE thread_id IS NULL"
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
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_thread_turn "
                "ON agent_jobs(thread_id, turn_no)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_jobs_user_repo "
                "ON agent_jobs(user_id, repo, created_at DESC)"
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
                CREATE TABLE IF NOT EXISTS agent_thread_messages (
                    id BIGSERIAL PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES agent_threads(id),
                    job_id TEXT NOT NULL REFERENCES agent_jobs(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_event_id BIGINT UNIQUE REFERENCES agent_job_events(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_thread_messages_thread "
                "ON agent_thread_messages(thread_id, id)"
            )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_thread_messages_user_job "
                "ON agent_thread_messages(job_id) WHERE role = 'user'"
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content, created_at)
                SELECT thread_id, id, 'user', task_prompt, created_at
                FROM agent_jobs
                ON CONFLICT DO NOTHING
                """
            )
            # Preserve the visible answers from jobs completed before thread
            # storage existed. Normalized ``message`` events are complete
            # assistant turns (not token deltas), and source_event_id makes
            # this safe to run at every startup.
            await conn.execute(
                """
                INSERT INTO agent_thread_messages
                    (thread_id, job_id, role, content, source_event_id, created_at)
                SELECT j.thread_id, e.job_id, 'assistant', e.payload->>'text', e.id, e.created_at
                FROM agent_job_events e
                JOIN agent_jobs j ON j.id = e.job_id
                WHERE e.event_type = 'message'
                  AND NULLIF(BTRIM(e.payload->>'text'), '') IS NOT NULL
                ON CONFLICT (source_event_id) DO NOTHING
                """
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

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_repo_grants (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    installation_id BIGINT NOT NULL,
                    account_login TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (user_id, installation_id)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_repo_grants_user "
                "ON agent_repo_grants(user_id)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    code_verifier_ciphertext TEXT,
                    expires_at TIMESTAMPTZ NOT NULL,
                    consumed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK (provider IN ('github', 'gitlab'))
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_oauth_states_expiry "
                "ON agent_oauth_states(expires_at)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_gitlab_connections (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    external_user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT,
                    web_url TEXT,
                    access_token_ciphertext TEXT NOT NULL,
                    refresh_token_ciphertext TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    # ── Repository grants ──────────────────────────────────────────────
    #
    # Which GitHub App installations a *user* has proved they can reach. The
    # entitlement has to come from something GitHub attests, never from the
    # request: the requester chooses the repository and the platform mints the
    # credential for it, so anything the requester can simply assert is a
    # confused deputy waiting to happen.

    async def record_repo_grant(
        self, *, user_id: str, installation_id: int, account_login: str | None = None
    ) -> None:
        """Record that this user may use this installation."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_repo_grants (user_id, installation_id, account_login)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, installation_id)
                DO UPDATE SET account_login = EXCLUDED.account_login
                """,
                user_id,
                installation_id,
                account_login,
            )

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return the installations this user has connected."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT installation_id, account_login FROM agent_repo_grants "
                "WHERE user_id = $1 ORDER BY id",
                user_id,
            )
        return [
            {"installation_id": row["installation_id"], "account_login": row["account_login"]}
            for row in rows
        ]

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Drop one connection. Uninstalling on GitHub is the other half."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_repo_grants WHERE user_id = $1 AND installation_id = $2 "
                "RETURNING id",
                user_id,
                installation_id,
            )
        return deleted is not None

    # OAuth state is stored hashed and consumed atomically. A callback for a
    # different user/provider, an expired state, and a replay all return no
    # row and therefore cannot be distinguished or used to claim authority.

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        user_id: str,
        provider: str,
        code_verifier_ciphertext: str | None,
        expires_at: Any,
    ) -> None:
        """Persist a short-lived state without retaining the bearer value."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM agent_oauth_states WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            await conn.execute(
                """
                INSERT INTO agent_oauth_states
                    (state_hash, user_id, provider, code_verifier_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                state_hash,
                user_id,
                provider,
                code_verifier_ciphertext,
                expires_at,
            )

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Consume one live state exactly once for its owning user/provider."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_oauth_states
                SET consumed_at = NOW()
                WHERE state_hash = $1 AND user_id = $2 AND provider = $3
                  AND consumed_at IS NULL AND expires_at > NOW()
                RETURNING code_verifier_ciphertext
                """,
                state_hash,
                user_id,
                provider,
            )
        return dict(row) if row else None

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Store one GitLab.com identity per platform user."""
        async with self._pool.acquire() as conn:
            connection_id = await conn.fetchval(
                """
                INSERT INTO agent_gitlab_connections
                    (user_id, external_user_id, username, display_name, web_url,
                     access_token_ciphertext, refresh_token_ciphertext, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (user_id) DO UPDATE SET
                    external_user_id = EXCLUDED.external_user_id,
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    web_url = EXCLUDED.web_url,
                    access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                    refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
                """,
                user_id,
                external_user_id,
                username,
                display_name,
                web_url,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return int(connection_id)

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the owning user's connection, including encrypted tokens."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, external_user_id, username, display_name, web_url,
                       access_token_ciphertext, refresh_token_ciphertext, expires_at
                FROM agent_gitlab_connections WHERE user_id = $1
                """,
                user_id,
            )
        return dict(row) if row else None

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate encrypted OAuth tokens without crossing user ownership."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_gitlab_connections SET
                    access_token_ciphertext = $3,
                    refresh_token_ciphertext = $4,
                    expires_at = $5,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2
                RETURNING id
                """,
                connection_id,
                user_id,
                access_token_ciphertext,
                refresh_token_ciphertext,
                expires_at,
            )
        return updated is not None

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delete only a connection owned by the authenticated user."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_gitlab_connections WHERE user_id = $1 AND id = $2 RETURNING id",
                user_id,
                connection_id,
            )
        return deleted is not None

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
        setup_script: str | None = None,
        budget_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it."""
        job_id = _new_job_id()
        thread_id = _new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_threads (id, user_id, repo, title)
                VALUES ($1, $2, $3, $4)
                """,
                thread_id,
                user_id,
                repo,
                task_prompt.splitlines()[0][:160],
            )
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_jobs
                    (id, thread_id, turn_no, user_id, repo, base_sha, task_prompt,
                     setup_script, runtime, model, budget_usd, metadata, mcp_servers)
                VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
                RETURNING {_JOB_COLUMNS}
                """,
                job_id,
                thread_id,
                user_id,
                repo,
                base_sha,
                task_prompt,
                setup_script,
                runtime,
                model,
                Decimal(str(budget_usd)) if budget_usd is not None else None,
                json.dumps(metadata) if metadata is not None else None,
                list(mcp_servers or []),
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                VALUES ($1, $2, 'user', $3)
                """,
                thread_id,
                job_id,
                task_prompt,
            )
        return _job_row_to_dict(row)

    async def resolve_legacy_published_commit(self, *, parent_job_id: str, commit_sha: str) -> bool:
        """Backfill an old published run's branch tip and release its child.

        Jobs published before conversation support have a PR URL and patch but
        no recorded commit. Their migrated thread id deliberately equals the
        old job id, preserving the existing ``agent/<job-id>`` branch. Once
        GitHub resolves that branch, the next turn can use it as its pinned
        base instead of re-applying the old patch and creating divergent git
        history.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET published_commit_sha = $2, updated_at = NOW()
                WHERE id = $1 AND published_pr_url IS NOT NULL
                  AND published_commit_sha IS NULL
                RETURNING id
                """,
                parent_job_id,
                commit_sha,
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'queued', base_sha = $2, updated_at = NOW()
                WHERE parent_job_id = $1 AND state = 'waiting'
                """,
                parent_job_id,
                commit_sha,
            )
        return True

    async def fail_waiting_follow_up(self, *, job_id: str, detail: str) -> bool:
        """Fail a follow-up that could not resolve its inherited git base."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE agent_jobs
                SET state = 'failed', detail = $2, updated_at = NOW()
                WHERE id = $1 AND state = 'waiting'
                RETURNING thread_id, turn_no
                """,
                job_id,
                detail[:2000],
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'failed', detail = $3, updated_at = NOW()
                WHERE thread_id = $1 AND turn_no > $2 AND state = 'waiting'
                """,
                updated["thread_id"],
                updated["turn_no"],
                "an earlier turn could not resume its draft PR branch",
            )
        return True

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

        A follow-up submitted while the latest turn is active is stored as
        ``waiting``. The thread row is locked before selecting that turn, so
        concurrent submissions form one linear chain instead of siblings that
        could later mutate the same branch at once. A request made from an old
        turn also appends to the current tip, matching chat semantics.
        """
        job_id = _new_job_id()
        async with self._pool.acquire() as conn, conn.transaction():
            requested = await conn.fetchrow(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                parent_job_id,
                user_id,
            )
            if requested is None:
                return None
            thread = await conn.fetchrow(
                "SELECT id FROM agent_threads WHERE id = $1 AND user_id = $2 FOR UPDATE",
                requested["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            thread_id = thread["id"]
            parent = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS}, "
                "EXISTS (SELECT 1 FROM agent_job_artifacts a "
                "        WHERE a.job_id = agent_jobs.id AND a.kind = 'patch') AS has_patch "
                "FROM agent_jobs WHERE thread_id = $1 "
                "ORDER BY turn_no DESC LIMIT 1 FOR UPDATE",
                thread_id,
            )
            if parent is None:  # Defensive: every thread is created with turn one.
                return None
            turn_no = parent["turn_no"] + 1
            parent_ready = parent["state"] in (FAILED, CANCELLED) or (
                parent["state"] == SUCCEEDED
                and (parent["published_commit_sha"] is not None or not parent["has_patch"])
            )
            state = QUEUED if parent_ready else WAITING
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_jobs
                    (id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha,
                     task_prompt, setup_script, runtime, model, state, budget_usd, metadata,
                     mcp_servers)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, $15)
                RETURNING {_JOB_COLUMNS}
                """,
                job_id,
                thread_id,
                parent["id"],
                turn_no,
                user_id,
                parent["repo"],
                parent["published_commit_sha"] or parent["base_sha"],
                prompt,
                parent["setup_script"],
                runtime or parent["runtime"],
                model or parent["model"],
                state,
                Decimal(str(budget_usd if budget_usd is not None else parent["budget_usd"]))
                if (budget_usd is not None or parent["budget_usd"] is not None)
                else None,
                json.dumps(_load_json(parent["metadata"]))
                if parent["metadata"] is not None
                else None,
                # Inherited, not re-resolved: the next turn of a conversation
                # gets the tool surface the thread has been running with, so a
                # follow-up cannot quietly gain a server the owner never chose.
                list(parent["mcp_servers"] or []),
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                VALUES ($1, $2, 'user', $3)
                """,
                thread_id,
                job_id,
                prompt,
            )
            await conn.execute(
                "UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id
            )
        return _job_row_to_dict(row)

    async def fork_thread(self, *, source_job_id: str, user_id: str) -> dict[str, Any] | None:
        """Duplicate a conversation up to (and including) one settled turn.

        Events are append-only and ``(thread_id, turn_no)`` is unique, so a
        thread cannot branch in place; a fork is a *new* thread whose turns
        are copies. The copies are display shells — terminal state, no
        attempts, no artifacts — which keeps them invisible to ``claim_job``
        (never queued), to the publisher (no patch artifact to claim), and to
        the reaper (no running attempt).

        Git lineage does carry over: each copy keeps ``base_sha`` and
        ``published_commit_sha``, and ``fork_source_job_id`` names the
        original turn so ``follow_up_context`` can fetch the source's
        still-unpublished patch for the fork's next run. The fork therefore
        continues from the same code state while publishing to its own
        ``agent/<thread-id>`` branch.

        Returns the copied anchor turn — the fork's navigation target — or
        ``None`` when the job is not the caller's or has not settled. An
        anchor mid-run is refused rather than partially copied: its patch
        does not exist yet, so history and workspace would disagree.
        """
        new_thread_id = _new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            source = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            if source is None or source["state"] not in TERMINAL_STATES:
                return None
            thread = await conn.fetchrow(
                "SELECT repo, title FROM agent_threads WHERE id = $1 AND user_id = $2",
                source["thread_id"],
                user_id,
            )
            if thread is None:
                return None
            turns = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs "
                "WHERE thread_id = $1 AND turn_no <= $2 ORDER BY turn_no",
                source["thread_id"],
                source["turn_no"],
            )
            messages = await conn.fetch(
                """
                SELECT m.job_id, m.role, m.content, m.created_at
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1 AND j.turn_no <= $2
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                source["thread_id"],
                source["turn_no"],
            )
            await conn.execute(
                "INSERT INTO agent_threads (id, user_id, repo, title) VALUES ($1, $2, $3, $4)",
                new_thread_id,
                user_id,
                thread["repo"],
                f"{thread['title']} (fork)"[:160],
            )
            copied_id: dict[str, str] = {}
            previous_copy: str | None = None
            for turn in turns:
                copy_id = _new_job_id()
                copied_id[turn["id"]] = copy_id
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, parent_job_id, turn_no, user_id, repo, base_sha,
                         task_prompt, setup_script, runtime, model, state,
                         published_commit_sha, detail, budget_usd, metadata,
                         fork_source_job_id, mcp_servers, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16::jsonb, $17, $18,
                            clock_timestamp(), clock_timestamp())
                    """,
                    copy_id,
                    new_thread_id,
                    previous_copy,
                    turn["turn_no"],
                    user_id,
                    turn["repo"],
                    turn["base_sha"],
                    turn["task_prompt"],
                    turn["setup_script"],
                    turn["runtime"],
                    turn["model"],
                    # An unsettled earlier turn (a cancelled anchor can sit
                    # after a still-running parent) copies as cancelled: its
                    # messages are whatever had durably landed by now.
                    turn["state"] if turn["state"] in TERMINAL_STATES else CANCELLED,
                    turn["published_commit_sha"],
                    turn["detail"],
                    turn["budget_usd"],
                    json.dumps(_load_json(turn["metadata"]))
                    if turn["metadata"] is not None
                    else None,
                    turn["id"],
                    # Carried with the turn, like its runtime and model. A fork
                    # that dropped these would give the copy a strictly smaller
                    # tool surface than the conversation it claims to continue,
                    # and the next turn would fail in a way that looks like the
                    # model got worse.
                    list(turn["mcp_servers"] or []),
                )
                previous_copy = copy_id
            # clock_timestamp(), not NOW(): NOW() is frozen for the whole
            # transaction, and list_jobs orders by created_at — copies must
            # both sort in turn order and surface as the newest conversation.
            await conn.executemany(
                """
                INSERT INTO agent_thread_messages (thread_id, job_id, role, content, created_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        new_thread_id,
                        copied_id[message["job_id"]],
                        message["role"],
                        message["content"],
                        message["created_at"],
                    )
                    for message in messages
                ],
            )
            anchor = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE id = $1",
                copied_id[source["id"]],
            )
        return _job_row_to_dict(anchor)

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return the conversation containing an owned job."""
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.repo, t.title, t.created_at, t.updated_at
                FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND j.user_id = $2
                """,
                job_id,
                user_id,
            )
            if thread is None:
                return None
            messages = await conn.fetch(
                """
                SELECT m.id, m.role, m.content, m.job_id, m.created_at
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                thread["id"],
            )
            jobs = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no",
                thread["id"],
            )
        return {
            "id": thread["id"],
            "repo": thread["repo"],
            "title": thread["title"],
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "messages": [dict(message) for message in messages],
            "jobs": [_job_row_to_dict(job) for job in jobs],
        }

    async def follow_up_context(self, *, job_id: str) -> dict[str, Any]:
        """Return prior turns and the successful parent patch for a claimed run."""
        async with self._pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT thread_id, parent_job_id, turn_no FROM agent_jobs WHERE id = $1",
                job_id,
            )
            if job is None or job["parent_job_id"] is None:
                return {"messages": [], "patch": None}
            messages = await conn.fetch(
                """
                SELECT m.role, m.content
                FROM agent_thread_messages m
                JOIN agent_jobs j ON j.id = m.job_id
                WHERE m.thread_id = $1 AND j.turn_no < $2
                ORDER BY j.turn_no, CASE WHEN m.role = 'user' THEN 0 ELSE 1 END, m.id
                """,
                job["thread_id"],
                job["turn_no"],
            )
            patch = await conn.fetchval(
                """
                SELECT a.content
                FROM agent_job_artifacts a
                JOIN agent_jobs p ON p.id = a.job_id
                WHERE a.job_id = $1 AND a.kind = 'patch'
                  AND p.state IN ('succeeded', 'publishing')
                  AND p.published_commit_sha IS NULL
                ORDER BY a.created_at DESC
                LIMIT 1
                """,
                job["parent_job_id"],
            )
            if patch is None:
                # A forked anchor is a copy with no artifacts of its own. Its
                # base_sha was frozen at fork time, so the *source* turn's
                # patch is still the uncommitted work this thread continues
                # from — even if the source thread published it later, that
                # commit landed on the source's branch, not in this fork's
                # base. The parent's own published_commit_sha stays the guard:
                # a fork taken after publish already carries the commit.
                patch = await conn.fetchval(
                    """
                    SELECT a.content
                    FROM agent_jobs parent
                    JOIN agent_jobs src ON src.id = parent.fork_source_job_id
                    JOIN agent_job_artifacts a ON a.job_id = src.id AND a.kind = 'patch'
                    WHERE parent.id = $1
                      AND parent.published_commit_sha IS NULL
                      AND src.state IN ('succeeded', 'publishing')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                    """,
                    job["parent_job_id"],
                )
        return {"messages": [dict(message) for message in messages], "patch": patch}

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

        Returns ``{"user_id", "role", "job_id", "budget_usd", "model"}``; the
        caller bills the job's owner and enforces the budget.

        ``role`` is the owner's: the sandbox's model calls run with exactly the
        model access the owner has — they pay for them and could make them
        directly, so a narrower role here only produces 404s on models the
        composer legitimately offered. A suspended owner's jobs stop buying
        inference the moment the account does. The lookup tolerates a database
        without the ``users`` table (unit fixtures build only the agent
        schema); the caller treats a missing role as ``free``.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.user_id, j.id AS job_id, j.budget_usd, j.model, j.state,
                       j.mcp_servers
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
            role: str | None = None
            try:
                owner = await conn.fetchrow(
                    "SELECT role, status FROM users WHERE id = $1", row["user_id"]
                )
            except asyncpg.UndefinedTableError:
                owner = None
            if owner is not None:
                if owner["status"] not in (None, "active"):
                    # The fence is live but the account behind it is not.
                    return None
                role = owner["role"]
        return {
            "user_id": row["user_id"],
            "role": role,
            "job_id": row["job_id"],
            # What this job may reach through the MCP proxy. Carried on the same
            # fenced lookup as the model credential so the two cannot disagree:
            # the instant the token stops buying inference it also stops
            # reaching tools.
            "mcp_servers": list(row["mcp_servers"] or []),
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

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """List a user's jobs from active or archived threads, newest first.

        ``repo`` narrows the page to one project, which is how the sidebar
        pages a single project past the global newest-first window.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_JOB_COLUMNS} FROM agent_jobs "
                "WHERE user_id = $1 AND ($4::text IS NULL OR repo = $4) AND EXISTS ("
                "    SELECT 1 FROM agent_threads t "
                "    WHERE t.id = agent_jobs.thread_id AND t.user_id = $1 "
                "      AND (($3 AND t.archived_at IS NOT NULL) "
                "           OR (NOT $3 AND t.archived_at IS NULL))"
                ") ORDER BY created_at DESC LIMIT $2",
                user_id,
                limit,
                archived,
                repo,
            )
        return [_job_row_to_dict(row) for row in rows]

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        """Summarize a user's repos, most recently active first.

        The sidebar groups tasks by project, so it needs every repo the user
        has ever run in — not just those represented in the newest-first job
        page, which would silently drop dormant projects from the tree.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT repo,
                       COUNT(DISTINCT thread_id) AS task_count,
                       COUNT(*) FILTER (
                           WHERE state IN ('queued', 'waiting', 'running', 'publishing')
                       ) AS active_count,
                       MAX(created_at) AS last_activity_at
                FROM agent_jobs
                WHERE user_id = $1 AND EXISTS (
                    SELECT 1 FROM agent_threads t
                    WHERE t.id = agent_jobs.thread_id AND t.user_id = $1
                      AND (($2 AND t.archived_at IS NOT NULL)
                           OR (NOT $2 AND t.archived_at IS NULL))
                )
                GROUP BY repo
                ORDER BY last_activity_at DESC
                """,
                user_id,
                archived,
            )
        return [
            {
                "repo": row["repo"],
                "task_count": int(row["task_count"]),
                "active_count": int(row["active_count"]),
                "last_activity_at": row["last_activity_at"],
            }
            for row in rows
        ]

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET archived_at = CASE
                    WHEN $3 THEN COALESCE(t.archived_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.archived_at
                """,
                job_id,
                user_id,
                archived,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "archived_at": row["archived_at"]}

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
            # Follow-ups may be submitted while a run is active. Promote only
            # those whose direct parent has settled; a chain therefore
            # advances one turn at a time even with multiple waiting messages.
            await conn.execute(
                """
                UPDATE agent_jobs child
                SET state = 'queued', updated_at = NOW()
                FROM agent_jobs parent
                WHERE child.state = 'waiting'
                  AND child.parent_job_id = parent.id
                  AND parent.state = ANY($1::text[])
                  AND (
                        parent.state <> 'succeeded'
                        OR parent.published_commit_sha IS NOT NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM agent_job_artifacts a
                            WHERE a.job_id = parent.id AND a.kind = 'patch'
                        )
                  )
                """,
                list(TERMINAL_STATES),
            )
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

    @staticmethod
    async def _attempts_spent(conn: Any, job_id: str) -> int:
        """Count attempts that actually got their turn.

        The retry budget used to be read off ``attempt_no``, which numbers
        every claim including ones the platform abandoned before the agent
        started. Counting instead means an aborted claim is free: numbers still
        advance (they are referenced by events, and gaps are informative), but
        only attempts that ran spend the budget.
        """
        return await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status <> $2",
            job_id,
            ATTEMPT_ABORTED,
        )

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        """Hand a just-claimed job back to the queue without spending a retry.

        For the case where the *platform* could not go through with a claim it
        already made — it could not mint the repository credential, say. The
        job never got its turn, so making it pay for the attempt is wrong twice
        over: it waits out a lease it will never use, and after
        ``max_attempts`` such failures the reaper fails it outright. A brief
        GitHub outage would terminally fail every queued private-repo job
        without an agent ever starting.

        The attempt row stays, marked ``aborted``, with a control event saying
        why: history is append-only, and "the platform dropped this one" is
        exactly the kind of thing an operator later needs to see. It simply
        does not count — see the retry budget in :meth:`reap_expired`.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            released = await conn.fetchval(
                """
                UPDATE agent_attempts
                SET status = $2, finished_at = NOW()
                WHERE id = $1 AND status = 'running'
                  AND ($3::bigint IS NULL OR lease_generation = $3)
                  AND lease_expires_at > NOW()
                RETURNING job_id
                """,
                attempt_id,
                ATTEMPT_ABORTED,
                lease_generation,
            )
            if released is None:
                return False
            # The store's own answer, not the caller's. `agent_job_events.job_id`
            # has no foreign key, so a mismatched (job_id, attempt_id) pair would
            # write a control event into a different job's stream — potentially a
            # different tenant's.
            await self._insert_event(
                conn,
                job_id=released,
                attempt_id=attempt_id,
                event_type=EVENT_ATTEMPT_ABORTED,
                payload={"reason": "credential_unavailable"},
            )
            # Fenced on this attempt still being the current one, so a job that
            # has since moved on is never dragged back to `queued`.
            #
            # An owner who cancelled while we held the claim gets `cancelled`,
            # not `queued`. Requeueing them was a dead end: `claim_job` skips
            # queued rows with `cancel_requested`, and the reaper only reaches
            # jobs with a *running* attempt — which this no longer has — so the
            # job sat in `queued` that nothing could ever move again.
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                    current_attempt_id = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND current_attempt_id = $2 AND state = 'running'
                """,
                released,
                attempt_id,
            )
        return True

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
        base_sha: str | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease.

        Terminal transitions also close the attempt (``finished``). Returns
        False when the caller lost the lease or the job is not in
        ``from_states`` — the caller must stop.

        ``base_sha`` fills in the commit the worker resolved, and only when the
        job does not already have one: an owner who pinned a commit must get a
        patch against *that* commit, so a worker may report the base it used
        but never overwrite the base it was given.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs j
                SET state = $4,
                    detail = COALESCE($5, j.detail),
                    base_sha = COALESCE(j.base_sha, $7),
                    updated_at = NOW()
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
                base_sha,
            )
            if updated is None:
                return False
            if to_state in TERMINAL_STATES:
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1 AND status = 'running'",
                    attempt_id,
                )
                has_patch = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM agent_job_artifacts "
                    "WHERE job_id = $1 AND kind = 'patch')",
                    job_id,
                )
                # A successful patch still has to cross the trusted publish
                # boundary. The next turn waits for that commit so it can
                # fast-forward the same thread branch instead of opening one
                # PR per message. Failures/cancellations and no-op successes
                # have no publish step and may release their child now.
                if to_state != SUCCEEDED or not has_patch:
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                        "WHERE parent_job_id = $1 AND state = 'waiting'",
                        job_id,
                    )
        return True

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
        event_id = await conn.fetchval(
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
        text = (payload or {}).get("text")
        if event_type == "message" and isinstance(text, str) and text.strip():
            await conn.execute(
                """
                INSERT INTO agent_thread_messages
                    (thread_id, job_id, role, content, source_event_id)
                SELECT thread_id, id, 'assistant', $2, $3
                FROM agent_jobs
                WHERE id = $1 AND thread_id IS NOT NULL
                ON CONFLICT (source_event_id) DO NOTHING
                """,
                job_id,
                text,
                event_id,
            )
            await conn.execute(
                """
                UPDATE agent_threads t
                SET updated_at = NOW()
                FROM agent_jobs j
                WHERE j.id = $1 AND t.id = j.thread_id
                """,
                job_id,
            )
        return event_id

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
            if state in (WAITING, QUEUED):
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                    "WHERE parent_job_id = $1 AND state = 'waiting'",
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
                SELECT j.id, j.thread_id, j.parent_job_id, j.user_id, j.repo, j.base_sha,
                       j.task_prompt, a.content AS patch,
                       thread_publish.published_pr_url AS parent_pr_url
                FROM agent_jobs j
                JOIN agent_job_artifacts a
                  ON a.job_id = j.id AND a.kind = 'patch'
                LEFT JOIN LATERAL (
                    SELECT prior.published_pr_url
                    FROM agent_jobs prior
                    WHERE prior.thread_id = j.thread_id
                      AND prior.published_pr_url IS NOT NULL
                    ORDER BY prior.turn_no DESC
                    LIMIT 1
                ) thread_publish ON TRUE
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
            "thread_id": row["thread_id"],
            "parent_job_id": row["parent_job_id"],
            "parent_pr_url": row["parent_pr_url"],
            "user_id": row["user_id"],
            "repo": row["repo"],
            "base_sha": row["base_sha"],
            "task_prompt": row["task_prompt"],
            "patch": row["patch"],
        }

    async def record_publish(
        self, *, job_id: str, pr_url: str, commit_sha: str | None = None
    ) -> bool:
        """Record the published PR exactly once, returning the job to succeeded."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET state = 'succeeded', published_pr_url = $2,
                    published_commit_sha = $3, updated_at = NOW()
                WHERE id = $1 AND published_pr_url IS NULL AND state = 'publishing'
                RETURNING id
                """,
                job_id,
                pr_url,
                commit_sha,
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
            await conn.execute(
                """
                UPDATE agent_jobs
                SET state = 'queued', base_sha = COALESCE($2, base_sha), updated_at = NOW()
                WHERE parent_job_id = $1 AND state = 'waiting'
                """,
                job_id,
                commit_sha,
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
            failed = await conn.fetchval(
                # `state = 'publishing'` as well as the null URL: without it a
                # late failure report could fail a job that had already moved
                # on — including one a later attempt published successfully.
                "UPDATE agent_jobs SET state = 'failed', detail = $2, updated_at = NOW() "
                "WHERE id = $1 AND published_pr_url IS NULL AND state = 'publishing' "
                "RETURNING id",
                job_id,
                detail[:2000],
            )
            if failed is None:
                return
            await conn.execute(
                "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                "WHERE parent_job_id = $1 AND state = 'waiting'",
                job_id,
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

    async def reap_stalled_publishes(self, *, stall_seconds: float = 900.0) -> list[str]:
        """Fail jobs the publisher took and never finished.

        ``claim_for_publish`` moves a job to ``publishing`` in its own
        transaction; if the publisher then crashes, nothing else ever touches
        that row. ``reap_expired`` cannot help — it scans running *attempts*,
        and this job's attempt finished before publishing began — so the job
        sits in ``publishing`` forever, invisible to its owner and to the
        publish queue.

        Failed rather than retried, for the reason this module already gives
        for a lease that expires while publishing: the branch or PR may
        already exist, and a second automatic publish must never happen.
        Returns the job ids it failed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_jobs
                SET state = 'failed',
                    detail = $2,
                    updated_at = NOW()
                WHERE state = 'publishing'
                  AND published_pr_url IS NULL
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id
                """,
                stall_seconds,
                "the publisher stopped before finishing; manual review required",
            )
            if rows:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() "
                    "WHERE parent_job_id = ANY($1::text[]) AND state = 'waiting'",
                    [row["id"] for row in rows],
                )
        return [row["id"] for row in rows]

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
                elif await self._attempts_spent(conn, job_id) >= max_attempts:
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
