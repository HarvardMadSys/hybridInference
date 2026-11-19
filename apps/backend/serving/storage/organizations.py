"""Organizations: the record, its SSO connections and its members.

An organization owns the accounts of its members: an account is in one
organization at most (the primary key of ``sso_organization_members``), and an
``SSO_PROVIDERS`` entry belongs to one organization at most (the primary key of
``sso_organization_connections``). Nothing here reads the provider document;
which accounts a connection makes members is decided by the caller, which
knows the provider's domains (:mod:`serving.organizations`).

Every change an administrator makes is written with its audit row in one
transaction, so the methods that change something take the actor.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from serving.accounting import get_accounting_policy
from serving.audit import AuditTarget, audit_columns
from serving.quota import native_limit
from serving.storage.audit_schema import insert_audit_row

if TYPE_CHECKING:
    from collections.abc import Sequence

    from serving.audit import AuditActor

#: What the writers here answer.
CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
NOT_FOUND = "not_found"
NAME_TAKEN = "name_taken"
CONNECTION_TAKEN = "connection_taken"
PROTECTED = "protected"
NOT_ACTIVE = "not_active"
NOT_REMOVED = "not_removed"
ABOVE_LIMIT = "above_limit"

ROLES = ("member", "admin")

#: The two limits an organization holds, by the name its column pair starts with.
LIMITS = ("monthly_limit", "default_member_monthly_limit")

_ORGANIZATION_SELECT = (
    "SELECT o.id, o.name, o.status, o.monthly_limit_usd, o.monthly_limit_amount, "
    "o.default_member_monthly_limit_usd, o.default_member_monthly_limit_amount, "
    "o.accounting_currency, o.created_at, o.updated_at, "
    "(SELECT COUNT(*) FROM sso_organization_members m WHERE m.organization_id = o.id) "
    "AS member_count, "
    "COALESCE((SELECT array_agg(c.provider_id ORDER BY c.created_at, c.provider_id) "
    "FROM sso_organization_connections c WHERE c.organization_id = o.id), '{}'::text[]) "
    "AS provider_ids "
    "FROM sso_organizations o "
)

_MEMBERSHIP_SELECT = (
    "SELECT m.user_id, m.organization_id, m.role, m.joined_at, m.removed_at, m.removed_by, "
    "o.name AS organization_name, o.status AS organization_status "
    "FROM sso_organization_members m JOIN sso_organizations o ON o.id = m.organization_id "
)


#: How a member list may be ordered, by the name the route accepts.
MEMBER_ORDER = {
    "name": "lower(COALESCE(NULLIF(u.user_name, ''), u.email, ''))",
    "status": "u.status",
    "last_request": "seen.last_request_at",
    "requests": "COALESCE(logged.requests, 0)",
    "tokens": "COALESCE(logged.total_tokens, 0)",
    "spend": "COALESCE(spent.cost_amount, 0)",
    "month": "COALESCE(month.cost_amount, 0)",
    "joined": "s.joined_at",
}

#: The member statuses a list may be narrowed to. ``removed`` is a member the
#: organization removed; ``suspended`` is every suspended account, removed or not.
MEMBER_STATUS = {
    "active": "u.status = 'active'",
    "suspended": "u.status = 'suspended'",
    "removed": "u.status = 'suspended' AND s.removed_at IS NOT NULL",
    "pending_approval": "u.status = 'pending_approval'",
}


def _scope(member_parameter: int | None = None) -> str:
    """The CTE every figure is summed over: the accounts of one organization (``$1``).

    With *member_parameter*, only the member bound at that parameter number.
    """
    member = f" AND m.user_id = ${member_parameter}" if member_parameter else ""
    return (
        "scope AS (SELECT m.user_id, m.role, m.joined_at, m.removed_at "
        f"FROM sso_organization_members m WHERE m.organization_id = $1{member})"
    )


_SCOPE = _scope()

#: A counter's native amount; ``$2`` is the fixed policy rate, for a row written
#: before its native column existed.
_AMOUNT = "COALESCE(c.cost_amount, c.cost_usd * $2::numeric)"
_LOG_AMOUNT = "COALESCE(l.cost_amount, l.cost_usd * $2::numeric)"

_THIS_MONTH = "to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"


_NO_PARAMETER = object()


def _member_filters(
    status: str | None, user_id: str | None, search: str | None
) -> list[tuple[str, Any]]:
    """The conditions a member list is narrowed by, each with the value it binds.

    ``{n}`` in a condition stands for the number its value is bound at, which
    differs between the statement that counts and the one that pages.
    """
    filters: list[tuple[str, Any]] = []
    if status is not None:
        filters.append((f"({MEMBER_STATUS[status]})", _NO_PARAMETER))
    if user_id is not None:
        filters.append(("s.user_id = ${n}", user_id))
    if search:
        filters.append(
            (
                "(lower(COALESCE(u.email, '')) LIKE ${n} "
                "OR lower(COALESCE(u.user_name, '')) LIKE ${n})",
                f"%{search.lower()}%",
            )
        )
    return filters


def _render(filters: list[tuple[str, Any]], params: list[Any]) -> str:
    """Bind *filters* after *params* and return them as one ``WHERE`` condition."""
    clauses = ["TRUE"]
    for template, value in filters:
        if value is not _NO_PARAMETER:
            params.append(value)
        clauses.append(template.replace("${n}", f"${len(params)}"))
    return " AND ".join(clauses)


def organization_scope(organization_id: str) -> str:
    """The ``spend_counters`` scope an organization's spend is counted under."""
    return f"o:{organization_id}"


