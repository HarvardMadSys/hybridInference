"""Admin routes that set an organization up.

The platform administrator makes an organization, attaches its SSO
connections, gives the role of organization administrator and sets the
organization's monthly limit. What an organization administrator then does in
its own organization is in :mod:`serving.servers.routers.organization`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from serving import organizations
from serving.accounting import get_accounting_policy
from serving.audit import AuditActor  # noqa: TC001 (FastAPI reads the annotation)
from serving.schemas_organization import (
    ConnectionAttachedResponse,
    CreateOrganizationRequest,
    ListOrganizationsResponse,
    OrganizationCreatedResponse,
    OrganizationMessageResponse,
    OrganizationResponse,
    SetMemberRoleRequest,
    UpdateOrganizationRequest,
)
from serving.servers.deps import get_admin_audit_actor, get_operational_store, verify_admin_access
from serving.storage import organizations as org_store
from serving.utils.jwt import generate_ulid

router = APIRouter(prefix="/admin")


def _store(op_store: Any) -> Any:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    return op_store


def _native_limit(payload: Any) -> Any:
    """The native monthly limit a request sent with the accounting ``currency``, exactly."""
    if get_accounting_policy().native and "monthly_limit" in payload.model_fields_set:
        return payload.monthly_limit
    return None


async def _organization(op_store: Any, organization_id: str) -> OrganizationResponse:
    view = await organizations.organization_view(op_store, organization_id)
    if view is None:
        raise HTTPException(404, "No such organization.")
    return OrganizationResponse(**view)


def _provider(provider_id: str) -> Any:
    provider = organizations.configured_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"SSO_PROVIDERS has no provider with the id {provider_id!r}.")
    return provider


@router.get("/organizations", response_model=ListOrganizationsResponse)
async def list_organizations(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListOrganizationsResponse:
    """Every organization of the deployment, by name."""
    store = _store(op_store)
    rows = await store.list_organizations()
    return ListOrganizationsResponse(
        organizations=[await _organization(store, row["id"]) for row in rows]
    )


@router.post("/organizations", response_model=OrganizationCreatedResponse, status_code=201)
async def create_organization(
    payload: CreateOrganizationRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationCreatedResponse:
    """Make an organization from one SSO connection or more.

    Each account that has an identity of one of the connections and an email
    in its domains becomes a member.

    Raises:
        HTTPException: 404 for a provider id ``SSO_PROVIDERS`` does not have;
            409 when the name is another organization's, or a connection
            belongs to another organization.
    """
    store = _store(op_store)
    provider_ids = list(dict.fromkeys(payload.provider_ids))
    connections = [
        (provider_id, await organizations.accounts_of_connection(store, _provider(provider_id)))
        for provider_id in provider_ids
    ]
    organization_id = generate_ulid()
    outcome, added = await store.create_organization(
        organization_id=organization_id,
        name=payload.name.strip(),
        monthly_limit_usd=payload.monthly_limit_usd,
        monthly_limit_amount=_native_limit(payload),
        connections=connections,
        actor=actor,
    )
    if outcome == org_store.NAME_TAKEN:
        raise HTTPException(409, "Another organization has this name.")
    if outcome == org_store.CONNECTION_TAKEN:
        raise HTTPException(409, "One of these SSO connections belongs to another organization.")
    return OrganizationCreatedResponse(
        organization=await _organization(store, organization_id), members_added=added
    )


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    organization_id: str,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """One organization, in the shape its own administrators see it."""
    return await _organization(_store(op_store), organization_id)


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: str,
    payload: UpdateOrganizationRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Change the name or the monthly limit; a field that is not sent stays as it is.

    Raises:
        HTTPException: 404 for no such organization; 409 when the name is
            another organization's.
    """
    store = _store(op_store)
    sent = payload.model_fields_set
    if "name" in sent and payload.name is not None:
        outcome = await store.rename_organization(
            organization_id, payload.name.strip(), actor=actor
        )
        if outcome == org_store.NOT_FOUND:
            raise HTTPException(404, "No such organization.")
        if outcome == org_store.NAME_TAKEN:
            raise HTTPException(409, "Another organization has this name.")
    if sent & {"monthly_limit_usd", "monthly_limit"}:
        outcome, _old, _new = await store.set_organization_limit(
            organization_id,
            "monthly_limit",
            usd=payload.monthly_limit_usd,
            amount=_native_limit(payload),
            action="organization.limit.set",
            actor=actor,
        )
        if outcome == org_store.NOT_FOUND:
            raise HTTPException(404, "No such organization.")
    return await _organization(store, organization_id)


@router.put(
    "/organizations/{organization_id}/connections/{provider_id}",
    response_model=ConnectionAttachedResponse,
)
async def attach_connection(
    organization_id: str,
    provider_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> ConnectionAttachedResponse:
    """Attach an SSO connection to an organization.

    Each account that has an identity of the connection and an email in its
    domains, and is in no organization, becomes a member. Attaching a
    connection the organization already has adds the members it lacks.

    Raises:
        HTTPException: 404 for no such organization or provider; 409 when the
            connection belongs to another organization.
    """
    store = _store(op_store)
    accounts = await organizations.accounts_of_connection(store, _provider(provider_id))
    outcome, added = await store.attach_organization_connection(
        organization_id, provider_id, user_ids=accounts, actor=actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "No such organization.")
    if outcome == org_store.CONNECTION_TAKEN:
        raise HTTPException(409, "This SSO connection belongs to another organization.")
    return ConnectionAttachedResponse(
        organization=await _organization(store, organization_id), members_added=added
    )


@router.delete(
    "/organizations/{organization_id}/connections/{provider_id}",
    response_model=OrganizationResponse,
)
async def detach_connection(
    organization_id: str,
    provider_id: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationResponse:
    """Detach an SSO connection. The members stay.

    New accounts of the connection no longer become members. A connection
    whose provider ``SSO_PROVIDERS`` no longer has is detached like any other.

    Raises:
        HTTPException: 404 when the organization has no such connection.
    """
    store = _store(op_store)
    if not await store.detach_organization_connection(organization_id, provider_id, actor=actor):
        raise HTTPException(404, "This organization has no such SSO connection.")
    return await _organization(store, organization_id)


@router.put(
    "/organizations/{organization_id}/members/{user_id}/role",
    response_model=OrganizationMessageResponse,
)
async def set_member_role(
    organization_id: str,
    user_id: str,
    payload: SetMemberRoleRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> OrganizationMessageResponse:
    """Give a member the role of organization administrator, or take it back.

    The account must be a member already: the first administrator signs in
    through an SSO connection of the organization once before this.

    Raises:
        HTTPException: 404 when the account is not a member of the organization.
    """
    outcome = await _store(op_store).set_organization_member_role(
        organization_id, user_id, payload.role, actor=actor
    )
    if outcome == org_store.NOT_FOUND:
        raise HTTPException(404, "This account is not a member of the organization.")
    if payload.role == "admin":
        return OrganizationMessageResponse(message="The member is an organization administrator.")
    return OrganizationMessageResponse(message="The member is no longer an administrator.")
