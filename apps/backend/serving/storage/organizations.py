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

from typing import TYPE_CHECKING, Any

from serving.accounting import get_accounting_policy
from serving.audit import AuditTarget, audit_columns
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
        by different administrators.
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
                f"SELECT {limit}_usd, {limit}_amount FROM sso_organizations "
                "WHERE id = $1 FOR UPDATE",
                organization_id,
            )
            if before is None:
                return NOT_FOUND, None, None
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
