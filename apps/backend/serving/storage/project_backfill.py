"""Create the Default project every account needs.

Projects landed after accounts and keys already existed, so every existing
account gets one Default project and its keys are attached to it. New signups
use the same function inline.

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


def default_project_id(user_id: str) -> str:
    """Return the deterministic id of the account's Default project."""
    return f"proj_d_{user_id}"


def legacy_org_id(user_id: str) -> str:
    """Return the organization id releases before projects belonged to accounts used.

    Those releases found a project through ``projects.org_id``, which must name
    an ``organizations`` row, and kept one active name and one Default per
    account through unique indexes on it; their startup backfill moves a key
    whose ``api_keys.org_id`` is empty to Default. This release still fills
    both columns, and creates the empty row they point to, so the indexes keep
    holding and a rolled-back release finds every project with its keys.
    Nothing here reads them; they go with the contract revision that drops the
    table.
    """
    return f"org_p_{user_id}"


async def ensure_legacy_org(conn: asyncpg.Connection, *, user_id: str) -> None:
    """Create the empty organization :func:`legacy_org_id` names, and its owner, if absent.

    The membership is what the previous release's startup backfill looks for;
    without it that backfill would revisit the account on every start.
    """
    await conn.execute(
        "INSERT INTO organizations (id, name, kind) VALUES ($1, $2, 'personal') "
        "ON CONFLICT (id) DO NOTHING",
        legacy_org_id(user_id),
        user_id,
    )
    await conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'owner') "
        "ON CONFLICT (org_id, user_id) DO NOTHING",
        legacy_org_id(user_id),
        user_id,
    )


async def ensure_default_project(conn: asyncpg.Connection, *, user_id: str) -> None:
    """Create the account's Default project if absent and attach unattached keys."""
    async with conn.transaction():
        project_id = default_project_id(user_id)
        await ensure_legacy_org(conn, user_id=user_id)
        await conn.execute(
            "INSERT INTO projects (id, user_id, org_id, name, is_default) "
            "VALUES ($1, $2, $3, 'Default', TRUE) ON CONFLICT (id) DO NOTHING",
            project_id,
            user_id,
            legacy_org_id(user_id),
        )
        await conn.execute(
            "UPDATE api_keys SET project_id = COALESCE(project_id, $1), "
            "org_id = COALESCE(org_id, $3) "
            "WHERE user_id = $2 AND (project_id IS NULL OR org_id IS NULL)",
            project_id,
            user_id,
            legacy_org_id(user_id),
        )


async def backfill_projects(conn: asyncpg.Connection) -> int:
    """Give every account its Default project, in bounded batches.

    Also adopts projects the previous release created while the migration that
    gave projects an owning account was running: those carry only the legacy
    organization id. Returns the number of accounts handled. Safe to call on
    every boot.
    """
    await conn.execute(
        "UPDATE projects p SET user_id = u.id FROM users u "
        "WHERE p.user_id IS NULL AND p.org_id = 'org_p_' || u.id"
    )
    total = 0
    while True:
        rows = await conn.fetch(
            "SELECT u.id FROM users u "
            "WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id = 'proj_d_' || u.id) "
            "OR EXISTS (SELECT 1 FROM api_keys k WHERE k.user_id = u.id "
            "AND (k.project_id IS NULL OR k.org_id IS NULL)) "
            "ORDER BY u.created_at LIMIT $1",
            BACKFILL_BATCH_SIZE,
        )
        if not rows:
            return total
        for row in rows:
            await ensure_default_project(conn, user_id=row["id"])
        total += len(rows)
        logger.info(
            "project_backfill_batch",
            extra={"event": "project_backfill_batch", "accounts": len(rows)},
        )
