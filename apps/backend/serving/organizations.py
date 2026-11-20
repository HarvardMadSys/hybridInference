"""Organizations: the member rule, and what an organization is shown as.

An organization is a record of its own (:mod:`serving.storage.organizations`)
that owns ``SSO_PROVIDERS`` entries, its SSO connections. An account is a
member when its email is in the ``domains`` of one of those connections and it
has an identity of that connection. The store writes the membership with the
event that makes the rule true (a first sign-in, a link, an attach); this
module holds the half of the rule the store cannot know, the provider
document, and tells the audit log when an account joined.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving import quota
from serving.accounting import money_decimal
from serving.audit import AuditActor, AuditTarget
from serving.config import sso as sso_config
from serving.storage.base import OrganizationsUnavailableError
from serving.storage.organizations import is_protected
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.config.sso import SsoProvider

logger = get_logger(__name__)


def configured_provider(provider_id: str) -> SsoProvider | None:
    """The ``SSO_PROVIDERS`` entry with this id, switched on or off.

    An organization keeps a connection whose provider is switched off, so this
    looks past ``enabled``, unlike :func:`serving.config.sso.provider_by_id`.
    """
    return next((p for p in sso_config.get_sso_document().providers if p.id == provider_id), None)


def may_link(provider: SsoProvider, account_email: str | None) -> bool:
    """Whether an account with this email may link an identity of *provider*.

    Only an account the provider answers for. An account outside its domains
    would gain nothing from the organization, and a sign-in through the
    organization would then open a personal account.
    """
    return provider.answers_for(account_email)


def accounts_of_connection(
    provider: SsoProvider, identities: Iterable[Mapping[str, Any]]
) -> list[str]:
    """The accounts attaching *provider* to an organization makes members.

    Every account with an identity among *identities* that signs in through
    *provider*, and with an email *provider* answers for. *identities* is every
    linked identity of an account that is in no organization, as the store
    reads it while it attaches the connection: it calls this as the rule of
    the connection (:data:`serving.storage.organizations.AccountsOf`), so that
    no sign-in comes between that read and the connection.
    """
    accounts: list[str] = []
    for row in identities:
        served = sso_config.serves_identity(
            provider, row["provider_id"], row["issuer"], row.get("email")
        )
        if served and provider.answers_for(row.get("account_email")):
            accounts.append(row["user_id"])
    # An account holds one identity per issuer, so no account is listed twice.
    return accounts


async def membership_of(op_store: Any, user_id: str) -> dict[str, Any] | None:
    """An account's membership; None in a store that keeps no organizations."""
    lookup = getattr(op_store, "get_organization_membership", None)
    if lookup is None:
        return None
    try:
        row = await lookup(user_id)
    except OrganizationsUnavailableError:
        return None
    return dict(row) if row else None


#: The columns an identity carries of the organization its requests are checked
#: against (:func:`serving.quota.organization_fields` reads them).
ORGANIZATION_COLUMNS = (
    "organization_id",
    "organization_name",
    "organization_limit_usd",
    "organization_limit_amount",
    "organization_spent_month_usd",
    "organization_spent_month_amount",
)


async def month_spend_of(op_store: Any, organization_id: str, *, native: bool) -> Any:
    """What an organization's counter holds for this UTC month, in the ledger currency.

    A float of USD, or the exact native amount on a ledger that is not in USD.
    Zero from a store that keeps no organizations.
    """
    reader = getattr(op_store, "get_organization_month_spend", None)
    spent = reader(organization_id) if reader is not None else None
    if inspect.isawaitable(spent):
        spent = await spent
    if not isinstance(spent, Mapping):
        return Decimal(0) if native else 0.0
    if native:
        return money_decimal(spent.get("cost_amount") or 0)
    return float(spent.get("cost_usd") or 0.0)


async def check_fields(op_store: Any, identity: Mapping[str, Any], *, native: bool) -> dict:
    """The organization fields of the spend check for *identity*, with the month's spend.

    The spend arrives with the lookups that read spend. The cached identity
    lookup (the one a request carrying ``X-On-Behalf-Of`` authenticates
    through) carries the organization and its limit, not what it spent: that
    is read here, and only for an organization that has a limit, as a
    project's spend is. Taken as zero, such a request would pass a limit that
    is spent.
    """
    fields = quota.organization_fields(identity, native=native)
    if not fields or fields["organization_limit_usd"] is None or not op_store:
        return fields
    carried = "organization_spent_month_amount" if native else "organization_spent_month_usd"
    if identity.get(carried) is None:
        fields["organization_spent_month_usd"] = await month_spend_of(
            op_store, fields["organization_id"], native=native
        )
    return fields


