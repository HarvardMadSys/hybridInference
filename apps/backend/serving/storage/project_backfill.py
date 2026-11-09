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


def personal_org_user_id(org_id: str) -> str | None:
    """Return the account a personal organization id belongs to, or None."""
    prefix = "org_p_"
    return org_id[len(prefix) :] if org_id.startswith(prefix) else None


def default_project_id(user_id: str) -> str:
    """Return the deterministic id of the account's Default project."""
    return f"proj_d_{user_id}"


async def ensure_personal_org(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    user_name: str | None = None,
) -> None:
    """Create the personal org, owner membership and Default project if absent."""
    async with conn.transaction():
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
            "WHERE NOT EXISTS (SELECT 1 FROM org_members m WHERE m.user_id = u.id AND m.org_id = 'org_p_' || u.id) "
            "OR NOT EXISTS (SELECT 1 FROM projects p WHERE p.id = 'proj_d_' || u.id) "
            "OR EXISTS (SELECT 1 FROM api_keys k WHERE k.user_id = u.id AND (k.org_id IS NULL OR k.project_id IS NULL)) "
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