def utc_bounds(first_day: date, last_day: date) -> tuple[datetime, datetime]:
    """The instants a span of UTC days starts at and ends before."""
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    end = datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC)
    return start, end + timedelta(days=1)


class _ConnectionTaken(Exception):
    """The provider belongs to another organization; the transaction is rolled back."""


def limit_write(name: str, usd: Any, amount: Any) -> tuple[str, Any]:
    """Which column of a limit's pair a write names, and the value it gets.

    The ledger's own column is written and the ``accounting_sync_amounts``
    trigger fills the other: the ``*_usd`` column on a USD ledger, the native
    ``*_amount`` column otherwise (the exact *amount* when the caller has one,
    else *usd* at the fixed policy rate). None clears the limit either way.
    """
    if name not in LIMITS:
        raise ValueError(f"unknown organization limit {name!r}")
    policy = get_accounting_policy()
    if not policy.native:
        return f"{name}_usd", usd
    if amount is None and usd is not None:
        amount = policy.from_usd(usd)
    return f"{name}_amount", amount


def limit_value(row: Any, name: str) -> Any:
    """A stored limit in the ledger currency; None when there is none."""
    if row is None:
        return None
    policy = get_accounting_policy()
    return row[f"{name}_amount"] if policy.native else row[f"{name}_usd"]


def _own_quota_join(account: str) -> str:
    """Join the monthly quota an account has of its own, as the request path reads it.

    The record of the account (``aq``) when it has one, else the copy on a key
    that is not revoked (``ak``): every such key of an account carries the
    same quota, and a key lookup reads the same two places. *account* is the
    SQL expression of the account's id.
    """
    return (
        f"LEFT JOIN account_quotas aq ON aq.user_id = {account} "
        "LEFT JOIN LATERAL ("
        "  SELECT k.quota_monthly_cost_usd, k.quota_monthly_cost_amount FROM api_keys k "
        f"  WHERE k.user_id = {account} AND k.status <> 'revoked' ORDER BY k.id LIMIT 1"
        ") ak ON aq.user_id IS NULL "
    )


#: The two columns of that quota (:func:`_own_quota_join`); NULL in both is none.
_OWN_QUOTA_USD = "COALESCE(aq.quota_monthly_cost_usd, ak.quota_monthly_cost_usd)"
_OWN_QUOTA_AMOUNT = "COALESCE(aq.quota_monthly_cost_amount, ak.quota_monthly_cost_amount)"
_OWN_QUOTA_COLUMNS = (
    f"{_OWN_QUOTA_USD} AS quota_monthly_cost_usd, {_OWN_QUOTA_AMOUNT} AS quota_monthly_cost_amount"
)


def _above(value: Any, ceiling: Any) -> bool:
    """Whether a limit *value* is above *ceiling*; never when either is none."""
    return value is not None and ceiling is not None and value > ceiling


def _inserted(tag: str) -> int:
    """The row count of an ``INSERT`` command tag (``INSERT 0 3``)."""
    return int(tag.rsplit(" ", 1)[-1])


async def join_organization(conn: Any, *, user_id: str, provider_id: str) -> str | None:
    """Make *user_id* a member of the organization *provider_id* belongs to.

    Returns the organization's id when this call made the membership, None when
    the provider belongs to no organization or the account is a member already.
    """
    return await conn.fetchval(
        "INSERT INTO sso_organization_members (user_id, organization_id) "
        "SELECT $1, c.organization_id FROM sso_organization_connections c "
        "WHERE c.provider_id = $2 "
        "ON CONFLICT (user_id) DO NOTHING RETURNING organization_id",
        user_id,
        provider_id,
    )


async def in_other_organization(conn: Any, *, user_id: str, provider_id: str) -> bool:
    """Whether *user_id* is a member of an organization other than *provider_id*'s."""
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM sso_organization_members m "
            "JOIN sso_organization_connections c ON c.provider_id = $2 "
            "WHERE m.user_id = $1 AND m.organization_id <> c.organization_id",
            user_id,
            provider_id,
        )
    )


