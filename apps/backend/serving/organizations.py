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
from serving.accounting import get_accounting_policy, money_decimal
from serving.audit import AuditActor, AuditTarget
from serving.auth.two_factor import has_authenticator
from serving.config import sso as sso_config
from serving.config.settings import is_admin_email
from serving.model_access import (
    ORGANIZATION_DISABLED_MODELS_KEY,
    disabled_models_of,
    get_disabled_models_from_preferences,
    normalize_disabled_models,
)
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
        answer = lookup(user_id)
        row = await answer if inspect.isawaitable(answer) else None
    except OrganizationsUnavailableError:
        return None
    # A membership is a row; anything else a store answers with is none.
    return dict(row) if isinstance(row, Mapping) else None


async def organization_of_provider(op_store: Any, provider_id: str) -> str | None:
    """The organization an SSO connection belongs to, or None.

    None too in a store that keeps no organizations.
    """
    lookup = getattr(op_store, "get_provider_organization", None)
    if lookup is None:
        return None
    try:
        answer = lookup(provider_id)
        owner = await answer if inspect.isawaitable(answer) else None
    except OrganizationsUnavailableError:
        return None
    return owner if isinstance(owner, str) else None


async def organization_provider_for(op_store: Any, email: str | None) -> SsoProvider | None:
    """The SSO connection of an organization that answers for *email*, if there is one.

    The members of that organization sign in through it only. Decided from the
    domain of the address and the connections that are attached: no account is
    read, so the answer says nothing about whether one exists.
    """
    if not email or "@" not in email:
        return None
    provider = sso_config.provider_for_email(email)
    if provider is None or await organization_of_provider(op_store, provider.id) is None:
        return None
    return provider


async def disabled_models_of_account(op_store: Any, user_id: str) -> list[str]:
    """The models that are off for an account that signed in to the console.

    A session carries no key lookup, so the two lists are read here: the
    platform administrator's, in the preferences of the account, and the one of
    its organization, on the membership (:func:`serving.model_access.disabled_models_of`).
    Not the organization's for an administrator, of the organization or of the
    platform: the key lookups leave it out for those too.
    """
    preferences = await op_store.get_user_preferences(user_id)
    membership = await membership_of(op_store, user_id)
    held = membership is not None and not is_protected(membership)
    return disabled_models_of(
        {"preferences": preferences}, membership.get("disabled_models") if held else None
    )


#: What a member is told when it asks for a password.
SSO_ONLY_DETAIL = (
    "This account belongs to an organization and signs in through single sign-on. "
    "It cannot have a password."
)


def is_sso_only(user: Mapping[str, Any], membership: Mapping[str, Any] | None) -> bool:
    """Whether an account signs in through single sign-on only.

    A member of an organization that is not a platform administrator: the
    organization owns the account, so its identity provider decides who gets
    in. A platform administrator (the role, or an ``ADMIN_EMAILS`` address,
    which becomes one on its next token) keeps every way in, for the day the
    provider is not available.
    """
    return membership is not None and not is_platform_administrator(user)


def is_platform_administrator(user: Mapping[str, Any]) -> bool:
    """Whether an account is a platform administrator, or becomes one on its next token."""
    return user.get("role") == "admin" or is_admin_email(user.get("email"))


async def signs_in_through_sso_only(op_store: Any, user: Mapping[str, Any]) -> bool:
    """:func:`is_sso_only` for an account whose membership is not read yet."""
    return is_sso_only(user, await membership_of(op_store, user["id"]))


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
    organization columns, and the monthly quota of the account, the lower of
    its own and what its organization has for it (the limit it set for the
    member, else its default). None for an account in no organization, and
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
    "access": ("organization.member_models.", "organization.member_key."),
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


def _has(limit: tuple[Any, Any]) -> bool:
    """Whether a limit, a USD value and a native one, is set in either."""
    return limit[0] is not None or limit[1] is not None


