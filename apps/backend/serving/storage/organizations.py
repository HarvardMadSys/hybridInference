"""Organizations: the record, its SSO connections and its members.

An organization owns the accounts of its members: an account is in one
organization at most (the primary key of ``org_memberships``), and an
``SSO_PROVIDERS`` entry belongs to one organization at most (the primary key of
``org_sso_connections``). Nothing here reads the provider document;
which accounts a connection makes members is decided by the caller, which
knows the provider's domains (:mod:`serving.organizations`): it gives the rule
(:data:`AccountsOf`), and the attach applies it to the identities it reads.

Every change an administrator makes is written with its audit row in one
transaction, so the methods that change something take the actor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.accounting import get_accounting_policy
from serving.audit import AuditTarget, audit_columns
from serving.config.settings import admin_email_addresses
from serving.model_access import normalize_disabled_models
from serving.storage.audit_schema import insert_audit_row

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from serving.audit import AuditActor

#: The rule of one connection: from every linked identity of an account that is
#: in no organization (:func:`_identities_without_organization`), the accounts
#: the connection makes members. An attach calls it inside its transaction, on
#: what it reads there, so the rule must only compute.
AccountsOf = Callable[[Sequence[Mapping[str, Any]]], Sequence[str]]

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

# Two events make an account a member: a sign-in that makes or links an identity
# of a connection, and the attach of a connection to an organization. Each looks
# for what the other writes -- the sign-in for the connection, the attach for
# the identity -- so two that overlap can each read before the other commits.
# The account then has the identity, the organization has the connection, and
# there is no membership; nothing later would make one.
#
# So both take one advisory lock, until their transaction ends: an attach
# alone, a sign-in together with other sign-ins. The second of the two then
# reads after the first has committed -- every statement of a READ COMMITTED
# transaction, the default here, sees what was committed before it began.
# The lock is the first statement of both, so that neither waits for it while
# it holds a row the other needs.
_MEMBERSHIP_LOCK = "hashtext('hybridinference:organization-membership')"
ATTACH_LOCK_SQL = f"SELECT pg_advisory_xact_lock({_MEMBERSHIP_LOCK})"
SIGN_IN_LOCK_SQL = f"SELECT pg_advisory_xact_lock_shared({_MEMBERSHIP_LOCK})"

#: The two limits an organization holds, by the name its column pair starts with.
LIMITS = ("monthly_limit", "default_member_monthly_limit")

_ORGANIZATION_SELECT = (
    "SELECT o.id, o.name, o.monthly_limit_usd, o.monthly_limit_amount, "
    "o.default_member_monthly_limit_usd, o.default_member_monthly_limit_amount, "
    "o.accounting_currency, o.created_at, o.updated_at, "
    "(SELECT COUNT(*) FROM org_memberships m WHERE m.organization_id = o.id) "
    "AS member_count, "
    "COALESCE((SELECT array_agg(c.provider_id ORDER BY c.created_at, c.provider_id) "
    "FROM org_sso_connections c WHERE c.organization_id = o.id), '{}'::text[]) "
    "AS provider_ids "
    "FROM orgs o "
)

_MEMBERSHIP_SELECT = (
    "SELECT m.user_id, m.organization_id, m.role, m.joined_at, m.removed_at, m.removed_by, "
    "m.disabled_models, o.name AS organization_name "
    "FROM org_memberships m JOIN orgs o ON o.id = m.organization_id "
)


#: The monthly quota an account has of its own (:func:`_own_quota_join`); NULL
#: in both columns is none. The platform administrator sets it, and an
#: organization never writes it.
_OWN_QUOTA_USD = "COALESCE(aq.quota_monthly_cost_usd, ak.quota_monthly_cost_usd)"
_OWN_QUOTA_AMOUNT = "COALESCE(aq.quota_monthly_cost_amount, ak.quota_monthly_cost_amount)"


def member_quota_join(
    *, own: tuple[str, str], member: str, organization: str, account_role: str
) -> str:
    """Join ``mq``: the monthly quota the request path enforces for an account.

    ``mq.usd`` and ``mq.amount``, NULL in both for none. It is the lower of
    two:

    - *own*, the quota of the account itself, a USD expression and a native
      one. The platform administrator sets it.
    - what its organization has for it: the limit it set for the member
      (*member*, the alias of the row of ``org_memberships``), else
      its default (*organization*, the alias of its own row). Nothing for an
      account that is no member, and nothing for an administrator, of the
      organization or of the platform (*account_role* is the SQL of the
      account's role): the organization sets no limit of those
      (:func:`is_protected`), so its default does not reach them either.

    So an organization can bring the quota of a member down, and never above
    what the platform administrator set. The two are compared in the native
    amounts where both have one, else in USD: a quota row from before its
    native column has only the USD one, and a native amount is not judged
    through its rounded USD equivalent when it need not be.
    """
    own_usd, own_amount = own
    has = f"({member}.monthly_limit_usd IS NOT NULL OR {member}.monthly_limit_amount IS NOT NULL)"
    none = f"({member}.user_id IS NULL OR {member}.role = 'admin' OR {account_role} = 'admin')"

    def limit(suffix: str) -> str:
        return (
            f"CASE WHEN {none} THEN NULL WHEN {has} THEN {member}.monthly_limit_{suffix} "
            f"ELSE {organization}.default_member_monthly_limit_{suffix} END AS {suffix}"
        )

    applies = (
        "(lim.usd IS NOT NULL OR lim.amount IS NOT NULL) "
        "AND ((own.usd IS NULL AND own.amount IS NULL) "
        "OR CASE WHEN lim.amount IS NOT NULL AND own.amount IS NOT NULL "
        "THEN lim.amount <= own.amount ELSE lim.usd <= own.usd END)"
    )
    return (
        "LEFT JOIN LATERAL ("
        f"  SELECT CASE WHEN {applies} THEN lim.usd ELSE own.usd END AS usd, "
        f"         CASE WHEN {applies} THEN lim.amount ELSE own.amount END AS amount "
        f"  FROM (SELECT {limit('usd')}, {limit('amount')}) lim, "
        f"       (SELECT {own_usd} AS usd, {own_amount} AS amount) own"
        ") mq ON TRUE "
    )


#: What an organization has for a member of a member list (``s``, ``u``, ``o``),
#: in the ledger currency: the limit it set for the member, else its default;
#: NULL for an administrator. ``$2`` is the fixed policy rate.
_ORGANIZATION_LIMIT = (
    "CASE WHEN s.role = 'admin' OR u.role = 'admin' THEN NULL ELSE "
    "COALESCE(s.monthly_limit_amount, s.monthly_limit_usd * $2::numeric, "
    "o.default_member_monthly_limit_amount, o.default_member_monthly_limit_usd * $2::numeric) END"
)

#: The monthly limit that applies to a member, in the ledger currency: the
#: lower of the account's own quota and what its organization has for it
#: (``LEAST`` leaves a NULL out). NULL is no limit. ``$2`` is the fixed policy
#: rate, for a row written before its native column.
_MEMBER_LIMIT = (
    f"LEAST(COALESCE({_OWN_QUOTA_AMOUNT}, {_OWN_QUOTA_USD} * $2::numeric), {_ORGANIZATION_LIMIT})"
)

#: How far the month's spend of a member is into that limit: 0.59 for 59%. A
#: limit of zero is used up. NULL, which every order puts last, is no limit.
_LIMIT_USE = (
    f"CASE WHEN {_MEMBER_LIMIT} IS NULL THEN NULL WHEN {_MEMBER_LIMIT} <= 0 THEN 1 "
    f"ELSE COALESCE(month.cost_amount, 0) / {_MEMBER_LIMIT} END"
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
    "today": "COALESCE(fixed.today_amount, 0)",
    "week": "COALESCE(fixed.week_amount, 0)",
    "alltime": "COALESCE(fixed.alltime_amount, 0)",
    "limit_use": _LIMIT_USE,
    "joined": "s.joined_at",
}

#: How far into its monthly limit a member is that a list may be narrowed to:
#: ``near`` is 80% of it or more, as in Admin, and ``over`` all of it or more.
#: A member without a limit is in neither.
LIMIT_STATES = {"near": "0.8", "over": "1"}

#: How many days the trend of a member's spend has, with today.
TREND_DAYS = 7

#: The windows of the request volume, each ending now; the longest is last.
VOLUME_WINDOWS = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}

#: The member statuses a list may be narrowed to, one for each word a member is
#: shown with: ``removed`` is a member the organization removed, ``suspended``
#: an account that is suspended in another way. No member is in two of them.
MEMBER_STATUS = {
    "active": "u.status = 'active'",
    "suspended": "u.status = 'suspended' AND s.removed_at IS NULL",
    "removed": "u.status = 'suspended' AND s.removed_at IS NOT NULL",
    "pending_approval": "u.status = 'pending_approval'",
}


def _scope(member_parameter: int | None = None) -> str:
    """The CTE every figure is summed over: the accounts of one organization (``$1``).

    With *member_parameter*, only the member bound at that parameter number.
    """
    member = f" AND m.user_id = ${member_parameter}" if member_parameter else ""
    return (
        "scope AS (SELECT m.user_id, m.role, m.joined_at, m.removed_at, "
        "m.monthly_limit_usd, m.monthly_limit_amount "
        f"FROM org_memberships m WHERE m.organization_id = $1{member})"
    )


_SCOPE = _scope()

#: A counter's native amount; ``$2`` is the fixed policy rate, for a row written
#: before its native column existed.
_AMOUNT = "COALESCE(c.cost_amount, c.cost_usd * $2::numeric)"
_LOG_AMOUNT = "COALESCE(l.cost_amount, l.cost_usd * $2::numeric)"

_THIS_MONTH = "to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"

#: The figures of a member start when its account became a member (``s`` is its
#: scope row): what the account did before is not its organization's to see. An
#: attach makes accounts members that did nothing for it, and their earlier
#: requests name models, projects and keys. A request has its instant. A cost
#: counter is one row for a UTC day, so the day of the joining counts whole.
_SINCE_JOINED_LOG = "l.timestamp >= s.joined_at"
_SINCE_JOINED_DAY = "c.day >= to_char(s.joined_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"


def contains_pattern(text: str) -> str:
    """A ``LIKE`` pattern that finds *text* anywhere, in lower case.

    ``%``, ``_`` and the backslash of the text are escaped (:data:`_LIKE`), so
    a search for ``50%`` finds that text and not everything that has ``50``.
    """
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


#: ``LIKE`` against a :func:`contains_pattern`; ``{n}`` is its parameter number.
_LIKE = "LIKE ${n} ESCAPE '\\'"


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
                f"(lower(COALESCE(u.email, '')) {_LIKE} "
                f"OR lower(COALESCE(u.user_name, '')) {_LIKE})",
                contains_pattern(search),
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


#: The two columns of that quota, as a list of members and a limit read select them.
_OWN_QUOTA_COLUMNS = (
    f"{_OWN_QUOTA_USD} AS quota_monthly_cost_usd, {_OWN_QUOTA_AMOUNT} AS quota_monthly_cost_amount"
)


#: :func:`member_quota_join` for a membership ``om`` of organization ``o`` with
#: its account ``u`` and that account's own quota (:func:`_own_quota_join`).
_MEMBER_QUOTA_JOIN = member_quota_join(
    own=(_OWN_QUOTA_USD, _OWN_QUOTA_AMOUNT), member="om", organization="o", account_role="u.role"
)


def model_list(stored: Any) -> list[str]:
    """A ``disabled_models`` column as a list, whatever the driver returned it as."""
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except json.JSONDecodeError:
            return []
    return normalize_disabled_models(stored)


def is_protected(member: Mapping[str, Any]) -> bool:
    """Whether a member is the platform administrator's to manage, not its organization's.

    An administrator, of the organization (``role``) or of the platform
    (``account_role``): the organization sets no limit of it and does not
    remove it.
    """
    return member["role"] == "admin" or member["account_role"] == "admin"


def _above(value: Any, ceiling: Any) -> bool:
    """Whether a limit *value* is above *ceiling*; never when either is none."""
    return value is not None and ceiling is not None and value > ceiling


async def lock_for_sign_in(conn: Any) -> None:
    """Keep an attach out until this sign-in's transaction ends.

    The first statement of a transaction that makes or links an identity, so
    that :func:`join_organization` finds a connection attached before it, and
    an attach after it finds the identity (see :data:`ATTACH_LOCK_SQL`).
    """
    await conn.execute(SIGN_IN_LOCK_SQL)


#: A member that is not a platform administrator, as ``users u``: the account
#: that signs in through single sign-on only. ``{exempt}`` is the parameter
#: that holds the ``ADMIN_EMAILS`` addresses, which make an administrator of an
#: account whose role does not say so yet.
_SSO_ONLY_ACCOUNT = (
    "u.role IS DISTINCT FROM 'admin' "
    "AND NOT COALESCE(lower(u.email) = ANY(${exempt}::text[]), FALSE)"
)


async def close_password_sign_in(conn: Any, user_ids: Sequence[str]) -> None:
    """Take the password, and the links that set one, from accounts that just became members.

    The organization owns the account of a member, so its identity provider
    decides who signs in: a password from before would let in a person the
    provider disabled. In the transaction that makes the membership. A platform
    administrator keeps the password: that account must get in when the
    provider is not available.
    """
    if not user_ids:
        return
    members = f"u.id = ANY($1::text[]) AND {_SSO_ONLY_ACCOUNT.format(exempt=2)}"
    arguments = (list(user_ids), admin_email_addresses())
    await conn.execute(
        f"DELETE FROM password_reset_tokens t USING users u WHERE t.user_id = u.id AND {members}",
        *arguments,
    )
    await conn.execute(
        f"UPDATE users u SET password_hash = NULL WHERE u.password_hash IS NOT NULL AND {members}",
        *arguments,
    )


async def signs_in_through_sso_only(conn: Any, user_id: str) -> bool:
    """Whether *user_id* is a member that is not a platform administrator.

    The rule of :func:`serving.organizations.is_sso_only`, for a transaction
    that must decide under its own locks.
    """
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM org_memberships m JOIN users u ON u.id = m.user_id "
            f"WHERE m.user_id = $1 AND {_SSO_ONLY_ACCOUNT.format(exempt=2)}",
            user_id,
            admin_email_addresses(),
        )
    )


async def join_organization(conn: Any, *, user_id: str, provider_id: str) -> str | None:
    """Make *user_id* a member of the organization *provider_id* belongs to.

    Returns the organization's id when this call made the membership, None when
    the provider belongs to no organization or the account is a member already.
    Its transaction took :func:`lock_for_sign_in` first. A new member loses its
    password (:func:`close_password_sign_in`).
    """
    organization_id = await conn.fetchval(
        "INSERT INTO org_memberships (user_id, organization_id) "
        "SELECT $1, c.organization_id FROM org_sso_connections c "
        "WHERE c.provider_id = $2 "
        "ON CONFLICT (user_id) DO NOTHING RETURNING organization_id",
        user_id,
        provider_id,
    )
    if organization_id is not None:
        await close_password_sign_in(conn, [user_id])
    return organization_id


async def in_other_organization(conn: Any, *, user_id: str, provider_id: str) -> bool:
    """Whether *user_id* is a member of an organization other than *provider_id*'s."""
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM org_memberships m "
            "JOIN org_sso_connections c ON c.provider_id = $2 "
            "WHERE m.user_id = $1 AND m.organization_id <> c.organization_id",
            user_id,
            provider_id,
        )
    )