async def _attach(
    conn: Any, organization_id: str, provider_id: str, user_ids: Sequence[str]
) -> int:
    """Attach *provider_id* and make *user_ids* members; how many memberships are new."""
    attached = await conn.fetchval(
        "INSERT INTO sso_organization_connections (provider_id, organization_id) "
        "VALUES ($1, $2) ON CONFLICT (provider_id) DO NOTHING RETURNING provider_id",
        provider_id,
        organization_id,
    )
    if attached is None:
        owner = await conn.fetchval(
            "SELECT organization_id FROM sso_organization_connections WHERE provider_id = $1",
            provider_id,
        )
        if owner != organization_id:
            raise _ConnectionTaken
    if not user_ids:
        return 0
    tag = await conn.execute(
        "INSERT INTO sso_organization_members (user_id, organization_id) "
        "SELECT u.id, $1 FROM users u WHERE u.id = ANY($2::text[]) "
        "ON CONFLICT (user_id) DO NOTHING",
        organization_id,
        list(user_ids),
    )
    return _inserted(tag)


class PostgresOrganizationsMixin:
    """Persist organizations, their SSO connections and their members."""

    _pool: Any

    # The operational store's own, shared with its ``suspend_user``.

    async def _lock_account_on(self, conn: Any, user_id: str) -> None:
        raise NotImplementedError

    async def _suspend_user_on(self, conn: Any, user_id: str, **fields: Any) -> None:
        raise NotImplementedError

    async def _set_account_monthly_quota_on(
        self, conn: Any, user_id: str, *, usd: Any, amount: Any
    ) -> None:
        raise NotImplementedError

    # -- the organization ----------------------------------------------------

    async def create_organization(
        self,
        *,
        organization_id: str,
        name: str,
        monthly_limit_usd: Any = None,
        monthly_limit_amount: Any = None,
        connections: Sequence[tuple[str, Sequence[str]]] = (),
        actor: AuditActor,
    ) -> tuple[str, int]:
        """Create an organization with its SSO connections and their members.

        *connections* pairs each provider id with the accounts it makes
        members. One transaction with the audit row. Returns the outcome
        (``"created"``, ``"name_taken"`` or ``"connection_taken"``, nothing
        written for the last two) and how many accounts became members.
        """
        column, value = limit_write("monthly_limit", monthly_limit_usd, monthly_limit_amount)
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.organization(organization_id),
            organization_id=organization_id,
        )
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                created = await conn.fetchval(
                    f"INSERT INTO sso_organizations (id, name, {column}) VALUES ($1, $2, $3) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    organization_id,
                    name,
                    value,
                )
                if created is None:
                    return NAME_TAKEN, 0
                added = 0
                for provider_id, user_ids in connections:
                    added += await _attach(conn, organization_id, provider_id, user_ids)
                await insert_audit_row(
                    conn,
                    columns=audit,
                    action="organization.create",
                    details={
                        "name": name,
                        "providers": [provider_id for provider_id, _ in connections],
                        "members_added": added,
                    },
                    success=True,
                )
        except _ConnectionTaken:
            return CONNECTION_TAKEN, 0
        return CREATED, added

    async def get_organization(self, organization_id: str) -> dict | None:
        """One organization with its member count and the ids of its SSO connections."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_ORGANIZATION_SELECT + "WHERE o.id = $1", organization_id)
        return dict(row) if row else None

    async def list_organizations(self) -> list[dict]:
        """Every organization, by name."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_ORGANIZATION_SELECT + "ORDER BY lower(o.name), o.id")
        return [dict(row) for row in rows]

    async def rename_organization(
        self, organization_id: str, name: str, *, actor: AuditActor
    ) -> str:
        """Give an organization another name; ``"name_taken"`` when another holds it."""
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.organization(organization_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            old = await conn.fetchval(
                "SELECT name FROM sso_organizations WHERE id = $1 FOR UPDATE", organization_id
            )
            if old is None:
                return NOT_FOUND
            if old == name:
                return UNCHANGED
            taken = await conn.fetchval(
                "SELECT 1 FROM sso_organizations WHERE lower(name) = lower($1) AND id <> $2",
                name,
                organization_id,
            )
            if taken:
                return NAME_TAKEN
            await conn.execute(
                "UPDATE sso_organizations SET name = $2, updated_at = NOW() WHERE id = $1",
                organization_id,
                name,
            )
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.update",
                details={"name": {"old": old, "new": name}},
                success=True,
            )
        return UPDATED

    async def set_organization_limit(
        self,
        organization_id: str,
        limit: str,
        *,
        usd: Any = None,
        amount: Any = None,
        action: str,
        actor: AuditActor,
    ) -> tuple[str, Any, Any]:
        """Set or clear one of an organization's limits (:data:`LIMITS`).

        Returns the outcome with the old and the new value in the ledger
        currency. *action* is the audit action the row is written with: the
        organization's own limit and the default of its members are changed
        by different administrators. The default of a member cannot be above
        the organization's own limit (``"above_limit"``).
        """
        column, value = limit_write(limit, usd, amount)
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.organization(organization_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            before = await conn.fetchrow(
                "SELECT monthly_limit_usd, monthly_limit_amount, "
                "default_member_monthly_limit_usd, default_member_monthly_limit_amount "
                "FROM sso_organizations WHERE id = $1 FOR UPDATE",
                organization_id,
            )
            if before is None:
                return NOT_FOUND, None, None
            if limit == "default_member_monthly_limit" and _above(
                value, limit_value(before, "monthly_limit")
            ):
                return ABOVE_LIMIT, limit_value(before, limit), None
            after = await conn.fetchrow(
                f"UPDATE sso_organizations SET {column} = $2, updated_at = NOW() WHERE id = $1 "
                f"RETURNING {limit}_usd, {limit}_amount",
                organization_id,
                value,
            )
            old, new = limit_value(before, limit), limit_value(after, limit)
            if old == new:
                return UNCHANGED, old, new
            await insert_audit_row(
                conn,
                columns=audit,
                action=action,
                details={
                    "old": str(old) if old is not None else None,
                    "new": str(new) if new is not None else None,
                    "currency": get_accounting_policy().currency,
                },
                success=True,
            )
        return UPDATED, old, new

    # -- SSO connections -----------------------------------------------------

    async def get_provider_organization(self, provider_id: str) -> str | None:
        """The organization an ``SSO_PROVIDERS`` entry belongs to, if any."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT organization_id FROM sso_organization_connections WHERE provider_id = $1",
                provider_id,
            )

    async def attach_organization_connection(
        self,
        organization_id: str,
        provider_id: str,
        *,
        user_ids: Sequence[str] = (),
        actor: AuditActor,
    ) -> tuple[str, int]:
        """Attach a provider to an organization and make *user_ids* its members.

        An account that is a member of another organization stays there.
        Attaching a provider the organization has already only adds the
        members it lacks. Returns the outcome (``"updated"``, ``"not_found"``
        or ``"connection_taken"``) and how many memberships are new.
        """
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.organization(organization_id),
            organization_id=organization_id,
        )
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                found = await conn.fetchval(
                    "SELECT 1 FROM sso_organizations WHERE id = $1 FOR UPDATE", organization_id
                )
                if found is None:
                    return NOT_FOUND, 0
                added = await _attach(conn, organization_id, provider_id, user_ids)
                await insert_audit_row(
                    conn,
                    columns=audit,
                    action="organization.connection.attach",
                    details={"provider": provider_id, "members_added": added},
                    success=True,
                )
        except _ConnectionTaken:
            return CONNECTION_TAKEN, 0
        return UPDATED, added

    async def detach_organization_connection(
        self, organization_id: str, provider_id: str, *, actor: AuditActor
    ) -> bool:
        """Detach a provider from its organization; False when it was not attached there.

        The members stay: a membership ends only with its account.
        """
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.organization(organization_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            detached = await conn.fetchval(
                "DELETE FROM sso_organization_connections "
                "WHERE provider_id = $1 AND organization_id = $2 RETURNING provider_id",
                provider_id,
                organization_id,
            )
            if detached is None:
                return False
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.connection.detach",
                details={"provider": provider_id},
                success=True,
            )
        return True

    async def list_identities_without_organization(self) -> list[dict]:
        """Every linked identity of an account that is in no organization.

        With the account's own email, which the member rule is checked on.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT i.user_id, i.provider_id, i.issuer, i.email, u.email AS account_email "
                "FROM user_identities i JOIN users u ON u.id = i.user_id "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM sso_organization_members m WHERE m.user_id = i.user_id)"
            )
        return [dict(row) for row in rows]

    # -- members -------------------------------------------------------------

    async def get_organization_membership(self, user_id: str) -> dict | None:
        """The organization an account belongs to and its role there, if any."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MEMBERSHIP_SELECT + "WHERE m.user_id = $1", user_id)
        return dict(row) if row else None

    async def set_organization_member_role(
        self, organization_id: str, user_id: str, role: str, *, actor: AuditActor
    ) -> str:
        """Give a member the administrator role, or take it back."""
        if role not in ROLES:
            raise ValueError(f"unknown organization role {role!r}")
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            old = await conn.fetchval(
                "SELECT role FROM sso_organization_members "
                "WHERE user_id = $1 AND organization_id = $2 FOR UPDATE",
                user_id,
                organization_id,
            )
            if old is None:
                return NOT_FOUND
            if old == role:
                return UNCHANGED
            await conn.execute(
                "UPDATE sso_organization_members SET role = $2 WHERE user_id = $1", user_id, role
            )
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.admin.add" if role == "admin" else "organization.admin.remove",
                details={"role": {"old": old, "new": role}},
                success=True,
            )
        return UPDATED

    async def remove_organization_member(
        self,
        organization_id: str,
        user_id: str,
        *,
        suspension_message: str | None = None,
        actor: AuditActor,
    ) -> str:
        """Suspend a member's account for its organization.

        What an administrator's suspension does (the account's keys and
        sessions are revoked with it), with the membership marked so the
        organization can restore the member later, and the audit row: one
        transaction. Not for an organization administrator nor a platform
        administrator (``"protected"``), and only for an active account
        (``"not_active"``).
        """
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            # First, as in every write to an account's keys: a row lock taken
            # before it could deadlock with a suspension that holds it.
            await self._lock_account_on(conn, user_id)
            member = await conn.fetchrow(
                "SELECT m.role, u.role AS account_role, u.status, u.email "
                "FROM sso_organization_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m, u",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND
            if member["role"] == "admin" or member["account_role"] == "admin":
                return PROTECTED
            if member["status"] != "active":
                return NOT_ACTIVE
            await self._suspend_user_on(conn, user_id, suspension_message=suspension_message)
            await conn.execute(
                "UPDATE sso_organization_members SET removed_at = NOW(), removed_by = $2 "
                "WHERE user_id = $1",
                user_id,
                actor.id,
            )
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member.remove",
                details={"email": member["email"]},
                success=True,
            )
        return UPDATED

    async def restore_organization_member(
        self, organization_id: str, user_id: str, *, actor: AuditActor
    ) -> str:
        """Make a member the organization removed active again.

        Only a removal is undone: an account a platform administrator
        suspended is theirs to restore (``"not_removed"``). The revoked keys
        stay revoked.
        """
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            member = await conn.fetchrow(
                "SELECT m.removed_at, u.status, u.email "
                "FROM sso_organization_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m, u",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND
            if member["removed_at"] is None or member["status"] != "suspended":
                return NOT_REMOVED
            await conn.execute(
                "UPDATE users SET status = 'active', suspension_message = NULL WHERE id = $1",
                user_id,
            )
            await conn.execute(
                "UPDATE sso_organization_members SET removed_at = NULL, removed_by = NULL "
                "WHERE user_id = $1",
                user_id,
            )
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member.restore",
                details={"email": member["email"]},
                success=True,
            )
        return UPDATED

    async def set_organization_member_limit(
        self,
        organization_id: str,
        user_id: str,
        *,
        usd: Any = None,
        amount: Any = None,
        actor: AuditActor,
    ) -> tuple[str, Any, Any]:
        """Set or clear the monthly quota of a member's account.

        The quota the request path already enforces for an account
        (``account_quotas``), written as an administrator's quota edit writes
        it, with the audit row: one transaction. None clears it, and the
        organization's default then applies. Not for an administrator
        (``"protected"``), nor above the organization's own limit
        (``"above_limit"``). Returns the outcome with the old and the new
        value in the ledger currency.
        """
        policy = get_accounting_policy()
        value = amount if policy.native else usd
        if policy.native and value is None and usd is not None:
            value = policy.from_usd(usd)
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_account_on(conn, user_id)
            member = await conn.fetchrow(
                "SELECT m.role, u.role AS account_role, u.email, "
                f"o.monthly_limit_usd, o.monthly_limit_amount, {_OWN_QUOTA_COLUMNS} "
                "FROM sso_organization_members m "
                "JOIN users u ON u.id = m.user_id "
                "JOIN sso_organizations o ON o.id = m.organization_id "
                f"{_own_quota_join('m.user_id')}"
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND, None, None
            if member["role"] == "admin" or member["account_role"] == "admin":
                return PROTECTED, None, None
            old = (
                native_limit(member, "quota_monthly_cost_amount", "quota_monthly_cost_usd")
                if policy.native
                else member["quota_monthly_cost_usd"]
            )
            if _above(value, limit_value(member, "monthly_limit")):
                return ABOVE_LIMIT, old, None
            if old == value:
                return UNCHANGED, old, value
            await self._set_account_monthly_quota_on(conn, user_id, usd=usd, amount=amount)
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member_limit.set",
                details={
                    "email": member["email"],
                    "old": str(old) if old is not None else None,
                    "new": str(value) if value is not None else None,
                    "currency": policy.currency,
                },
                success=True,
            )
        return UPDATED, old, value

    async def clear_organization_member_removal(self, user_id: str) -> None:
        """Forget that the organization removed *user_id*.

        For an account a platform administrator made active again: its next
        suspension is the platform's, not the organization's to undo.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sso_organization_members SET removed_at = NULL, removed_by = NULL "
                "WHERE user_id = $1 AND removed_at IS NOT NULL",
                user_id,
            )

    # -- the request path ----------------------------------------------------

    async def get_organization_spend_scope(self, user_id: str) -> dict | None:
        """The organization of an account as the request path checks it; None for none.

        The ``organization_*`` columns the API-key lookups carry, with the
        monthly quota of the member under the names those lookups give it: its
        own, else the default of the organization. For an identity resolved by
        account rather than by key, whose lookup read neither.
        """
        own = f"({_OWN_QUOTA_USD} IS NOT NULL OR {_OWN_QUOTA_AMOUNT} IS NOT NULL)"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT o.id AS organization_id, o.name AS organization_name, "
                "o.monthly_limit_usd AS organization_limit_usd, "
                "o.monthly_limit_amount AS organization_limit_amount, "
                f"CASE WHEN {own} THEN {_OWN_QUOTA_USD} "
                "ELSE o.default_member_monthly_limit_usd END AS quota_monthly_cost_usd, "
                f"CASE WHEN {own} THEN {_OWN_QUOTA_AMOUNT} "
                "ELSE o.default_member_monthly_limit_amount END AS quota_monthly_cost_amount, "
                "COALESCE(oc.cost_usd, 0) AS organization_spent_month_usd, "
                "COALESCE(oc.cost_amount, 0) AS organization_spent_month_amount "
                "FROM sso_organization_members om "
                "JOIN sso_organizations o ON o.id = om.organization_id "
                f"{_own_quota_join('om.user_id')}"
                "LEFT JOIN spend_counters oc ON oc.scope_id = 'o:' || o.id "
                " AND oc.period = 'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM') "
                "WHERE om.user_id = $1",
                user_id,
            )
        return dict(row) if row else None

    # -- what an administrator is shown --------------------------------------
    #
    # Every figure is a sum over the accounts that have a membership in the
    # organization. Spend comes from the cost counters (``user_daily_cost``),
    # the days the quota gates read; requests, tokens and models come from the
    # request log, bounded by the period. No statement here selects a
    # request's content.

    async def list_organization_administrators(self, organization_id: str) -> list[dict]:
        """The members that have the administrator role, by email."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.user_id, u.email, u.user_name, u.last_login_at "
                "FROM sso_organization_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.organization_id = $1 AND m.role = 'admin' "
                "ORDER BY lower(COALESCE(u.email, '')), m.user_id",
                organization_id,
            )
        return [dict(row) for row in rows]

    async def get_organization_month_spend(self, organization_id: str) -> dict:
        """What the organization's counter holds for this UTC month.

        The row the request path checks the organization's limit against; an
        organization nothing was charged to this month has none, and reads 0.
        """
        rate = get_accounting_policy().usd_to_currency_rate
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(c.cost_usd, 0) AS cost_usd, "
                f"COALESCE({_AMOUNT}, 0) AS cost_amount "
                "FROM spend_counters c WHERE c.scope_id = $1 "
                "AND c.period = 'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM')",
                organization_scope(organization_id),
                rate,
            )
        return dict(row) if row else {"cost_usd": 0, "cost_amount": 0}

    async def list_organization_members(
        self,
        organization_id: str,
        *,
        first_day: date,
        last_day: date,
        search: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        sort_by: str = "name",
        descending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """One page of members with their usage from *first_day* to *last_day* (UTC).

        Returns how many members match with the page. *user_id* keeps one
        member; *search* matches a name or an email; *status* is a key of
        :data:`MEMBER_STATUS`; *sort_by* a key of :data:`MEMBER_ORDER`.
        """
        if sort_by not in MEMBER_ORDER:
            raise ValueError(f"unknown member order {sort_by!r}")
        if status is not None and status not in MEMBER_STATUS:
            raise ValueError(f"unknown member status {status!r}")
        start, end = utc_bounds(first_day, last_day)
        filters = _member_filters(status, user_id, search)
        count_params: list[Any] = [organization_id]
        count_where = _render(filters, count_params)
        params: list[Any] = [
            organization_id,
            get_accounting_policy().usd_to_currency_rate,
            first_day.isoformat(),
            last_day.isoformat(),
            start,
            end,
        ]
        where = _render(filters, params)
        params.extend([limit, offset])
        direction = "DESC" if descending else "ASC"
        sql = (
            f"WITH {_SCOPE}, "
            "spent AS ("
            f"  SELECT c.user_id, SUM(c.cost_usd) AS cost_usd, SUM({_AMOUNT}) AS cost_amount "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
            "  WHERE c.day >= $3 AND c.day <= $4 GROUP BY c.user_id), "
            "month AS ("
            f"  SELECT c.user_id, SUM(c.cost_usd) AS cost_usd, SUM({_AMOUNT}) AS cost_amount "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
            f"  WHERE c.day >= {_THIS_MONTH} GROUP BY c.user_id), "
            "seen AS ("
            "  SELECT c.user_id, MAX(c.last_request_at) AS last_request_at "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id GROUP BY c.user_id), "
            "logged AS ("
            "  SELECT l.user_id, COUNT(*) AS requests, "
            "         COALESCE(SUM(l.total_tokens), 0) AS total_tokens "
            "  FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
            "  WHERE l.timestamp >= $5 AND l.timestamp < $6 GROUP BY l.user_id) "
            "SELECT s.user_id, s.role, s.joined_at, s.removed_at, "
            "       u.email, u.user_name, u.status, u.role AS account_role, u.last_login_at, "
            f"       {_OWN_QUOTA_COLUMNS}, "
            "       COALESCE(spent.cost_usd, 0) AS cost_usd, "
            "       COALESCE(spent.cost_amount, 0) AS cost_amount, "
            "       COALESCE(month.cost_usd, 0) AS spent_month_usd, "
            "       COALESCE(month.cost_amount, 0) AS spent_month_amount, "
            "       COALESCE(logged.requests, 0) AS requests, "
            "       COALESCE(logged.total_tokens, 0) AS total_tokens, "
            "       seen.last_request_at "
            "FROM scope s JOIN users u ON u.id = s.user_id "
            f"{_own_quota_join('s.user_id')}"
            "LEFT JOIN spent ON spent.user_id = s.user_id "
            "LEFT JOIN month ON month.user_id = s.user_id "
            "LEFT JOIN seen ON seen.user_id = s.user_id "
            "LEFT JOIN logged ON logged.user_id = s.user_id "
            f"WHERE {where} "
            f"ORDER BY {MEMBER_ORDER[sort_by]} {direction} NULLS LAST, s.user_id "
            f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(
                f"WITH {_SCOPE} SELECT COUNT(*) FROM scope s JOIN users u ON u.id = s.user_id "
                f"WHERE {count_where}",
                *count_params,
            )
            rows = await conn.fetch(sql, *params)
        return total, [dict(row) for row in rows]

    async def get_organization_usage(
        self,
        organization_id: str,
        *,
        first_day: date,
        last_day: date,
        hourly: bool = False,
        user_id: str | None = None,
    ) -> dict:
        """The organization's totals, series and models from *first_day* to *last_day* (UTC).

        ``spend_days`` has one entry per UTC day with spend, from the cost
        counters; ``buckets`` one per UTC day (per hour with *hourly*) with
        requests, from the request log; ``models`` one per model. With
        *user_id*, the figures of that member alone.
        """
        start, end = utc_bounds(first_day, last_day)
        rate = get_accounting_policy().usd_to_currency_rate
        bucket = "hour" if hourly else "day"

        def scoped(*values: Any) -> tuple[str, list[Any]]:
            """The scope CTE and the parameters of a statement that binds *values* after ``$1``."""
            params = [organization_id, *values]
            if user_id is None:
                return _SCOPE, params
            params.append(user_id)
            return _scope(len(params)), params

        async with self._pool.acquire() as conn:
            scope, params = scoped(rate, first_day.isoformat(), last_day.isoformat())
            spend_days = await conn.fetch(
                f"WITH {scope} "
                f"SELECT c.day, SUM(c.cost_usd) AS cost_usd, SUM({_AMOUNT}) AS cost_amount "
                "FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
                "WHERE c.day >= $3 AND c.day <= $4 GROUP BY c.day ORDER BY c.day",
                *params,
            )
            scope, params = scoped(rate, start, end)
            logged = await conn.fetch(
                f"WITH {scope} "
                f"SELECT date_trunc('{bucket}', l.timestamp AT TIME ZONE 'UTC') AS bucket, "
                "       COUNT(*) AS requests, COALESCE(SUM(l.total_tokens), 0) AS total_tokens, "
                "       COUNT(DISTINCT l.user_id) AS members, "
                "       COALESCE(SUM(l.cost_usd), 0) AS cost_usd, "
                f"       COALESCE(SUM({_LOG_AMOUNT}), 0) AS cost_amount "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                "WHERE l.timestamp >= $3 AND l.timestamp < $4 GROUP BY 1 ORDER BY 1",
                *params,
            )
            models = await conn.fetch(
                f"WITH {scope} "
                "SELECT l.model_id, COUNT(*) AS requests, "
                "       COUNT(*) FILTER (WHERE l.status_code >= 400) AS errors, "
                "       COALESCE(SUM(l.prompt_tokens), 0) AS prompt_tokens, "
                "       COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens, "
                "       COALESCE(SUM(l.cost_usd), 0) AS cost_usd, "
                f"       COALESCE(SUM({_LOG_AMOUNT}), 0) AS cost_amount "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                "WHERE l.timestamp >= $3 AND l.timestamp < $4 "
                "GROUP BY l.model_id ORDER BY 6 DESC, 2 DESC, l.model_id",
                *params,
            )
            scope, params = scoped(start, end)
            totals = await conn.fetchrow(
                f"WITH {scope} "
                "SELECT COUNT(*) AS requests, COALESCE(SUM(l.total_tokens), 0) AS total_tokens, "
                "       COUNT(DISTINCT l.user_id) AS members_with_requests "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                "WHERE l.timestamp >= $2 AND l.timestamp < $3",
                *params,
            )
            member_count = await conn.fetchval(
                "SELECT COUNT(*) FROM sso_organization_members WHERE organization_id = $1",
                organization_id,
            )
        return {
            "spend_days": [dict(row) for row in spend_days],
            "buckets": [dict(row) for row in logged],
            "requests": totals["requests"],
            "total_tokens": totals["total_tokens"],
            "members_with_requests": totals["members_with_requests"],
            "member_count": member_count,
            "models": [dict(row) for row in models],
        }

    async def list_organization_requests(
        self,
        organization_id: str,
        *,
        first_day: date,
        last_day: date,
        search: str | None = None,
        model: str | None = None,
        errors: bool | None = None,
        user_id: str | None = None,
        before: tuple[datetime, int] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Requests of the organization's members, newest first, without their content.

        *errors* keeps only the failed requests (True) or only the others
        (False). *before* is the ``(timestamp, id)`` of the last row of the
        page before; rows after it in the order are returned.
        """
        start, end = utc_bounds(first_day, last_day)
        params: list[Any] = [organization_id, start, end]
        where = ["l.timestamp >= $2", "l.timestamp < $3"]
        if user_id is not None:
            params.append(user_id)
            where.append(f"l.user_id = ${len(params)}")
        if model:
            params.append(model)
            where.append(f"l.model_id = ${len(params)}")
        if errors is not None:
            where.append("l.status_code >= 400" if errors else "COALESCE(l.status_code, 0) < 400")
        if search:
            params.append(f"%{search.lower()}%")
            where.append(
                f"(lower(COALESCE(u.email, '')) LIKE ${len(params)} "
                f"OR lower(COALESCE(u.user_name, '')) LIKE ${len(params)})"
            )
        if before is not None:
            params.extend(before)
            where.append(f"(l.timestamp, l.id) < (${len(params) - 1}, ${len(params)})")
        params.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"WITH {_SCOPE} "
                "SELECT l.id, l.timestamp, l.user_id, u.email, u.user_name, "
                "       l.project_id, p.name AS project_name, k.name AS key_name, "
                "       l.model_id, l.status_code, "
                "       COALESCE(l.prompt_tokens, 0) AS prompt_tokens, "
                "       COALESCE(l.completion_tokens, 0) AS completion_tokens, "
                "       COALESCE(l.cost_usd, 0) AS cost_usd, l.cost_amount "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                "JOIN users u ON u.id = l.user_id "
                "LEFT JOIN projects p ON p.id = l.project_id "
                "LEFT JOIN api_keys k ON k.id = l.key_id "
                f"WHERE {' AND '.join(where)} "
                f"ORDER BY l.timestamp DESC, l.id DESC LIMIT ${len(params)}",
                *params,
            )
        return [dict(row) for row in rows]

    async def list_organization_audit(
        self,
        organization_id: str,
        *,
        since: datetime | None = None,
        prefixes: Sequence[str] = (),
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """One page of the audit rows that belong to an organization, newest first.

        With the emails of the acting account and of a user target. *prefixes*
        keeps the actions that start with one of them.
        """
        params: list[Any] = [organization_id]
        where = ["a.organization_id = $1"]
        if since is not None:
            params.append(since)
            where.append(f"a.timestamp >= ${len(params)}")
        if prefixes:
            params.append([f"{prefix}%" for prefix in prefixes])
            where.append(f"a.action LIKE ANY(${len(params)}::text[])")
        if search:
            params.append(f"%{search.lower()}%")
            where.append(
                f"(lower(a.action) LIKE ${len(params)} "
                f"OR lower(COALESCE(actor.email, '')) LIKE ${len(params)} "
                f"OR lower(COALESCE(target.email, '')) LIKE ${len(params)} "
                f"OR lower(COALESCE(a.details::text, '')) LIKE ${len(params)})"
            )
        source = (
            "FROM admin_audit_log a "
            "LEFT JOIN users actor ON actor.id = a.actor_id "
            "LEFT JOIN users target ON target.id = a.target_user_id "
            f"WHERE {' AND '.join(where)}"
        )
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) {source}", *params)
            rows = await conn.fetch(
                "SELECT a.id, a.timestamp, a.action, a.details, a.actor_type, a.actor_id, "
                "a.target_type, a.target_id, a.target_user_id, "
                "actor.email AS actor_email, target.email AS target_email "
                f"{source} ORDER BY a.timestamp DESC, a.id DESC "
                f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
                *params,
                limit,
                offset,
            )
        return total, [dict(row) for row in rows]