def columns_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """The organization columns a lookup row carries; none from a lookup that reads none."""
    return {name: row[name] for name in ORGANIZATION_COLUMNS if name in row}


async def spend_scope_of(op_store: Any, user_id: str) -> dict[str, Any] | None:
    """The organization of an account as the request path checks it, read on its own.

    For an account no API-key lookup answered for (it holds no live key): the
    organization columns, and the monthly quota of the member, its own or the
    default of the organization. None for an account in no organization, and
    in a store that keeps none. A store that cannot answer now raises: a
    request whose organization is unknown is not one without a limit.
    """
    lookup = getattr(op_store, "get_organization_spend_scope", None)
    if lookup is None:
        return None
    try:
        answer = lookup(user_id)
        scope = await answer if inspect.isawaitable(answer) else None
    except OrganizationsUnavailableError:
        return None
    return dict(scope) if isinstance(scope, Mapping) else None


async def forget_removal(op_store: Any, user_id: str) -> None:
    """Forget that an organization removed *user_id*; nothing in a store without organizations."""
    forget = getattr(op_store, "clear_organization_member_removal", None)
    if forget is None:
        return
    try:
        await forget(user_id)
    except OrganizationsUnavailableError:
        return


async def joined_organization(
    op_store: Any, user_id: str, before: dict[str, Any] | None
) -> str | None:
    """The organization an event just made *user_id* a member of, if it did.

    *before* is the account's membership before the event: None for an
    account the event created. None when the account was a member already, or
    the provider of the event belongs to no organization.
    """
    if before is not None:
        return None
    membership = await membership_of(op_store, user_id)
    return membership["organization_id"] if membership else None


async def log_join(
    db_logger: Any,
    *,
    user_id: str,
    organization_id: str,
    provider: SsoProvider,
    source_ip: str | None,
) -> None:
    """Record that a sign-in or a link made *user_id* a member of an organization."""
    from serving.servers.auth import log_admin_action

    await log_admin_action(
        db_logger,
        action="organization.member.join",
        details={"sso_provider": provider.id},
        actor=AuditActor.user(user_id, source_ip),
        target=AuditTarget.user(user_id),
        organization_id=organization_id,
    )
    logger.info(f"Account {user_id} joined organization {organization_id} through {provider.id}")


async def is_organization_identity(op_store: Any, user_id: str, identity_id: str) -> bool:
    """Whether *identity_id* is the identity a member has from its own organization.

    The member rule rests on that identity, so its owner cannot disconnect it.
    An identity is the organization's when the provider it signs in through
    is one of the organization's connections; for an identity no entry of
    ``SSO_PROVIDERS`` serves any more, the provider it was linked through
    decides.
    """
    membership = await membership_of(op_store, user_id)
    if membership is None:
        return False
    identity = next(
        (row for row in await op_store.list_user_identities(user_id) if row["id"] == identity_id),
        None,
    )
    if identity is None:
        return False
    provider_id = identity["provider_id"]
    for provider in sso_config.get_sso_document().providers:
        if sso_config.serves_identity(
            provider, identity["provider_id"], identity["issuer"], identity.get("email")
        ):
            provider_id = provider.id
            break
    owner = await op_store.get_provider_organization(provider_id)
    return owner == membership["organization_id"]


def connection_view(provider_id: str) -> dict[str, Any]:
    """An SSO connection as an administrator is shown it: no secret, no client id.

    ``available`` is False when ``SSO_PROVIDERS`` has no entry with this id any
    more: the id was changed or the entry removed, and the connection signs
    nobody in until the platform administrator attaches the new id.
    """
    provider = configured_provider(provider_id)
    if provider is None:
        return {"provider_id": provider_id, "available": False}
    return {
        "provider_id": provider.id,
        "available": True,
        "display_name": provider.display_name,
        "type": provider.type,
        "domains": list(provider.domains),
        "enabled": provider.enabled,
        "auto_create": provider.auto_create,
        "require_approval": provider.require_approval,
    }