async def _identities_without_organization(conn: Any) -> list[dict]:
    """Every linked identity of an account that is in no organization.

    With the account's own email, which the member rule is checked on. Read
    under :data:`ATTACH_LOCK_SQL`, so no sign-in is adding to it.
    """
    rows = await conn.fetch(
        "SELECT i.user_id, i.provider_id, i.issuer, i.email, u.email AS account_email "
        "FROM user_identities i JOIN users u ON u.id = i.user_id "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM org_memberships m WHERE m.user_id = i.user_id)"
    )
    return [dict(row) for row in rows]


async def _attach(
    conn: Any, organization_id: str, provider_id: str, user_ids: Sequence[str]
) -> list[str]:
    """Attach *provider_id* and make *user_ids* members; the accounts whose membership is new."""
    attached = await conn.fetchval(
        "INSERT INTO org_sso_connections (provider_id, organization_id) "
        "VALUES ($1, $2) ON CONFLICT (provider_id) DO NOTHING RETURNING provider_id",
        provider_id,
        organization_id,
    )
    if attached is None:
        owner = await conn.fetchval(
            "SELECT organization_id FROM org_sso_connections WHERE provider_id = $1",
            provider_id,
        )
        if owner != organization_id:
            raise _ConnectionTaken
    if not user_ids:
        return []
    rows = await conn.fetch(
        "INSERT INTO org_memberships (user_id, organization_id) "
        "SELECT u.id, $1 FROM users u WHERE u.id = ANY($2::text[]) "
        "ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
        organization_id,
        list(user_ids),
    )
    joined = [row["user_id"] for row in rows]
    await close_password_sign_in(conn, joined)
    return joined


