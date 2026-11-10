"""Console API for projects.

A project groups the account's keys under budgets below the organization's
ceiling and an optional model allowlist. Every account has a Default project
that cannot be archived.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.audit import AuditTarget
from serving.schemas_auth import (
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_current_user,
    get_db_logger,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
    user_audit_actor,
)
from serving.servers.routers.user_routes import _canonical_allowlist
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from decimal import Decimal

logger = get_logger(__name__)
router = APIRouter(prefix="/user/projects", tags=["User Dashboard"])

_MAX_PROJECTS = 100


async def _project_response(op_store: Any, row: dict[str, Any]) -> ProjectResponse:
    today, month = await op_store.get_project_spend(row["id"])
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        is_default=row.get("is_default", False),
        budget_daily_usd=row.get("budget_daily_usd"),
        budget_monthly_usd=row.get("budget_monthly_usd"),
        allowed_models=row.get("allowed_models"),
        max_concurrent_requests=row.get("max_concurrent_requests"),
        spent_today_usd=today,
        spent_month_usd=month,
        key_count=await op_store.count_project_keys(row["id"]),
        created_at=row["created_at"],
    )


def _validate_budgets(
    account_daily: float,
    account_monthly: float | None,
    daily: Decimal | None,
    monthly: Decimal | None,
) -> None:
    if daily is not None and float(daily) > account_daily:
        raise HTTPException(
            status_code=422,
            detail=f"A project daily budget cannot exceed the account's {account_daily}.",
        )
    if monthly is not None and account_monthly is not None and float(monthly) > account_monthly:
        raise HTTPException(
            status_code=422,
            detail=f"A project monthly budget cannot exceed the account's {account_monthly}.",
        )


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> list[ProjectResponse]:
    """List the account's active projects with their current spend."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    rows = await op_store.list_projects(current_user["user_id"])
    return [await _project_response(op_store, row) for row in rows]


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    request: Request,
    payload: ProjectCreateRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    router_exec: Any = Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    db_logger=Depends(get_db_logger),
) -> ProjectResponse:
    """Create a project under the caller's personal organization."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name is required.")
    existing = await op_store.list_projects(current_user["user_id"])
    if len(existing) >= _MAX_PROJECTS:
        raise HTTPException(
            status_code=409,
            detail=f"An account may hold at most {_MAX_PROJECTS} active projects.",
        )
    if any(row["name"].casefold() == name.casefold() for row in existing):
        raise HTTPException(status_code=409, detail="A project with that name already exists.")
    account_daily, account_monthly = await op_store.get_account_budgets(current_user["user_id"])
    _validate_budgets(
        account_daily, account_monthly, payload.budget_daily_usd, payload.budget_monthly_usd
    )
    allowed_models = payload.allowed_models
    if allowed_models is not None:
        allowed_models = await _canonical_allowlist(
            allowed_models,
            current_user=current_user,
            router_exec=router_exec,
            embedding_adapters=embedding_adapters,
            model_visibility_resolver=model_visibility_resolver,
            op_store=op_store,
        )
    project_id = f"proj_{secrets.token_urlsafe(18)}"
    try:
        row = await op_store.create_project(
            project_id=project_id,
            user_id=current_user["user_id"],
            name=name,
            description=payload.description,
            budget_daily_usd=payload.budget_daily_usd,
            budget_monthly_usd=payload.budget_monthly_usd,
            allowed_models=allowed_models,
            max_concurrent_requests=payload.max_concurrent_requests,
        )
    except Exception as exc:
        if "idx_projects_org_name" in str(exc):
            raise HTTPException(
                status_code=409, detail="A project with that name already exists."
            ) from exc
        raise
    await log_admin_action(
        db_logger,
        action="create_project",
        details={"actor": "user", "project_id": project_id, "name": name},
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )
    return await _project_response(op_store, row)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> ProjectResponse:
    """Return one project owned by the caller."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    row = await op_store.get_project(current_user["user_id"], project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _project_response(op_store, row)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request,
    project_id: str,
    payload: ProjectUpdateRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
    router_exec: Any = Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
) -> ProjectResponse:
    """Change a project's name, budgets or allowlist."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await op_store.get_project(current_user["user_id"], project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        changes["name"] = (changes["name"] or "").strip()
        if not changes["name"]:
            raise HTTPException(status_code=422, detail="Project name is required.")
    account_daily, account_monthly = await op_store.get_account_budgets(current_user["user_id"])
    _validate_budgets(
        account_daily,
        account_monthly,
        changes.get("budget_daily_usd", existing.get("budget_daily_usd")),
        changes.get("budget_monthly_usd", existing.get("budget_monthly_usd")),
    )
    allowed_models = payload.allowed_models
    if allowed_models is not None:
        allowed_models = await _canonical_allowlist(
            allowed_models,
            current_user=current_user,
            router_exec=router_exec,
            embedding_adapters=embedding_adapters,
            model_visibility_resolver=model_visibility_resolver,
            op_store=op_store,
        )
    if "allowed_models" in changes:
        changes["allowed_models"] = allowed_models
    try:
        row = await op_store.update_project(current_user["user_id"], project_id, changes)
    except Exception as exc:
        if "idx_projects_org_name" in str(exc):
            raise HTTPException(
                status_code=409, detail="A project with that name already exists."
            ) from exc
        raise
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    await log_admin_action(
        db_logger,
        action="update_project",
        details={"project_id": project_id, "fields": sorted(changes)},
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )
    return await _project_response(op_store, row)


@router.post("/{project_id}/archive")
async def archive_project(
    request: Request,
    project_id: str,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    """Archive a project and revoke its keys."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await op_store.get_project(current_user["user_id"], project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    if existing.get("is_default"):
        raise HTTPException(status_code=409, detail="The Default project cannot be archived.")
    try:
        revoked = await op_store.archive_project(current_user["user_id"], project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="The project cannot be archived.") from exc
    await log_admin_action(
        db_logger,
        action="archive_project",
        details={"project_id": project_id, "keys_revoked": revoked},
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )
    return {
        "project_id": project_id,
        "archived_at": datetime.now(UTC).isoformat(),
        "keys_revoked": revoked,
    }
