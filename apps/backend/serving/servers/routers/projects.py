"""Dashboard project management.

A project groups the account's keys under budgets below the account ceiling
and an optional model allowlist. The account ceiling always applies, so a
project budget is only ever a tighter limit.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving import quota
from serving.schemas_auth import (
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/user/projects", tags=["User Dashboard"])
logger = get_logger(__name__)


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


async def _project_response(op_store: Any, row: dict[str, Any]) -> ProjectResponse:
    allowed = row.get("allowed_models") or []
    if isinstance(allowed, str):
        import json

        try:
            allowed = json.loads(allowed)
        except (json.JSONDecodeError, TypeError):
            allowed = []
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        daily_cost_quota_usd=row.get("daily_cost_quota_usd"),
        monthly_cost_quota_usd=row.get("monthly_cost_quota_usd"),
        allowed_models=list(allowed),
        spent_today_usd=_as_decimal(await op_store.get_project_cost(row["id"], "today")),
        spent_month_usd=_as_decimal(await op_store.get_project_cost(row["id"], "month")),
        key_count=await op_store.count_active_keys_for_project(row["id"]),
        created_at=row["created_at"],
    )


def _require_ceilings(
    account: dict[str, Any],
    daily: Decimal | None,
    monthly: Decimal | None,
) -> None:
    account_daily = Decimal(str(quota.resolve_quota(account.get("daily_cost_quota_usd"))))
    if daily is not None and daily > account_daily:
        raise HTTPException(
            status_code=422,
            detail=f"Project daily budget cannot exceed the account's {account_daily}.",
        )
    if monthly is not None:
        account_monthly = account.get("monthly_cost_quota_usd")
        if account_monthly is not None and monthly > Decimal(str(account_monthly)):
            raise HTTPException(
                status_code=422,
                detail=f"Project monthly budget cannot exceed the account's {account_monthly}.",
            )


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> list[ProjectResponse]:
    """List the account's active projects with today's and this month's spend."""
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
    db_logger=Depends(get_db_logger),
) -> ProjectResponse:
    """Create a project under the current account."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name is required.")
    existing = await op_store.list_projects(current_user["user_id"])
    if any(row["name"].lower() == name.lower() for row in existing):
        raise HTTPException(status_code=409, detail="A project with that name already exists.")
    _require_ceilings(
        current_user,
        payload.daily_cost_quota_usd,
        payload.monthly_cost_quota_usd,
    )
    project_id = f"prj_{secrets.token_urlsafe(18)}"
    try:
        row = await op_store.create_project(
            project_id=project_id,
            account_id=current_user["user_id"],
            name=name,
            daily_cost_quota_usd=payload.daily_cost_quota_usd,
            monthly_cost_quota_usd=payload.monthly_cost_quota_usd,
            allowed_models=payload.allowed_models,
        )
    except Exception as exc:
        if "idx_projects_account_name_active" in str(exc):
            raise HTTPException(
                status_code=409, detail="A project with that name already exists."
            ) from exc
        raise
    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "create_project",
        current_user["user_id"],
        {"actor": "user", "project_id": project_id, "name": name},
    )
    return await _project_response(op_store, row)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> ProjectResponse:
    """Patch a project's name, budgets or model allowlist."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await op_store.get_project(current_user["user_id"], project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(status_code=422, detail="Project name is required.")
    _require_ceilings(
        current_user,
        fields.get("daily_cost_quota_usd", existing.get("daily_cost_quota_usd")),
        fields.get("monthly_cost_quota_usd", existing.get("monthly_cost_quota_usd")),
    )
    row = await op_store.update_project(current_user["user_id"], project_id, **fields)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _project_response(op_store, row)


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Delete a project and revoke its keys."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    existing = await op_store.get_project(current_user["user_id"], project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    revoked = await op_store.delete_project(current_user["user_id"], project_id)
    return {
        "project_id": project_id,
        "deleted_at": datetime.now(UTC).isoformat(),
        "keys_revoked": revoked,
    }
