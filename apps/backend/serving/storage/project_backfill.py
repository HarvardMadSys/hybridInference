"""Create the personal organization and Default project every account needs.

The project model landed after accounts and keys already existed, so every
existing account gets one hidden personal organization, an owner membership and
one Default project, and its keys are attached to that project. New signups use
the same function inline.

Ids are derived from the user id, so the work is idempotent: a second run finds
rows that already exist and does nothing, and a crash halfway through resumes
where it stopped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

#: Highest number of accounts a single backfill pass touches before yielding.
BACKFILL_BATCH_SIZE = 500


def personal_org_id(user_id: str) -> str:
    """Return the deterministic id of the account's personal organization."""
    return f"org_p_{user_id}"


def default_project_id(user_id: str) -> str:
    """Return the deterministic id of the account's Default project."""
    return f"proj_d_{user_id}"


def account_scope_id(user_id: str) -> str:
    """Return the spend-counter scope id for an account's personal organization."""
    return personal_org_id(user_id)


async def ensure_personal_org(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    user_name: str | None = None,
) -> None:
    """Create the personal org, owner membership and Default project if absent."""
    org_id = personal_org_id(user_id)
    project_id = default_project_id(user_id)
    await conn.execute(
        "INSERT INTO organizations (id, name, kind) VALUES ($1, $2, 'personal') "
        "ON CONFLICT (id) DO NOTHING",
        org_id,
        user_name or user_id,
    )
    await conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'owner') "
        "ON CONFLICT (org_id, user_id) DO NOTHING",
        org_id,
        user_id,
    )
    await conn.execute(
        "INSERT INTO projects (id, org_id, name, is_default) "
        "VALUES ($1, $2, 'Default', TRUE) ON CONFLICT (id) DO NOTHING",
        project_id,
        org_id,
    )
    await conn.execute(
        "UPDATE api_keys SET org_id = $1, project_id = $2 "
        "WHERE user_id = $3 AND (org_id IS NULL OR project_id IS NULL)",
        org_id,
        project_id,
        user_id,
    )


async def backfill_projects(conn: asyncpg.Connection) -> int:
    """Attach every account without an organization, in bounded batches.

    Returns the number of accounts handled. Safe to call on every boot: an
    account that already has its rows is skipped by the catalog, not rescanned.
    """
    total = 0
    while True:
        rows = await conn.fetch(
            "SELECT u.id, u.user_name FROM users u "
            "WHERE NOT EXISTS (SELECT 1 FROM org_members m WHERE m.user_id = u.id) "
            "ORDER BY u.created_at LIMIT $1",
            BACKFILL_BATCH_SIZE,
        )
        if not rows:
            return total
        for row in rows:
            await ensure_personal_org(conn, user_id=row["id"], user_name=row["user_name"])
        total += len(rows)
        logger.info(
            "project_backfill_batch",
            extra={"event": "project_backfill_batch", "accounts": len(rows)},
        )


async def seed_spend_counters(conn: asyncpg.Connection) -> int:
    """Copy existing account spend into the generic counters, once per bucket.

    Project and key counters start at cut-over; only the account counters can
    be reconstructed from today's rows. Existing counter rows win, so a second
    run does not double the total.
    """
    tag = await conn.execute(
        """
        INSERT INTO spend_counters (scope_id, period, cost_usd, requests, last_request_at)
        SELECT 'org_p_' || d.user_id, 'd:' || d.day, d.cost_usd, d.requests, d.last_request_at
        FROM user_daily_cost d
        ON CONFLICT (scope_id, period) DO NOTHING
        """
    )
    return _count(tag)


def _count(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0
