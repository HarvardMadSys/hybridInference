"""PostgreSQL persistence for user tags (``user_tags``, ``user_tag_members``).

The policy -- what a tag's limits mean and how they combine -- is
:mod:`serving.user_tags`; this module is the rows, the SQL fragment the auth
lookups join to carry an account's tags (:func:`user_tags_join`), and the
automatic rule's query (:func:`detect_members`).

**The automatic rule** counts, per account, the distinct hours among the last
``window_hours`` -- rolling hours back from now, not clock hours -- in which
it sent at least one request, and gives the tag to every active account with
at least ``min_active_hours`` of them. A request is a row of ``api_logs``
attributed to the account; the rows a batch job wrote are left out, since the
gateway sends those itself, at its own pace, whoever is watching. A
``dismissed`` membership counts only the requests sent after it was
dismissed.

**One writer at a time per tag.** Giving the tag back to a dismissed member
and dismissing it are both writes to the same row decided on what the log
said; each takes the tag's transaction-scoped advisory lock first
(:func:`lock_tag`), so a dismissal is never undone by a detection run that
read the log before it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractAsyncContextManager

#: A tag row's columns, in select order (alias ``t``).
TAG_COLUMNS = (
    "t.name, t.description, t.quota_daily_cost, t.quota_monthly_cost, "
    "t.accounting_currency, t.max_concurrent_requests, t.allowed_models, "
    "t.auto_enabled, t.auto_window_hours, t.auto_min_active_hours, t.system, "
    "t.created_at, t.updated_at"
)

#: How many accounts carry a tag, by how they came to (alias ``m``).
_MEMBER_COUNTS = (
    "COUNT(m.user_id) FILTER (WHERE m.source = 'manual') AS manual_members, "
    "COUNT(m.user_id) FILTER (WHERE m.source = 'auto') AS auto_members, "
    "COUNT(m.user_id) FILTER (WHERE m.source = 'dismissed') AS dismissed_members"
)

#: The columns an administrator may set on a tag, with the type each bound
#: value is cast to (a bare ``SET x = $1`` leaves asyncpg unable to type it).
TAG_WRITABLE_COLUMNS: dict[str, str] = {
    "description": "text",
    "quota_daily_cost": "numeric",
    "quota_monthly_cost": "numeric",
    "accounting_currency": "text",
    "max_concurrent_requests": "integer",
    "allowed_models": "text[]",
    "auto_enabled": "boolean",
    "auto_window_hours": "integer",
    "auto_min_active_hours": "integer",
}

#: The account columns a member listing names it by.
_MEMBER_ACCOUNT_COLUMNS = "u.email, u.login_name, u.phone, u.user_name, u.status"


def user_tags_join(user: str) -> str:
    """A LATERAL join adding the tags that apply to account *user*, with their limits.

    *user* is the SQL expression naming the account's id (``k.user_id``,
    ``users.id``). The join's ``ut.tag_policies`` is one JSON array of the
    tags, each with its limits, NULL for an account with none; select it and
    fold the row with :func:`serving.user_tags.tighten_identity`.
    ``ut.tag_monthly_quota`` is true when one of them sets a monthly quota,
    for a join that reads the month's spend only for an account with one.

    One index probe on ``user_tag_members``'s primary key for an account
    without tags, which is the cost every request pays.
    """
    return (
        "LEFT JOIN LATERAL ("
        "  SELECT jsonb_agg(jsonb_build_object("
        "           'name', t.name, "
        "           'source', tm.source, "
        "           'quota_daily_cost', t.quota_daily_cost, "
        "           'quota_monthly_cost', t.quota_monthly_cost, "
        "           'accounting_currency', t.accounting_currency, "
        "           'max_concurrent_requests', t.max_concurrent_requests, "
        "           'allowed_models', t.allowed_models"
        "         ) ORDER BY t.name) AS tag_policies, "
        "         bool_or(t.quota_monthly_cost IS NOT NULL) AS tag_monthly_quota "
        "  FROM user_tag_members tm "
        "  JOIN user_tags t ON t.name = tm.tag "
        f"  WHERE tm.user_id = {user} AND tm.source IN ('manual', 'auto')"
        ") ut ON TRUE "
    )


async def lock_tag(conn: Any, tag: str) -> None:
    """Hold off the other writers of *tag*'s memberships until this transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('hybridinference:user-tag:' || $1, 0))",
        tag,
    )