def _organization_limit_applies(limit: tuple[Any, Any], own: tuple[Any, Any]) -> bool:
    """Whether the organization's *limit* is the one the request path enforces, not *own*.

    The rule of :func:`serving.storage.organizations.member_quota_join`, on the
    same two pairs: the lower applies, the organization's when they are equal,
    compared in the native amounts where both have one, else in USD.
    """
    if not _has(limit):
        return False
    if not _has(own):
        return True
    if limit[1] is not None and own[1] is not None:
        return limit[1] <= own[1]
    return limit[0] is not None and own[0] is not None and limit[0] <= own[0]


def member_view(row: dict[str, Any], organization: dict[str, Any]) -> dict[str, Any]:
    """A member as its organization is shown it, with the monthly limit that applies to it.

    Two parties can limit a member. Its organization: the limit it set for the
    member, else its default, and nothing for an administrator, whose limit
    is not the organization's to set. The platform administrator: the monthly
    quota of the account. The lower of the two applies, as the request path
    enforces it, and ``monthly_limit_source`` names it.

    A limit the organization set before the member became an administrator
    stays on its row and is not shown: it applies again when the role goes.
    """
    platform = (row.get("quota_monthly_cost_usd"), row.get("quota_monthly_cost_amount"))
    own: tuple[Any, Any] = (row.get("member_limit_usd"), row.get("member_limit_amount"))
    default = (
        organization.get("default_member_monthly_limit_usd"),
        organization.get("default_member_monthly_limit_amount"),
    )
    if is_protected(row):
        own, source, limit = (None, None), "none", (None, None)
    elif _has(own):
        source, limit = "member", own
    elif _has(default):
        source, limit = "default", default
    else:
        source, limit = "none", (None, None)
    if _has(platform) and not _organization_limit_applies(limit, platform):
        source, limit = "platform", platform
    limit_usd, limit_amount = limit
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
        "spent_today_usd": row.get("spent_today_usd") or 0,
        "spent_today_amount": row.get("spent_today_amount"),
        "spent_7d_usd": row.get("spent_7d_usd") or 0,
        "spent_7d_amount": row.get("spent_7d_amount"),
        "spent_alltime_usd": row.get("spent_alltime_usd") or 0,
        "spent_alltime_amount": row.get("spent_alltime_amount"),
        "spend_days": row.get("spend_days") or [],
        "monthly_limit_usd": limit_usd,
        "monthly_limit_amount": limit_amount,
        "monthly_limit_source": source,
        "own_monthly_limit_usd": own[0],
        "own_monthly_limit_amount": own[1],
        "platform_monthly_limit_usd": platform[0],
        "platform_monthly_limit_amount": platform[1],
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
    """One point per UTC day of the span, or per hour of it; quiet ones included.

    A day's spend is the cost counters'; an hour's is the request log's, the
    only place an hour is recorded. An hour that has not begun has no point:
    a zero for it would read as a fall to nothing, not as a time to come.
    """
    logged = {row["bucket"].replace(tzinfo=UTC): row for row in usage["buckets"]}
    spent = {row["day"]: row for row in usage["spend_days"]}
    days = (last_day - first_day).days + 1
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    if hourly:
        begun = ((now or datetime.now(UTC)) - start) // timedelta(hours=1) + 1
        buckets = [start + timedelta(hours=hour) for hour in range(max(0, min(24 * days, begun)))]
    else:
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


#: The periods whose series can have one point for each hour.
HOURLY_PERIODS = ("1d", "7d")
#: How many members the overview names by their spend.
TOP_MEMBERS = 5


async def overview_view(
    op_store: Any, organization_id: str, period: str, granularity: str | None = None
) -> dict[str, Any] | None:
    """The organization's usage in *period*, with the period before for ``1d`` and ``7d``.

    The series has one point for each hour of ``1d`` and for each day of a
    longer period, unless *granularity* (``hour`` or ``day``) says otherwise;
    ``hour`` is for a period of :data:`HOURLY_PERIODS`.
    """
    if granularity == "hour" and period not in HOURLY_PERIODS:
        raise ValueError(f"no hourly series for the period {period!r}")
    organization = await op_store.get_organization(organization_id)
    if organization is None:
        return None
    first_day, last_day = period_span(period)
    hourly = granularity == "hour" or (granularity is None and period == "1d")
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
    _total_members, by_spend = await op_store.list_organization_members(
        organization_id,
        first_day=first_day,
        last_day=last_day,
        sort_by="spend",
        descending=True,
        limit=TOP_MEMBERS,
    )
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
        # Only those that spent or asked anything: a list of zeros ranks nobody.
        "top_members": [
            member_view(row, organization)
            for row in by_spend
            if row.get("cost_usd") or row.get("requests")
        ],
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


