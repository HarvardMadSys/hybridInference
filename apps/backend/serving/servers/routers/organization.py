"""The organization console: what an organization administrator sees and changes.

Every route is for a member whose membership has the administrator role
(:func:`serving.servers.deps.require_organization_admin`), and works in the
organization of that membership. No route takes an organization from the
request, so an administrator cannot reach another organization by naming it.

A member in a path must belong to the caller's organization; any other account
answers 404, as one that does not exist would.

No route returns the content of a request or the value of an API key.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TC003 -- FastAPI reads the annotation at runtime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from serving import organizations
from serving.accounting import get_accounting_policy
from serving.model_access import route_canonical_model_id
from serving.schemas_organization import (
    ListMembersResponse,
    ListOrganizationAuditResponse,
    ListOrganizationRequestsResponse,
    MemberDetailResponse,
    MemberModelsResponse,
    MembersSummaryResponse,
    ModelUsageResponse,
    OrganizationMember,
    OrganizationMessageResponse,
    OrganizationOverviewResponse,
    OrganizationResponse,
    Period,
    RequestVolumeResponse,
    SetMemberModelsRequest,
    SetMonthlyLimitRequest,
)
from serving.servers.deps import (
    OrganizationAdmin,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
    require_organization_admin,
)
from serving.servers.routers.models import _build_model_list_async
from serving.storage import organizations as org_store

router = APIRouter(prefix="/user/organization", tags=["Organization"])

MemberOrder = Literal[
    "name",
    "status",
    "last_request",
    "requests",
    "tokens",
    "spend",
    "today",
    "week",
    "month",
    "alltime",
    "limit_use",
    "joined",
]
MemberStatus = Literal["active", "suspended", "removed", "pending_approval"]
AuditCategory = Literal["membership", "limits", "access", "organization"]

_NOT_A_MEMBER = "This account is not a member of your organization."
_ABOVE_LIMIT = "A member's limit cannot be greater than the limit of the organization."


def _limit(payload: SetMonthlyLimitRequest) -> tuple[Any, Any]:
    """The limit a request sent, as ``(USD, native)``: the native amount only when it sent one.

    Raises:
        HTTPException: 400 when the request names no limit at all; ``null`` clears one.
    """
    sent = payload.model_fields_set
    if not sent & {"monthly_limit_usd", "monthly_limit"}:
        raise HTTPException(400, "Send monthly_limit_usd, or null for no limit.")
    native = get_accounting_policy().native and "monthly_limit" in sent
    return payload.monthly_limit_usd, payload.monthly_limit if native else None


@router.get("", response_model=OrganizationResponse)
async def get_organization(
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """The caller's organization: its SSO connections, administrators, limits and month's spend."""
    view = await organizations.organization_view(op_store, admin.organization_id)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationResponse(**view)


@router.get("/overview", response_model=OrganizationOverviewResponse)
async def get_overview(
    period: Period = "7d",
    granularity: Literal["hour", "day"] | None = None,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationOverviewResponse:
    """The organization's usage in a period: totals, a series, the models and the top members.

    The series has one point for each hour of ``1d`` and for each day of a
    longer period. ``granularity`` chooses: ``hour`` is for ``1d`` and ``7d``.

    Raises:
        HTTPException: 400 for an hourly series of ``30d``.
    """
    if granularity == "hour" and period not in organizations.HOURLY_PERIODS:
        raise HTTPException(400, "An hourly series is for a period of 1d or 7d.")
    view = await organizations.overview_view(op_store, admin.organization_id, period, granularity)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationOverviewResponse(**view)


@router.get("/members", response_model=ListMembersResponse)
async def list_members(
    period: Period = "30d",
    search: str | None = Query(default=None, max_length=200),
    status: MemberStatus | None = None,
    limit_state: Literal["near", "over"] | None = None,
    min_cost_today: Decimal | None = Query(default=None, ge=0),
    min_cost_month: Decimal | None = Query(default=None, ge=0),
    currency: Literal["USD", "CNY"] | None = None,
    sort_by: MemberOrder = "name",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListMembersResponse:
    """One page of the organization's members.

    Each row has the spend of today, of the last 7 days, of this month and of
    all time, whatever the period; ``period`` is that of its requests and
    tokens. ``limit_state`` keeps the members near (80% or more) or over
    their monthly limit. ``min_cost_today`` and ``min_cost_month`` are in
    ``currency`` when it is given, and in USD otherwise.

    Raises:
        HTTPException: 409 when a threshold is in a currency that is not the
            ledger's any more: the console read it before the currency changed.
    """
    policy = get_accounting_policy()
    thresholds = {"min_today": min_cost_today, "min_month": min_cost_month}
    if currency is not None and any(value is not None for value in thresholds.values()):
        if currency != policy.currency:
            raise HTTPException(409, "Accounting currency changed; reload the filters")
    else:
        thresholds = {
            name: None if value is None else policy.from_usd(value)
            for name, value in thresholds.items()
        }
    found = await organizations.members_view(
        op_store,
        admin.organization_id,
        period,
        search=search,
        status=status,
        limit_state=limit_state,
        **thresholds,
        sort_by=sort_by,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )
    if found is None:
        raise HTTPException(404, "No such organization.")
    total, members = found
    return ListMembersResponse(total=total, members=[OrganizationMember(**m) for m in members])


# Before ``/members/{user_id}``: the first route that matches a path answers it.
@router.get("/members/summary", response_model=MembersSummaryResponse)
async def get_members_summary(
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> MembersSummaryResponse:
    """The two cards above the member list: near or over the limit, and today's top spenders."""
    view = await organizations.members_summary_view(op_store, admin.organization_id)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return MembersSummaryResponse(**view)