#: The accounts the automatic rule matches for tag ``$1``: ``$2`` window hours,
#: ``$3`` hours with requests at least. A CTE, so the detection and its
#: preview share one definition.
_ACTIVITY_CTE = """
WITH activity AS (
    SELECT l.user_id,
           COUNT(DISTINCT FLOOR(EXTRACT(EPOCH FROM (NOW() - l.timestamp)) / 3600))::int
               AS active_hours
    FROM api_logs l
    LEFT JOIN user_tag_members d
      ON d.user_id = l.user_id AND d.tag = $1 AND d.source = 'dismissed'
    WHERE l.timestamp > NOW() - make_interval(hours => $2)
      AND l.timestamp <= NOW()
      AND l.user_id IS NOT NULL
      AND l.batch_job_id IS NULL
      AND (d.user_id IS NULL OR l.timestamp > d.assigned_at)
    GROUP BY l.user_id
    HAVING COUNT(DISTINCT FLOOR(EXTRACT(EPOCH FROM (NOW() - l.timestamp)) / 3600)) >= $3
),
matched AS (
    SELECT a.user_id, a.active_hours
    FROM activity a
    JOIN users u ON u.id = a.user_id AND u.status = 'active'
)
"""

_PREVIEW_SQL = (
    _ACTIVITY_CTE
    + f"""
SELECT x.user_id, x.active_hours, m.source AS member_source, {_MEMBER_ACCOUNT_COLUMNS}
FROM matched x
JOIN users u ON u.id = x.user_id
LEFT JOIN user_tag_members m ON m.user_id = x.user_id AND m.tag = $1
ORDER BY x.active_hours DESC, x.user_id
LIMIT $4
"""
)

#: Gives the tag to every matched account that does not carry it, and back to
#: a dismissed member whose requests since the dismissal match. A manual or
#: automatic member is left as it is.
_APPLY_SQL = (
    _ACTIVITY_CTE
    + """
INSERT INTO user_tag_members (user_id, tag, source, assigned_at, assigned_by)
SELECT x.user_id, $1, 'auto', NOW(), NULL FROM matched x
ON CONFLICT (user_id, tag) DO UPDATE
    SET source = 'auto', assigned_at = NOW(), assigned_by = NULL
    WHERE user_tag_members.source = 'dismissed'
RETURNING user_id
"""
)


async def preview_members(
    conn: Any, tag: str, *, window_hours: int, min_active_hours: int, limit: int
) -> list[dict[str, Any]]:
    """The accounts the rule matches now, most active first, with how they carry the tag.

    Reads only. ``member_source`` is the account's current membership (None
    when it has none): a ``dismissed`` one is matched on its requests since
    the dismissal.
    """
    rows = await conn.fetch(_PREVIEW_SQL, tag, window_hours, min_active_hours, limit)
    return [dict(row) for row in rows]


async def detect_members(
    conn: Any, tag: str, *, window_hours: int, min_active_hours: int
) -> list[str]:
    """Give *tag* to every account its rule matches now; return the accounts that gained it.

    Takes the tag's advisory lock (:func:`lock_tag`), so the caller runs it in
    a transaction, which the lock lasts for.
    """
    await lock_tag(conn, tag)
    rows = await conn.fetch(_APPLY_SQL, tag, window_hours, min_active_hours)
    return [row["user_id"] for row in rows]