#: How many members a card above the member list names.
SUMMARY_TOP = 5


async def members_summary_view(op_store: Any, organization_id: str) -> dict[str, Any] | None:
    """The two cards above the member list; None for no such organization.

    Each card is the member list under one filter and one order, so a card
    and the list it opens never disagree.
    """
    near = await members_view(
        op_store,
        organization_id,
        "30d",
        limit_state="near",
        sort_by="limit_use",
        descending=True,
        limit=SUMMARY_TOP,
    )
    if near is None:
        return None
    spenders = await members_view(
        op_store,
        organization_id,
        "30d",
        spent_today=True,
        sort_by="today",
        descending=True,
        limit=SUMMARY_TOP,
    )
    assert spenders is not None  # the organization was just read
    return {
        "near_limit": {"count": near[0], "top": near[1]},
        "top_spenders_today": {"count": spenders[0], "top": spenders[1]},
    }


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
    account = await op_store.get_user_by_id(user_id) or {}
    keys = [key_view(row) for row in await op_store.list_account_keys(user_id)]
    return {
        "member": found[1][0],
        "account": {
            "has_key": any(key["status"] == "active" for key in keys),
            "two_factor_enabled": has_authenticator(await op_store.get_two_factor(user_id)),
            "email_verified": bool(account.get("email_verified")),
        },
        "days": days,
        "models": usage["models"],
        "requests": [request_view(row) for row in requests],
        "projects": [
            await project_view(op_store, row) for row in await op_store.list_projects(user_id)
        ],
        "keys": keys,
        **member_models(account),
    }


def member_models(account: Mapping[str, Any]) -> dict[str, list[str]]:
    """The models that are off for a member, by who turned them off."""
    return {
        "disabled_models": normalize_disabled_models(account.get(ORGANIZATION_DISABLED_MODELS_KEY)),
        "platform_disabled_models": get_disabled_models_from_preferences(
            account.get("preferences")
        ),
    }


async def project_view(op_store: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """A project of a member as its organization is shown it: its name and its spend."""
    today, month = await op_store.get_project_spend(row["id"])
    view: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "is_default": bool(row.get("is_default")),
        "created_at": row["created_at"],
        "spent_today_usd": today,
        "spent_month_usd": month,
    }
    if get_accounting_policy().native:
        today_amount, month_amount = await op_store.get_project_spend_amount(row["id"])
        view.update(spent_today_amount=today_amount, spent_month_amount=month_amount)
    return view


def key_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """An API key of a member as its organization is shown it: by its name.

    Never the value, the prefix that begins it, or the limits its owner set.
    """
    view: dict[str, Any] = {
        "id": row["id"],
        "name": row.get("name"),
        "status": row["status"],
        "project_id": row.get("project_id"),
        "project_name": row.get("project_name"),
        "created_at": row["created_at"],
        "last_used_at": row.get("last_used_at"),
        "spent_today_usd": float(row.get("spent_today_usd") or 0),
        "spent_month_usd": float(row.get("spent_month_usd") or 0),
    }
    if get_accounting_policy().native:
        view.update(
            spent_today_amount=row.get("spent_today_amount"),
            spent_month_amount=row.get("spent_month_amount"),
        )
    return view


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
    member: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of the organization's audit log, newest first.

    *member* keeps the rows that have that account as target or as actor.
    """
    since = None
    if period is not None:
        first_day, _ = period_span(period)
        since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC)
    total, rows = await op_store.list_organization_audit(
        organization_id,
        since=since,
        prefixes=AUDIT_CATEGORIES[category] if category else (),
        search=search,
        member=member,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "entries": [audit_entry_view(row) for row in rows]}