# -- what an administrator is shown ------------------------------------------------
#
# Every function takes the organization as an argument: the organization console
# passes the caller's own, and a platform administrator names one.

#: The periods a page may ask for, in UTC days ending today.
PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30}

#: The audit actions of each category the audit log can be narrowed to.
AUDIT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "membership": ("organization.member.", "organization.admin."),
    "limits": (
        "organization.limit.",
        "organization.member_limit.",
        "organization.default_member_limit.",
    ),
    "organization": ("organization.create", "organization.update", "organization.connection."),
}

#: How many requests a member's details show.
MEMBER_RECENT_REQUESTS = 6
#: How many days of spend a member's details show.
MEMBER_SPEND_DAYS = 30

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: The largest id a ``BIGINT`` row has.
_BIGINT_MAX = 2**63 - 1


def period_span(period: str, today: date | None = None) -> tuple[date, date]:
    """The first and the last UTC day of *period*; the last one is today."""
    last = today or datetime.now(UTC).date()
    return last - timedelta(days=PERIOD_DAYS[period] - 1), last


def request_cursor(row: dict[str, Any]) -> str:
    """Where a request list goes on after *row*: its instant in microseconds, and its id."""
    microseconds = (row["timestamp"] - _EPOCH) // timedelta(microseconds=1)
    return f"{microseconds}-{row['id']}"


def parse_request_cursor(cursor: str) -> tuple[datetime, int] | None:
    """Read a cursor :func:`request_cursor` made; None for anything else.

    Its two numbers are ASCII digits only (``"²".isdigit()`` is true, and
    ``int`` refuses it), and the row id is one the column can hold.
    """
    microseconds, _, row_id = cursor.partition("-")
    if not (_is_number(microseconds) and _is_number(row_id)):
        return None
    if int(row_id) > _BIGINT_MAX:
        return None
    try:
        return _EPOCH + timedelta(microseconds=int(microseconds)), int(row_id)
    except OverflowError:
        return None


def _is_number(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _total(rows: list[dict[str, Any]], key: str) -> Any:
    return sum((row[key] or 0 for row in rows), 0)


def member_view(row: dict[str, Any], organization: dict[str, Any]) -> dict[str, Any]:
    """A member as its organization is shown it, with where its monthly limit comes from.

    A member's own monthly quota wins; without one the organization's default
    applies, and without that there is no limit.
    """
    own_usd, own_amount = row.get("quota_monthly_cost_usd"), row.get("quota_monthly_cost_amount")
    default_usd = organization.get("default_member_monthly_limit_usd")
    default_amount = organization.get("default_member_monthly_limit_amount")
    if own_usd is not None or own_amount is not None:
        source, limit_usd, limit_amount = "member", own_usd, own_amount
    elif default_usd is not None or default_amount is not None:
        source, limit_usd, limit_amount = "default", default_usd, default_amount
    else:
        source, limit_usd, limit_amount = "none", None, None
    return {
        "user_id": row["user_id"],
        "email": row.get("email"),
        "user_name": row.get("user_name"),
        "role": row["role"],
        "protected": is_protected(row),
        "status": row["status"],
        "removed": row.get("removed_at") is not None and row["status"] == "suspended",
        "joined_at": row["joined_at"],
        "last_login_at": row.get("last_login_at"),
        "last_request_at": row.get("last_request_at"),
        "requests": row.get("requests") or 0,
        "total_tokens": row.get("total_tokens") or 0,
        "cost_usd": row.get("cost_usd") or 0,
        "cost_amount": row.get("cost_amount"),
        "spent_month_usd": row.get("spent_month_usd") or 0,
        "spent_month_amount": row.get("spent_month_amount"),
        "monthly_limit_usd": limit_usd,
        "monthly_limit_amount": limit_amount,
        "monthly_limit_source": source,
    }


def request_view(row: dict[str, Any]) -> dict[str, Any]:
    """A request as an organization is shown it: no content, and a key by its name only."""
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "user_id": row["user_id"],
        "email": row.get("email"),
        "user_name": row.get("user_name"),
        "project_name": row.get("project_name"),
        "key_name": row.get("key_name"),
        "model_id": row["model_id"],
        "status_code": row.get("status_code"),
        "prompt_tokens": row.get("prompt_tokens") or 0,
        "completion_tokens": row.get("completion_tokens") or 0,
        "cost_usd": row.get("cost_usd") or 0,
        "cost_amount": row.get("cost_amount"),
    }