class PostgresOrganizationsMixin:
    """Persist organizations, their SSO connections and their members."""

    _pool: Any
    # The store's bounded-wait connection for request-path lookups.
    _request_connection: Callable[[], AbstractAsyncContextManager[Any]]

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
        connections: Sequence[tuple[str, AccountsOf]] = (),
        actor: AuditActor,
    ) -> tuple[str, int]:
        """Create an organization with its SSO connections and their members.

        *connections* pairs each provider id with the rule that says which
        accounts it makes members. One transaction with the audit row, and with
        the read the rules are applied to: no sign-in comes between that read
        and the connections (:data:`ATTACH_LOCK_SQL`). Returns the outcome
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
                if connections:
                    await conn.execute(ATTACH_LOCK_SQL)
                created = await conn.fetchval(
                    f"INSERT INTO orgs (id, name, {column}) VALUES ($1, $2, $3) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    organization_id,
                    name,
                    value,
                )
                if created is None:
                    return NAME_TAKEN, 0
                added = 0
                identities = await _identities_without_organization(conn) if connections else []
                for provider_id, accounts_of in connections:
                    added += len(
                        await _attach(conn, organization_id, provider_id, accounts_of(identities))
                    )
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
        """Give an organization another name; ``"name_taken"`` when another holds it.

        Also when another organization takes the name while this one is
        changed: the unique index decides between two renames at once, and the
        one that comes second is told the same.
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
                old = await conn.fetchval(
                    "SELECT name FROM orgs WHERE id = $1 FOR UPDATE", organization_id
                )
                if old is None:
                    return NOT_FOUND
                if old == name:
                    return UNCHANGED
                taken = await conn.fetchval(
                    "SELECT 1 FROM orgs WHERE lower(name) = lower($1) AND id <> $2",
                    name,
                    organization_id,
                )
                if taken:
                    return NAME_TAKEN
                await conn.execute(
                    "UPDATE orgs SET name = $2, updated_at = NOW() WHERE id = $1",
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
        except asyncpg.UniqueViolationError:
            return NAME_TAKEN
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
                "FROM orgs WHERE id = $1 FOR UPDATE",
                organization_id,
            )
            if before is None:
                return NOT_FOUND, None, None
            if limit == "default_member_monthly_limit" and _above(
                value, limit_value(before, "monthly_limit")
            ):
                return ABOVE_LIMIT, limit_value(before, limit), None
            after = await conn.fetchrow(
                f"UPDATE orgs SET {column} = $2, updated_at = NOW() WHERE id = $1 "
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
                "SELECT organization_id FROM org_sso_connections WHERE provider_id = $1",
                provider_id,
            )

    async def attach_organization_connection(
        self,
        organization_id: str,
        provider_id: str,
        *,
        accounts_of: AccountsOf,
        actor: AuditActor,
    ) -> tuple[str, int]:
        """Attach a provider to an organization and make its accounts members.

        *accounts_of* says which accounts those are, of the identities read in
        this transaction: no sign-in comes between that read and the connection
        (:data:`ATTACH_LOCK_SQL`). An account that is a member of another
        organization stays there. Attaching a provider the organization has
        already only adds the members it lacks. Returns the outcome
        (``"updated"``, ``"not_found"`` or ``"connection_taken"``) and how many
        memberships are new.
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
                await conn.execute(ATTACH_LOCK_SQL)
                found = await conn.fetchval(
                    "SELECT 1 FROM orgs WHERE id = $1 FOR UPDATE", organization_id
                )
                if found is None:
                    return NOT_FOUND, 0
                user_ids = accounts_of(await _identities_without_organization(conn))
                added = len(await _attach(conn, organization_id, provider_id, user_ids))
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
                "DELETE FROM org_sso_connections "
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

    # -- members -------------------------------------------------------------

    async def get_organization_membership(self, user_id: str) -> dict | None:
        """The organization an account belongs to and its role there, if any."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_MEMBERSHIP_SELECT + "WHERE m.user_id = $1", user_id)
        if row is None:
            return None
        return {**row, "disabled_models": model_list(row["disabled_models"])}

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
                "SELECT role FROM org_memberships "
                "WHERE user_id = $1 AND organization_id = $2 FOR UPDATE",
                user_id,
                organization_id,
            )
            if old is None:
                return NOT_FOUND
            if old == role:
                return UNCHANGED
            await conn.execute(
                "UPDATE org_memberships SET role = $2 WHERE user_id = $1", user_id, role
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
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m, u",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND
            if is_protected(member):
                return PROTECTED
            if member["status"] != "active":
                return NOT_ACTIVE
            await self._suspend_user_on(conn, user_id, suspension_message=suspension_message)
            await conn.execute(
                "UPDATE org_memberships SET removed_at = NOW(), removed_by = $2 WHERE user_id = $1",
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
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
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
                "UPDATE org_memberships SET removed_at = NULL, removed_by = NULL "
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
        """Set or clear the monthly limit an organization has for one member.

        The limit is the organization's own, on the member's row. The quota of
        the account (``account_quotas``) is the platform administrator's: it
        is not read and not written here, and the request path enforces the
        lower of the two (:func:`member_quota_join`). So this can bring the
        quota of a member down and never above what the platform administrator
        set. None clears the limit, and the organization's default then
        applies. Not for an administrator (``"protected"``), nor above the
        organization's own limit (``"above_limit"``). One transaction with the
        audit row. Returns the outcome with the old and the new value in the
        ledger currency.
        """
        column, value = limit_write("monthly_limit", usd, amount)
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            member = await conn.fetchrow(
                "SELECT m.role, u.role AS account_role, u.email, "
                "m.monthly_limit_usd AS member_limit_usd, "
                "m.monthly_limit_amount AS member_limit_amount, "
                "o.monthly_limit_usd, o.monthly_limit_amount "
                "FROM org_memberships m "
                "JOIN users u ON u.id = m.user_id "
                "JOIN orgs o ON o.id = m.organization_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND, None, None
            if is_protected(member):
                return PROTECTED, None, None
            old = limit_value(member, "member_limit")
            if _above(value, limit_value(member, "monthly_limit")):
                return ABOVE_LIMIT, old, None
            after = await conn.fetchrow(
                f"UPDATE org_memberships SET {column} = $2 WHERE user_id = $1 "
                "RETURNING monthly_limit_usd AS member_limit_usd, "
                "monthly_limit_amount AS member_limit_amount",
                user_id,
                value,
            )
            new = limit_value(after, "member_limit")
            if old == new:
                return UNCHANGED, old, new
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member_limit.set",
                details={
                    "email": member["email"],
                    "old": str(old) if old is not None else None,
                    "new": str(new) if new is not None else None,
                    "currency": get_accounting_policy().currency,
                },
                success=True,
            )
        return UPDATED, old, new

    async def set_organization_member_models(
        self,
        organization_id: str,
        user_id: str,
        models: Sequence[str],
        *,
        actor: AuditActor,
    ) -> tuple[str, list[str], list[str]]:
        """Replace the models an organization turned off for one member.

        The list is the organization's own, on the member's row. The list of
        the account (its ``preferences``) is the platform administrator's: it
        is not read and not written here, and the request path refuses a model
        in either (:func:`serving.model_access.disabled_models_of`). So this
        can turn a model off for a member and never turn on one the platform
        administrator turned off. Not for an administrator (``"protected"``).
        One transaction with the audit row. Returns the outcome with the old
        and the new list.
        """
        new = normalize_disabled_models(list(models))
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            member = await conn.fetchrow(
                "SELECT m.role, u.role AS account_role, u.email, m.disabled_models "
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2 FOR UPDATE OF m",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND, [], []
            if is_protected(member):
                return PROTECTED, [], []
            old = model_list(member["disabled_models"])
            if old == new:
                return UNCHANGED, old, new
            await conn.execute(
                "UPDATE org_memberships SET disabled_models = $2::jsonb WHERE user_id = $1",
                user_id,
                json.dumps(new),
            )
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member_models.set",
                details={"email": member["email"], "disabled_models": {"old": old, "new": new}},
                success=True,
            )
        return UPDATED, old, new

    async def revoke_organization_member_key(
        self, organization_id: str, user_id: str, key_id: int, *, actor: AuditActor
    ) -> tuple[str, str | None]:
        """Revoke one API key of a member, as its owner does in the console.

        The key must be the member's own: any other is ``"not_found"``, as a
        key that does not exist. Not for an administrator (``"protected"``),
        and a key that is not active stays as it is (``"not_active"``). One
        transaction with the audit row, which names the key and its project
        and never its value. Returns the outcome and the identity the key
        authenticates as, whose cached lookups the caller drops.
        """
        audit = audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=actor,
            target=AuditTarget.user(user_id),
            organization_id=organization_id,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            # First, as in every write to an account's keys.
            await self._lock_account_on(conn, user_id)
            member = await conn.fetchrow(
                "SELECT m.role, u.role AS account_role, u.email "
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = $1 AND m.organization_id = $2",
                user_id,
                organization_id,
            )
            if member is None:
                return NOT_FOUND, None
            key = await conn.fetchrow(
                "SELECT k.status, k.name, k.user_id, p.name AS project_name "
                "FROM api_keys k LEFT JOIN projects p ON p.id = k.project_id "
                "WHERE k.id = $1 AND k.account_id = $2 FOR UPDATE OF k",
                key_id,
                user_id,
            )
            if key is None:
                return NOT_FOUND, None
            if is_protected(member):
                return PROTECTED, None
            if key["status"] != "active":
                return NOT_ACTIVE, None
            await conn.execute("UPDATE api_keys SET status = 'revoked' WHERE id = $1", key_id)
            await insert_audit_row(
                conn,
                columns=audit,
                action="organization.member_key.revoke",
                details={
                    "email": member["email"],
                    "key_name": key["name"],
                    "project_name": key["project_name"],
                },
                success=True,
            )
        return UPDATED, key["user_id"]

    async def clear_organization_member_removal(self, user_id: str) -> None:
        """Forget that the organization removed *user_id*.

        For an account a platform administrator made active again: its next
        suspension is the platform's, not the organization's to undo.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE org_memberships SET removed_at = NULL, removed_by = NULL "
                "WHERE user_id = $1 AND removed_at IS NOT NULL",
                user_id,
            )

    # -- the request path ----------------------------------------------------

    async def get_organization_spend_scope(self, user_id: str) -> dict | None:
        """The organization of an account as the request path checks it; None for none.

        The ``organization_*`` columns the API-key lookups carry, with the
        monthly quota of the account under the names those lookups give it:
        the lower of its own and what its organization has for it
        (:func:`member_quota_join`). For an identity resolved by account rather
        than by key, whose lookup read neither. A request-path read
        (:data:`~serving.storage.postgres_operational.REQUEST_PATH_READS`).
        """
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT o.id AS organization_id, o.name AS organization_name, "
                "o.monthly_limit_usd AS organization_limit_usd, "
                "o.monthly_limit_amount AS organization_limit_amount, "
                "mq.usd AS quota_monthly_cost_usd, mq.amount AS quota_monthly_cost_amount, "
                "COALESCE(oc.cost_usd, 0) AS organization_spent_month_usd, "
                "COALESCE(oc.cost_amount, 0) AS organization_spent_month_amount "
                "FROM org_memberships om "
                "JOIN orgs o ON o.id = om.organization_id "
                "JOIN users u ON u.id = om.user_id "
                f"{_own_quota_join('om.user_id')}"
                f"{_MEMBER_QUOTA_JOIN}"
                "LEFT JOIN spend_counters oc ON oc.scope_id = 'o:' || o.id "
                " AND oc.period = 'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM') "
                "WHERE om.user_id = $1",
                user_id,
            )
        return dict(row) if row else None

    # -- what an administrator is shown --------------------------------------
    #
    # Every figure is a sum over the accounts that have a membership in the
    # organization, from when each became a member (:data:`_SINCE_JOINED_LOG`).
    # Spend comes from the cost counters (``user_daily_cost``), the days the
    # quota gates read; requests, tokens and models come from the request log,
    # bounded by the period. One figure is not cut at the joining: what a
    # member spent this month, because that is what its monthly limit is
    # compared with, and the request path counts the whole month. No statement
    # here selects a request's content.

    async def list_organization_administrators(self, organization_id: str) -> list[dict]:
        """The members that have the administrator role, by email."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.user_id, u.email, u.user_name, u.last_login_at "
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.organization_id = $1 AND m.role = 'admin' "
                "ORDER BY lower(COALESCE(u.email, '')), m.user_id",
                organization_id,
            )
        return [dict(row) for row in rows]

    async def get_organization_month_spend(self, organization_id: str) -> dict:
        """What the organization's counter holds for this UTC month.

        The row the request path checks the organization's limit against; an
        organization nothing was charged to this month has none, and reads 0.
        A request-path read
        (:data:`~serving.storage.postgres_operational.REQUEST_PATH_READS`).
        """
        rate = get_accounting_policy().usd_to_currency_rate
        async with self._request_connection() as conn:
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
        limit_state: str | None = None,
        min_today: Any = None,
        min_month: Any = None,
        spent_today: bool = False,
        sort_by: str = "name",
        descending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """One page of members with their figures.

        The requests, the tokens and ``cost`` are those from *first_day* to
        *last_day* (UTC). The spend of today, of the last
        :data:`TREND_DAYS` days, of this month and of all time is the same for
        every period, with one value for each day of the trend (``spend_days``).

        Returns how many members match with the page. *user_id* keeps one
        member; *search* matches a name or an email; *status* is a key of
        :data:`MEMBER_STATUS`; *limit_state* a key of :data:`LIMIT_STATES`;
        *min_today* and *min_month* are amounts in the ledger currency that the
        spend of today and of this month must reach; *spent_today* keeps the
        members that spent anything today; *sort_by* is a key of
        :data:`MEMBER_ORDER`.
        """
        if sort_by not in MEMBER_ORDER:
            raise ValueError(f"unknown member order {sort_by!r}")
        if status is not None and status not in MEMBER_STATUS:
            raise ValueError(f"unknown member status {status!r}")
        if limit_state is not None and limit_state not in LIMIT_STATES:
            raise ValueError(f"unknown limit state {limit_state!r}")
        start, end = utc_bounds(first_day, last_day)
        today = datetime.now(UTC).date()
        trend_start = today - timedelta(days=TREND_DAYS - 1)
        filters = _member_filters(status, user_id, search)
        if limit_state is not None:
            filters.append((f"({_LIMIT_USE}) >= {LIMIT_STATES[limit_state]}", _NO_PARAMETER))
        if min_today is not None:
            filters.append(("COALESCE(fixed.today_amount, 0) >= ${n}", min_today))
        if min_month is not None:
            filters.append(("COALESCE(month.cost_amount, 0) >= ${n}", min_month))
        if spent_today:
            filters.append(("COALESCE(fixed.today_amount, 0) > 0", _NO_PARAMETER))
        params: list[Any] = [
            organization_id,
            get_accounting_policy().usd_to_currency_rate,
            first_day.isoformat(),
            last_day.isoformat(),
            start,
            end,
            today.isoformat(),
            trend_start.isoformat(),
        ]
        where = _render(filters, params)
        direction = "DESC" if descending else "ASC"
        figures = (
            f"WITH {_SCOPE}, "
            "spent AS ("
            f"  SELECT c.user_id, SUM(c.cost_usd) AS cost_usd, SUM({_AMOUNT}) AS cost_amount "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
            f"  WHERE c.day >= $3 AND c.day <= $4 AND {_SINCE_JOINED_DAY} GROUP BY c.user_id), "
            "month AS ("
            f"  SELECT c.user_id, SUM(c.cost_usd) AS cost_usd, SUM({_AMOUNT}) AS cost_amount "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
            f"  WHERE c.day >= {_THIS_MONTH} GROUP BY c.user_id), "
            # Today, the days of the trend, and every day since the joining.
            "fixed AS ("
            "  SELECT c.user_id, "
            "         SUM(c.cost_usd) FILTER (WHERE c.day = $7) AS today_usd, "
            f"         SUM({_AMOUNT}) FILTER (WHERE c.day = $7) AS today_amount, "
            "         SUM(c.cost_usd) FILTER (WHERE c.day >= $8) AS week_usd, "
            f"         SUM({_AMOUNT}) FILTER (WHERE c.day >= $8) AS week_amount, "
            f"         SUM(c.cost_usd) AS alltime_usd, SUM({_AMOUNT}) AS alltime_amount "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
            f"  WHERE {_SINCE_JOINED_DAY} GROUP BY c.user_id), "
            "seen AS ("
            "  SELECT c.user_id, MAX(c.last_request_at) "
            "         FILTER (WHERE c.last_request_at >= s.joined_at) AS last_request_at "
            "  FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id GROUP BY c.user_id), "
            "logged AS ("
            "  SELECT l.user_id, COUNT(*) AS requests, "
            "         COALESCE(SUM(l.total_tokens), 0) AS total_tokens "
            "  FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
            f"  WHERE l.timestamp >= $5 AND l.timestamp < $6 AND {_SINCE_JOINED_LOG} "
            "  GROUP BY l.user_id) "
        )
        # One source for the count and for the page: a filter on a figure
        # narrows both.
        source = (
            "FROM scope s JOIN users u ON u.id = s.user_id "
            "JOIN orgs o ON o.id = $1 "
            f"{_own_quota_join('s.user_id')}"
            "LEFT JOIN spent ON spent.user_id = s.user_id "
            "LEFT JOIN month ON month.user_id = s.user_id "
            "LEFT JOIN fixed ON fixed.user_id = s.user_id "
            "LEFT JOIN seen ON seen.user_id = s.user_id "
            "LEFT JOIN logged ON logged.user_id = s.user_id "
            f"WHERE {where} "
        )
        page = (
            f"{figures}"
            "SELECT s.user_id, s.role, s.joined_at, s.removed_at, "
            "       u.email, u.user_name, u.status, u.role AS account_role, u.last_login_at, "
            f"       {_OWN_QUOTA_COLUMNS}, "
            "       s.monthly_limit_usd AS member_limit_usd, "
            "       s.monthly_limit_amount AS member_limit_amount, "
            "       COALESCE(spent.cost_usd, 0) AS cost_usd, "
            "       COALESCE(spent.cost_amount, 0) AS cost_amount, "
            "       COALESCE(month.cost_usd, 0) AS spent_month_usd, "
            "       COALESCE(month.cost_amount, 0) AS spent_month_amount, "
            "       COALESCE(fixed.today_usd, 0) AS spent_today_usd, "
            "       COALESCE(fixed.today_amount, 0) AS spent_today_amount, "
            "       COALESCE(fixed.week_usd, 0) AS spent_7d_usd, "
            "       COALESCE(fixed.week_amount, 0) AS spent_7d_amount, "
            "       COALESCE(fixed.alltime_usd, 0) AS spent_alltime_usd, "
            "       COALESCE(fixed.alltime_amount, 0) AS spent_alltime_amount, "
            "       COALESCE(logged.requests, 0) AS requests, "
            "       COALESCE(logged.total_tokens, 0) AS total_tokens, "
            "       seen.last_request_at "
            f"{source}"
            f"ORDER BY {MEMBER_ORDER[sort_by]} {direction} NULLS LAST, s.user_id "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"{figures}SELECT COUNT(*) {source}", *params)
            rows = [dict(row) for row in await conn.fetch(page, *params, limit, offset)]
            days = await conn.fetch(
                f"WITH {_SCOPE} "
                f"SELECT c.user_id, c.day, c.cost_usd, {_AMOUNT} AS cost_amount "
                "FROM user_daily_cost c JOIN scope s ON s.user_id = c.user_id "
                f"WHERE c.user_id = ANY($3::text[]) AND c.day >= $4 AND {_SINCE_JOINED_DAY}",
                organization_id,
                params[1],
                [row["user_id"] for row in rows],
                trend_start.isoformat(),
            )
        spent_on = {(row["user_id"], row["day"]): row for row in days}
        trend = [(trend_start + timedelta(days=offset)).isoformat() for offset in range(TREND_DAYS)]
        for row in rows:
            row["spend_days"] = [
                {
                    "day": day,
                    "cost_usd": (spent_on.get((row["user_id"], day)) or {}).get("cost_usd") or 0,
                    "cost_amount": (spent_on.get((row["user_id"], day)) or {}).get("cost_amount")
                    or 0,
                }
                for day in trend
            ]
        return total, rows

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
                f"WHERE c.day >= $3 AND c.day <= $4 AND {_SINCE_JOINED_DAY} "
                "GROUP BY c.day ORDER BY c.day",
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
                f"WHERE l.timestamp >= $3 AND l.timestamp < $4 AND {_SINCE_JOINED_LOG} "
                "GROUP BY 1 ORDER BY 1",
                *params,
            )
            models = await conn.fetch(
                f"WITH {scope} "
                "SELECT l.model_id, COUNT(*) AS requests, "
                "       COUNT(*) FILTER (WHERE l.status_code >= 400) AS errors, "
                "       COALESCE(SUM(l.prompt_tokens), 0) AS prompt_tokens, "
                "       COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens, "
                "       COALESCE(SUM(l.cost_usd), 0) AS cost_usd, "
                f"       COALESCE(SUM({_LOG_AMOUNT}), 0) AS cost_amount, "
                "       COALESCE(SUM(l.cache_read_tokens), 0) AS cached_tokens, "
                "       COALESCE(SUM(l.total_tokens), 0) AS total_tokens "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                f"WHERE l.timestamp >= $3 AND l.timestamp < $4 AND {_SINCE_JOINED_LOG} "
                "GROUP BY l.model_id ORDER BY 6 DESC, 2 DESC, l.model_id",
                *params,
            )
            scope, params = scoped(start, end)
            totals = await conn.fetchrow(
                f"WITH {scope} "
                "SELECT COUNT(*) AS requests, COALESCE(SUM(l.total_tokens), 0) AS total_tokens, "
                "       COUNT(DISTINCT l.user_id) AS members_with_requests "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                f"WHERE l.timestamp >= $2 AND l.timestamp < $3 AND {_SINCE_JOINED_LOG}",
                *params,
            )
            member_count = await conn.fetchval(
                "SELECT COUNT(*) FROM org_memberships WHERE organization_id = $1",
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

    async def get_organization_request_volume(self, organization_id: str) -> dict:
        """The requests of the members in the last hour, day, week and month, to now.

        For each window (:data:`VOLUME_WINDOWS`): how many requests, how many
        were OK and how many failed (a status of 400 or more, as the model
        table counts an error), and the mean latency of those that recorded
        one. One read of the request log, bounded by the longest window.
        """
        now = datetime.now(UTC)
        starts = [now - span for span in VOLUME_WINDOWS.values()]
        columns = []
        for number, name in enumerate(VOLUME_WINDOWS, start=2):
            within = f"l.timestamp >= ${number}"
            columns.append(
                f"COUNT(*) FILTER (WHERE {within}) AS {name}_requests, "
                f"COUNT(*) FILTER (WHERE {within} AND l.status_code < 400) AS {name}_ok, "
                f"COUNT(*) FILTER (WHERE {within} AND l.status_code >= 400) AS {name}_failed, "
                f"AVG(l.latency_ms) FILTER (WHERE {within}) AS {name}_latency"
            )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"WITH {_SCOPE} SELECT {', '.join(columns)} "
                "FROM api_logs l JOIN scope s ON s.user_id = l.user_id "
                f"WHERE l.timestamp >= ${len(starts) + 1} AND {_SINCE_JOINED_LOG}",
                organization_id,
                *starts,
            )
        return {
            name: {
                "requests": row[f"{name}_requests"],
                "ok": row[f"{name}_ok"],
                "failed": row[f"{name}_failed"],
                "avg_latency_ms": (
                    None
                    if row[f"{name}_latency"] is None
                    else round(float(row[f"{name}_latency"]), 1)
                ),
            }
            for name in VOLUME_WINDOWS
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
        where = ["l.timestamp >= $2", "l.timestamp < $3", _SINCE_JOINED_LOG]
        if user_id is not None:
            params.append(user_id)
            where.append(f"l.user_id = ${len(params)}")
        if model:
            params.append(model)
            where.append(f"l.model_id = ${len(params)}")
        if errors is not None:
            where.append("l.status_code >= 400" if errors else "COALESCE(l.status_code, 0) < 400")
        if search:
            params.append(contains_pattern(search))
            like = _LIKE.replace("${n}", f"${len(params)}")
            where.append(
                f"(lower(COALESCE(u.email, '')) {like} OR lower(COALESCE(u.user_name, '')) {like})"
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
        member: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """One page of the audit rows that belong to an organization, newest first.

        *member* keeps the rows an account is the target of, or the acting
        account of. Every row is the organization's already, so the id of an
        account outside it finds nothing.

        With the emails of the acting account and of a user target. *prefixes*
        keeps the actions that start with one of them. *search* looks in the
        action, the details, the email of a user target, and the email of an
        acting account of the organization -- never that of a platform
        administrator, which the organization is not shown: a search that
        matched it would say, one guess at a time, what that address is.
        """
        params: list[Any] = [organization_id]
        where = ["a.organization_id = $1"]
        if since is not None:
            params.append(since)
            where.append(f"a.timestamp >= ${len(params)}")
        if prefixes:
            params.append([f"{prefix}%" for prefix in prefixes])
            where.append(f"a.action LIKE ANY(${len(params)}::text[])")
        if member:
            params.append(member)
            where.append(
                f"(a.target_user_id = ${len(params)} "
                f"OR (a.actor_type = 'user' AND a.actor_id = ${len(params)}))"
            )
        if search:
            params.append(contains_pattern(search))
            like = _LIKE.replace("${n}", f"${len(params)}")
            where.append(
                f"(lower(a.action) {like} "
                f"OR (a.actor_type = 'user' "
                f"AND lower(COALESCE(actor.email, '')) {like}) "
                f"OR lower(COALESCE(target.email, '')) {like} "
                f"OR lower(COALESCE(a.details::text, '')) {like})"
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
