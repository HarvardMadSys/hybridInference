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

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from serving import organizations
from serving.accounting import get_accounting_policy
from serving.schemas_organization import (
    ListMembersResponse,
    ListOrganizationAuditResponse,
    ListOrganizationRequestsResponse,
    MemberDetailResponse,
    ModelUsageResponse,
    OrganizationMember,
    OrganizationMessageResponse,
    OrganizationOverviewResponse,
    OrganizationResponse,
    Period,
    SetMonthlyLimitRequest,
)
from serving.servers.deps import (
    OrganizationAdmin,
    get_operational_store,
    require_organization_admin,
)
from serving.storage import organizations as org_store

router = APIRouter(prefix="/user/organization", tags=["Organization"])

MemberOrder = Literal[
    "name", "status", "last_request", "requests", "tokens", "spend", "month", "limit_use", "joined"
]
MemberStatus = Literal["active", "suspended", "removed", "pending_approval"]
AuditCategory = Literal["membership", "limits", "organization"]

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
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationOverviewResponse:
    """The organization's usage in a period: totals, a series, and the spend for each model."""
    view = await organizations.overview_view(op_store, admin.organization_id, period)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationOverviewResponse(**view)


@router.get("/members", response_model=ListMembersResponse)
async def list_members(
    period: Period = "7d",
    search: str | None = Query(default=None, max_length=200),
    status: MemberStatus | None = None,
    sort_by: MemberOrder = "name",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListMembersResponse:
    """One page of the organization's members, with their usage in a period."""
    found = await organizations.members_view(
        op_store,
        admin.organization_id,
        period,
        search=search,
        status=status,
        sort_by=sort_by,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )
    if found is None:
        raise HTTPException(404, "No such organization.")
    total, members = found
    return ListMembersResponse(total=total, members=[OrganizationMember(**m) for m in members])


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

    The limit is the monthly quota of the member's account, the one the
    gateway already enforces. A member without one takes the default of the
    organization. Not for an administrator.

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


@router.put("/default-member-limit", response_model=OrganizationResponse)
async def set_default_member_limit(
    payload: SetMonthlyLimitRequest,
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Set the default monthly limit of a member, or clear it with ``null``.

    It applies at once to every member that has no monthly limit of its own.

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
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: OrganizationAdmin = Depends(require_organization_admin),
    op_store=Depends(get_operational_store),
) -> ListOrganizationAuditResponse:
    """The management actions of the caller's organization, newest first."""
    return ListOrganizationAuditResponse(
        **await organizations.audit_view(
            op_store,
            admin.organization_id,
            period=period,
            category=category,
            search=search,
            limit=limit,
            offset=offset,
        )
    )