@router.get("/members/{user_id}", response_model=MemberDetailResponse)
async def get_member(
    user_id: str,
    period: Period = "7d",
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> MemberDetailResponse:
    """One member: its usage, its spend for each of the last 30 days, its last requests.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's organization.
    """
    view = await organizations.member_detail_view(op_store, admin.organization_id, user_id, period)
    if view is None:
        raise HTTPException(404, _NOT_A_MEMBER)
    return MemberDetailResponse(**view)


@router.post("/members/{user_id}/remove", response_model=OrganizationMessageResponse)
async def remove_member(
    user_id: str,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Remove a member: suspend its account, as Admin does.

    Its sessions end and its API keys are revoked; a sign-in is refused,
    through single sign-on too. Its usage history stays. Not for an
    administrator, of the organization or of the platform.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's
            organization; 409 for an administrator, or an account that is not active.
    """
    outcome = await op_store.remove_organization_member(
        admin.organization_id,
        user_id,
        suspension_message=f"Removed from {admin.organization_name} by its administrator.",
        actor=admin.actor,
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, _NOT_A_MEMBER)
    if outcome == org_store.PROTECTED:
        raise HTTPException(409, "An administrator cannot be removed here.")
    if outcome == org_store.NOT_ACTIVE:
        raise HTTPException(409, "Only an active member can be removed.")
    return OrganizationMessageResponse(message="Member removed.")


@router.post("/members/{user_id}/restore", response_model=OrganizationMessageResponse)
async def restore_member(
    user_id: str,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Make a member the organization removed active again.

    The revoked API keys stay revoked: the member makes new ones.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's
            organization; 409 when the organization did not remove it (an
            account a platform administrator suspended is theirs to restore).
    """
    outcome = await op_store.restore_organization_member(
        admin.organization_id, user_id, actor=admin.actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, _NOT_A_MEMBER)
    if outcome == org_store.NOT_REMOVED:
        raise HTTPException(
            409,
            "This member was not removed by the organization. A platform administrator "
            "restores an account they suspended.",
        )
    return OrganizationMessageResponse(message="Member restored.")


@router.put("/members/{user_id}/monthly-limit", response_model=OrganizationMessageResponse)
async def set_member_monthly_limit(
    user_id: str,
    payload: SetMonthlyLimitRequest,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Set the monthly limit of a member, or clear it with ``null``.

    The limit is the organization's own. The monthly quota of the member's
    account is the platform administrator's and is not changed: the gateway
    enforces the lower of the two, so this lowers what a member may spend and
    never raises it above that quota. A member without a limit takes the
    default of the organization. Not for an administrator.

    Raises:
        HTTPException: 400 when the limit is above the organization's own;
            404 when the account is not a member of the caller's
            organization; 409 for an administrator.
    """
    usd, native = _limit(payload)
    outcome, _old, _new = await op_store.set_organization_member_limit(
        admin.organization_id, user_id, usd=usd, amount=native, actor=admin.actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, _NOT_A_MEMBER)
    if outcome == org_store.PROTECTED:
        raise HTTPException(409, "The limit of an administrator is not set here.")
    if outcome == org_store.ABOVE_LIMIT:
        raise HTTPException(400, _ABOVE_LIMIT)
    if usd is None and native is None:
        return OrganizationMessageResponse(message="Monthly limit cleared.")
    return OrganizationMessageResponse(message="Monthly limit saved.")


async def _member_account(op_store: Any, admin: OrganizationAdmin, user_id: str) -> dict[str, Any]:
    """The account row of a member of the caller's organization.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's organization.
    """
    membership = await organizations.membership_of(op_store, user_id)
    account = await op_store.get_user_by_id(user_id) if membership else None
    if not account or membership["organization_id"] != admin.organization_id:
        raise HTTPException(404, _NOT_A_MEMBER)
    return account


@router.get("/members/{user_id}/models", response_model=MemberModelsResponse)
async def get_member_models(
    user_id: str,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
) -> MemberModelsResponse:
    """The models the organization can turn off for a member, and those that are off.

    ``available`` is what the platform lets the member use: the list the member
    would see, without what the organization itself turned off.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's organization.
    """
    account = await _member_account(op_store, admin, user_id)
    lists = organizations.member_models(account)
    listed = await _build_model_list_async(
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        model_visibility_resolver=model_visibility_resolver,
        user_ctx={
            "user_id": user_id,
            "role": account.get("role") or "free",
            "is_admin": account.get("role") == "admin",
            "disabled_models": lists["platform_disabled_models"],
        },
    )
    routes = _model_routes(router_exec)
    available = {
        route_canonical_model_id(routes[item.id], item.id) if item.id in routes else item.id
        for item in listed.data
    }
    return MemberModelsResponse(available=sorted(available), **lists)


def _model_routes(router_exec: Any) -> dict[str, Any]:
    """Every route of the deployment by the name it is listed under, chat and embedding."""
    return {**getattr(router_exec, "embedding_routes", {}), **router_exec.routes}


@router.put("/members/{user_id}/models", response_model=MemberModelsResponse)
async def set_member_models(
    user_id: str,
    payload: SetMemberModelsRequest,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
) -> MemberModelsResponse:
    """Replace the models the organization turned off for a member.

    The list is the organization's own. The list of the platform administrator
    is not changed: the gateway refuses a model in either, so this turns a
    model off for a member and never turns on one the platform administrator
    turned off. An id that names no model of the deployment is dropped, as
    Admin drops it. Not for an administrator.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's
            organization; 409 for an administrator.
    """
    known = {
        route_canonical_model_id(route, name) for name, route in _model_routes(router_exec).items()
    }
    outcome, _old, new = await op_store.set_organization_member_models(
        admin.organization_id,
        user_id,
        [model for model in payload.disabled_models if model in known],
        actor=admin.actor,
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, _NOT_A_MEMBER)
    if outcome == org_store.PROTECTED:
        raise HTTPException(409, "The models of an administrator are not set here.")
    account = await _member_account(op_store, admin, user_id)
    lists = organizations.member_models(account)
    return MemberModelsResponse(available=sorted(known), **{**lists, "disabled_models": new})


@router.post("/members/{user_id}/keys/{key_id}/revoke", response_model=OrganizationMessageResponse)
async def revoke_member_key(
    user_id: str,
    key_id: int,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Revoke one API key of a member, as its owner does in the console.

    The key stays revoked: the member makes a new one. Not for an administrator.

    Raises:
        HTTPException: 404 when the account is not a member of the caller's
            organization, or the key is not that member's; 409 for an
            administrator, or a key that is not active.
    """
    outcome, _identity = await op_store.revoke_organization_member_key(
        admin.organization_id, user_id, key_id, actor=admin.actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "This member has no such API key.")
    if outcome == org_store.PROTECTED:
        raise HTTPException(409, "The API keys of an administrator are not revoked here.")
    if outcome == org_store.NOT_ACTIVE:
        raise HTTPException(409, "This API key is not active.")
    return OrganizationMessageResponse(message="API key revoked.")


@router.put("/default-member-limit", response_model=OrganizationResponse)
async def set_default_member_limit(
    payload: SetMonthlyLimitRequest,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Set the default monthly limit of a member, or clear it with ``null``.

    It applies at once to every member the organization set no limit for,
    and not to an administrator, of the organization or of the platform.

    Raises:
        HTTPException: 400 when the default is above the organization's own limit.
    """
    usd, native = _limit(payload)
    outcome, _old, _new = await op_store.set_organization_limit(
        admin.organization_id,
        "default_member_monthly_limit",
        usd=usd,
        amount=native,
        action="organization.default_member_limit.set",
        actor=admin.actor,
    )
    if outcome == org_store.ABOVE_LIMIT:
        raise HTTPException(400, _ABOVE_LIMIT)
    view = await organizations.organization_view(op_store, admin.organization_id)
    if view is None or outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "No such organization.")
    return OrganizationResponse(**view)


@router.get("/usage/volume", response_model=RequestVolumeResponse)
async def get_request_volume(
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> RequestVolumeResponse:
    """The requests of the members in the last hour, day, week and month, to now.

    For each window: the requests, how many were OK, how many failed, and the
    average latency. The windows do not follow the period of the page.
    """
    return RequestVolumeResponse(
        **await op_store.get_organization_request_volume(admin.organization_id)
    )


@router.get("/usage/requests", response_model=ListOrganizationRequestsResponse)
async def list_requests(
    period: Period = "7d",
    search: str | None = Query(default=None, max_length=200),
    model: str | None = Query(default=None, max_length=256),
    status: Literal["ok", "errors"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None, max_length=64),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListOrganizationRequestsResponse:
    """The members' requests in a period, newest first, without their content.

    ``before`` is the ``next_before`` of the page before.

    Raises:
        HTTPException: 400 for a ``before`` this route did not return.
    """
    cursor: Any = None
    if before is not None:
        cursor = organizations.parse_request_cursor(before)
        if cursor is None:
            raise HTTPException(400, "before is not a value this route returned.")
    view = await organizations.requests_view(
        op_store,
        admin.organization_id,
        period,
        limit=limit,
        before=cursor,
        search=search,
        model=model,
        errors=None if status is None else status == "errors",
    )
    return ListOrganizationRequestsResponse(**view)


@router.get("/usage/models", response_model=ModelUsageResponse)
async def list_model_usage(
    period: Period = "7d",
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ModelUsageResponse:
    """What each model was used for by the organization's members in a period."""
    return ModelUsageResponse(
        **await organizations.models_view(op_store, admin.organization_id, period)
    )


@router.get("/usage/members", response_model=ListMembersResponse)
async def list_member_usage(
    period: Period = "7d",
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListMembersResponse:
    """The members by their spend in a period, largest first."""
    found = await organizations.members_view(
        op_store,
        admin.organization_id,
        period,
        search=search,
        sort_by="spend",
        descending=True,
        limit=limit,
        offset=offset,
    )
    if found is None:
        raise HTTPException(404, "No such organization.")
    total, members = found
    return ListMembersResponse(total=total, members=[OrganizationMember(**m) for m in members])


@router.get("/audit-log", response_model=ListOrganizationAuditResponse)
async def list_audit_log(
    period: Period | None = None,
    category: AuditCategory | None = None,
    member: str | None = Query(default=None, max_length=64),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListOrganizationAuditResponse:
    """The management actions of the caller's organization, newest first.

    ``member`` keeps the rows that have that account as target or as actor.
    Each entry has its details in full: for a change, the old value and the
    new one. No entry has the address the action came from.
    """
    return ListOrganizationAuditResponse(
        **await organizations.audit_view(
            op_store,
            admin.organization_id,
            period=period,
            category=category,
            member=member,
            search=search,
            limit=limit,
            offset=offset,
        )
    )