def audit_entry_view(row: dict[str, Any]) -> dict[str, Any]:
    """An audit row as an organization is shown it.

    A platform administrator is named as that and nothing more: its account is
    the deployment's, not the organization's to see.
    """
    action = row["action"]
    category = next(
        (name for name, prefixes in AUDIT_CATEGORIES.items() if action.startswith(prefixes)),
        "organization",
    )
    details = row.get("details")
    if isinstance(details, str):
        details = json.loads(details)
    entry: dict[str, Any] = {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "action": action,
        "category": category,
        "target_type": row.get("target_type"),
        "target_user_id": row.get("target_user_id"),
        "target_email": row.get("target_email"),
        "details": details,
    }
    if row.get("actor_type") in ("admin", "admin_token"):
        entry["actor"] = "platform_administrator"
    elif row.get("actor_type") == "user":
        entry.update(
            actor="member", actor_user_id=row.get("actor_id"), actor_email=row.get("actor_email")
        )
    else:
        entry["actor"] = "system"
    return entry


async def organization_view(op_store: Any, organization_id: str) -> dict[str, Any] | None:
    """One organization with its connections, its administrators and this month's spend."""
    row = await op_store.get_organization(organization_id)
    if row is None:
        return None
    spend = await op_store.get_organization_month_spend(organization_id)
    return {
        **row,
        "spent_month_usd": spend["cost_usd"],
        "spent_month_amount": spend["cost_amount"],
        "connections": [connection_view(provider_id) for provider_id in row["provider_ids"]],
        "administrators": await op_store.list_organization_administrators(organization_id),
    }