class PostgresUserTagsMixin:
    """Persist tags, their limits and rules, and which account carries which."""

    _pool: Any
    # The store's bounded-wait connection for request-path lookups.
    _request_connection: Callable[[], AbstractAsyncContextManager[Any]]

    async def list_user_tags(self) -> list[dict[str, Any]]:
        """Every tag with its limits, rule and member counts: built-in first, then by name."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {TAG_COLUMNS}, {_MEMBER_COUNTS} "
                "FROM user_tags t LEFT JOIN user_tag_members m ON m.tag = t.name "
                "GROUP BY t.name ORDER BY t.system DESC, t.name"
            )
        return [dict(row) for row in rows]

    async def get_user_tag(self, name: str) -> dict[str, Any] | None:
        """One tag with its member counts, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {TAG_COLUMNS}, {_MEMBER_COUNTS} "
                "FROM user_tags t LEFT JOIN user_tag_members m ON m.tag = t.name "
                "WHERE t.name = $1 GROUP BY t.name",
                name,
            )
        return dict(row) if row is not None else None

    async def create_user_tag(self, name: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Create a tag; None when one of that name exists.

        *fields* holds only :data:`TAG_WRITABLE_COLUMNS`; the others take
        their defaults.
        """
        columns = ["name", *fields]
        values = ["$1", *(f"${i}::{TAG_WRITABLE_COLUMNS[c]}" for i, c in enumerate(fields, 2))]
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                f"INSERT INTO user_tags ({', '.join(columns)}) VALUES ({', '.join(values)}) "
                "ON CONFLICT (name) DO NOTHING RETURNING name",
                name,
                *fields.values(),
            )
        if inserted is None:
            return None
        return await self.get_user_tag(name)

    async def update_user_tag(self, name: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Set *fields* (only :data:`TAG_WRITABLE_COLUMNS`) on a tag; None when there is none."""
        if fields:
            assignments = ", ".join(
                f"{column} = ${i}::{TAG_WRITABLE_COLUMNS[column]}"
                for i, column in enumerate(fields, 2)
            )
            async with self._pool.acquire() as conn:
                updated = await conn.fetchval(
                    f"UPDATE user_tags SET {assignments}, updated_at = NOW() "
                    "WHERE name = $1 RETURNING name",
                    name,
                    *fields.values(),
                )
            if updated is None:
                return None
        return await self.get_user_tag(name)

    async def delete_user_tag(self, name: str) -> bool:
        """Delete a tag that is not built in, and its memberships; whether one was deleted."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_tag(conn, name)
            deleted = await conn.fetchval(
                "DELETE FROM user_tags WHERE name = $1 AND NOT system RETURNING name", name
            )
        return deleted is not None

    async def list_user_tag_members(
        self,
        name: str,
        *,
        sources: Sequence[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """The accounts that carry (or were dismissed from) a tag, and how many in all.

        Applied memberships first, the most recently given first; *sources*
        narrows them (``manual``, ``auto``, ``dismissed``).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.user_id, m.source, m.assigned_at, m.assigned_by, "
                f"{_MEMBER_ACCOUNT_COLUMNS}, COUNT(*) OVER () AS total "
                "FROM user_tag_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.tag = $1 AND ($2::text[] IS NULL OR m.source = ANY($2::text[])) "
                "ORDER BY (m.source = 'dismissed'), m.assigned_at DESC, m.user_id "
                "LIMIT $3 OFFSET $4",
                name,
                list(sources) if sources is not None else None,
                limit,
                offset,
            )
        total = int(rows[0]["total"]) if rows else 0
        if not rows and offset:
            async with self._pool.acquire() as conn:
                total = int(
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM user_tag_members "
                        "WHERE tag = $1 AND ($2::text[] IS NULL OR source = ANY($2::text[]))",
                        name,
                        list(sources) if sources is not None else None,
                    )
                )
        return total, [{k: v for k, v in dict(row).items() if k != "total"} for row in rows]

    async def add_user_tag_members(
        self, name: str, user_ids: Sequence[str], *, assigned_by: str | None
    ) -> tuple[list[str], list[str]]:
        """Give a tag to accounts by hand; return ``(changed, unknown)``.

        Each known account becomes a ``manual`` member: a new one, or one that
        carried the tag automatically or was dismissed from it. One already a
        manual member is unchanged, and in neither list. *unknown* names the
        ids no account has.
        """
        wanted = list(dict.fromkeys(user_ids))
        async with self._pool.acquire() as conn, conn.transaction():
            known = {
                row["id"]
                for row in await conn.fetch(
                    "SELECT id FROM users WHERE id = ANY($1::text[])", wanted
                )
            }
            rows = await conn.fetch(
                "INSERT INTO user_tag_members (user_id, tag, source, assigned_at, assigned_by) "
                "SELECT id, $1, 'manual', NOW(), $3 FROM unnest($2::text[]) AS id "
                "ON CONFLICT (user_id, tag) DO UPDATE "
                "SET source = 'manual', assigned_at = NOW(), assigned_by = EXCLUDED.assigned_by "
                "WHERE user_tag_members.source <> 'manual' "
                "RETURNING user_id",
                name,
                [user_id for user_id in wanted if user_id in known],
                assigned_by,
            )
        changed = {row["user_id"] for row in rows}
        return (
            [user_id for user_id in wanted if user_id in changed],
            [user_id for user_id in wanted if user_id not in known],
        )

    async def remove_user_tag_member(
        self, name: str, user_id: str, *, removed_by: str | None
    ) -> str | None:
        """Take a tag away from an account; return the membership it had, or None.

        Under a tag with an automatic rule the membership becomes
        ``dismissed``, so the rule gives it back only for requests sent from
        now on; otherwise it is deleted. Removing a dismissed membership
        deletes the marker, and with it the dismissal: the rule reads the
        account's whole window again.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_tag(conn, name)
            row = await conn.fetchrow(
                "SELECT m.source, t.auto_enabled FROM user_tag_members m "
                "JOIN user_tags t ON t.name = m.tag "
                "WHERE m.tag = $1 AND m.user_id = $2 FOR UPDATE OF m",
                name,
                user_id,
            )
            if row is None:
                return None
            if row["source"] != "dismissed" and row["auto_enabled"]:
                await conn.execute(
                    "UPDATE user_tag_members SET source = 'dismissed', assigned_at = NOW(), "
                    "assigned_by = $3 WHERE tag = $1 AND user_id = $2",
                    name,
                    user_id,
                    removed_by,
                )
            else:
                await conn.execute(
                    "DELETE FROM user_tag_members WHERE tag = $1 AND user_id = $2", name, user_id
                )
        return str(row["source"])

    async def get_user_tag_memberships(self, user_id: str) -> list[dict[str, Any]]:
        """An account's memberships, dismissed ones included, each with its tag's limits."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT m.source, m.assigned_at, m.assigned_by, {TAG_COLUMNS} "
                "FROM user_tag_members m JOIN user_tags t ON t.name = m.tag "
                "WHERE m.user_id = $1 ORDER BY t.name",
                user_id,
            )
        return [dict(row) for row in rows]

    async def get_tags_for_users(self, user_ids: Sequence[str]) -> dict[str, list[str]]:
        """The tags that apply to each of *user_ids* (accounts with none are absent)."""
        if not user_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, array_agg(tag ORDER BY tag) AS tags FROM user_tag_members "
                "WHERE user_id = ANY($1::text[]) AND source IN ('manual', 'auto') "
                "GROUP BY user_id",
                list(user_ids),
            )
        return {row["user_id"]: list(row["tags"]) for row in rows}

    async def preview_user_tag_members(
        self, name: str, *, window_hours: int, min_active_hours: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The accounts a rule of these bounds matches now, without changing anything."""
        async with self._pool.acquire() as conn:
            return await preview_members(
                conn,
                name,
                window_hours=window_hours,
                min_active_hours=min_active_hours,
                limit=limit,
            )

    async def detect_user_tag_members(
        self, name: str, *, window_hours: int, min_active_hours: int
    ) -> list[str]:
        """Apply a tag's rule now; return the accounts that gained the tag."""
        async with self._pool.acquire() as conn, conn.transaction():
            return await detect_members(
                conn, name, window_hours=window_hours, min_active_hours=min_active_hours
            )