def _series(
    usage: dict[str, Any],
    first_day: date,
    last_day: date,
    *,
    hourly: bool,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One point per UTC day of the span, or per hour of its last day; quiet ones included.

    A day's spend is the cost counters'; an hour's is the request log's, the
    only place an hour is recorded. An hour that has not begun has no point:
    a zero for it would read as a fall to nothing, not as a time to come.
    """
    logged = {row["bucket"].replace(tzinfo=UTC): row for row in usage["buckets"]}
    spent = {row["day"]: row for row in usage["spend_days"]}
    if hourly:
        start = datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC)
        begun = ((now or datetime.now(UTC)) - start) // timedelta(hours=1) + 1
        buckets = [start + timedelta(hours=hour) for hour in range(max(0, min(24, begun)))]
    else:
        days = (last_day - first_day).days + 1
        start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
        buckets = [start + timedelta(days=day) for day in range(days)]
    series = []
    for bucket in buckets:
        log = logged.get(bucket) or {}
        cost = log if hourly else (spent.get(bucket.date().isoformat()) or {})
        series.append(
            {
                "bucket": bucket,
                "cost_usd": cost.get("cost_usd") or 0,
                "cost_amount": cost.get("cost_amount") or 0,
                "requests": log.get("requests") or 0,
                "total_tokens": log.get("total_tokens") or 0,
                "members_with_requests": log.get("members") or 0,
            }
        )
    return series


async def overview_view(op_store: Any, organization_id: str, period: str) -> dict[str, Any] | None:
    """The organization's usage in *period*, with the period before for ``1d`` and ``7d``."""
    organization = await op_store.get_organization(organization_id)
    if organization is None:
        return None
    first_day, last_day = period_span(period)
    hourly = period == "1d"
    usage = await op_store.get_organization_usage(
        organization_id, first_day=first_day, last_day=last_day, hourly=hourly
    )
    previous = None
    if period in ("1d", "7d"):
        days = PERIOD_DAYS[period]
        before = await op_store.get_organization_usage(
            organization_id,
            first_day=first_day - timedelta(days=days),
            last_day=first_day - timedelta(days=1),
        )
        previous = {
            "cost_usd": _total(before["spend_days"], "cost_usd"),
            "cost_amount": _total(before["spend_days"], "cost_amount"),
            "requests": before["requests"],
        }
    spend = await op_store.get_organization_month_spend(organization_id)
    return {
        "period": period,
        "bucket": "hour" if hourly else "day",
        "cost_usd": _total(usage["spend_days"], "cost_usd"),
        "cost_amount": _total(usage["spend_days"], "cost_amount"),
        "requests": usage["requests"],
        "total_tokens": usage["total_tokens"],
        "members_with_requests": usage["members_with_requests"],
        "member_count": usage["member_count"],
        "previous": previous,
        "spent_month_usd": spend["cost_usd"],
        "spent_month_amount": spend["cost_amount"],
        "monthly_limit_usd": organization["monthly_limit_usd"],
        "monthly_limit_amount": organization["monthly_limit_amount"],
        "series": _series(usage, first_day, last_day, hourly=hourly),
        "models": usage["models"],
    }


async def members_view(
    op_store: Any, organization_id: str, period: str, **filters: Any
) -> tuple[int, list[dict[str, Any]]] | None:
    """One page of members with their usage in *period*; None for no such organization."""
    organization = await op_store.get_organization(organization_id)
    if organization is None:
        return None
    first_day, last_day = period_span(period)
    total, rows = await op_store.list_organization_members(
        organization_id, first_day=first_day, last_day=last_day, **filters
    )
    return total, [member_view(row, organization) for row in rows]


async def member_detail_view(
    op_store: Any, organization_id: str, user_id: str, period: str
) -> dict[str, Any] | None:
    """One member's details; None when the account is not a member of the organization."""
    found = await members_view(op_store, organization_id, period, user_id=user_id, limit=1)
    if not found or not found[1]:
        return None
    first_day, last_day = period_span(period)
    usage = await op_store.get_organization_usage(
        organization_id, first_day=first_day, last_day=last_day, user_id=user_id
    )
    today = datetime.now(UTC).date()
    spent = {
        row["day"]: row for row in await op_store.get_user_cost_history(user_id, MEMBER_SPEND_DAYS)
    }
    days = []
    for offset in range(MEMBER_SPEND_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        row = spent.get(day.isoformat()) or {}
        days.append(
            {
                "bucket": datetime(day.year, day.month, day.day, tzinfo=UTC),
                "cost_usd": row.get("cost_usd") or 0,
                "cost_amount": row.get("cost_amount") or 0,
                "requests": row.get("requests") or 0,
            }
        )
    requests = await op_store.list_organization_requests(
        organization_id,
        first_day=first_day,
        last_day=last_day,
        user_id=user_id,
        limit=MEMBER_RECENT_REQUESTS,
    )
    return {
        "member": found[1][0],
        "days": days,
        "models": usage["models"],
        "requests": [request_view(row) for row in requests],
    }


async def requests_view(
    op_store: Any,
    organization_id: str,
    period: str,
    *,
    limit: int,
    before: tuple[datetime, int] | None = None,
    **filters: Any,
) -> dict[str, Any]:
    """One page of the members' requests in *period*, and the cursor of the page after it."""
    first_day, last_day = period_span(period)
    rows = await op_store.list_organization_requests(
        organization_id,
        first_day=first_day,
        last_day=last_day,
        before=before,
        limit=limit + 1,
        **filters,
    )
    page = rows[:limit]
    return {
        "requests": [request_view(row) for row in page],
        "next_before": request_cursor(page[-1]) if len(rows) > limit and page else None,
    }


async def models_view(op_store: Any, organization_id: str, period: str) -> dict[str, Any]:
    """The models the organization's members used in *period*."""
    first_day, last_day = period_span(period)
    usage = await op_store.get_organization_usage(
        organization_id, first_day=first_day, last_day=last_day
    )
    return {"period": period, "models": usage["models"]}


async def audit_view(
    op_store: Any,
    organization_id: str,
    *,
    period: str | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of the organization's audit log, newest first."""
    since = None
    if period is not None:
        first_day, _ = period_span(period)
        since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    total, rows = await op_store.list_organization_audit(
        organization_id,
        since=since,
        prefixes=AUDIT_CATEGORIES[category] if category else (),
        search=search,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "entries": [audit_entry_view(row) for row in rows]}
