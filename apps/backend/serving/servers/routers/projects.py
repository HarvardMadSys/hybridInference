"""Console API for projects.

A project groups the account's keys under budgets below the account's quota
and an optional model allowlist. Every account has a Default project
that cannot be archived.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.accounting import get_accounting_policy
from serving.audit import AuditTarget
from serving.quota import native_limit
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

    from pydantic import BaseModel

logger = get_logger(__name__)
router = APIRouter(prefix="/user/projects", tags=["User Dashboard"])

_MAX_PROJECTS = 100

#: The unique index on an account's active project names. It is on
#: ``projects.org_id``, which holds one value per account (``legacy_org_id``).
_NAME_INDEX = "idx_projects_org_name"


def _is_duplicate_name(exc: Exception) -> bool:
    """Whether *exc* is a write refused for reusing an active project's name."""
    return _NAME_INDEX in str(exc)


async def project_response(op_store: Any, row: dict[str, Any]) -> ProjectResponse:
    """A project row as the console API shows it, with its spend and live key count."""
    today, month = await op_store.get_project_spend(row["id"])
    native: dict[str, Any] = {}
    if get_accounting_policy().native:
        # The native ledger budgets and spend, exact; a budget whose native
        # column predates it shows its USD value at the fixed policy rate.
        today_amount, month_amount = await op_store.get_project_spend_amount(row["id"])
        native = {
            "budget_daily": native_limit(row, "budget_daily_amount", "budget_daily_usd"),
            "budget_monthly": native_limit(row, "budget_monthly_amount", "budget_monthly_usd"),
            "spent_today": today_amount,
            "spent_month": month_amount,
        }
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
        **native,
    )


def _native_budgets(payload: BaseModel) -> dict[str, Decimal | None]:
    """The native budgets a request sent with the accounting ``currency``, to store exactly."""
    return {
        f"{name}_amount": getattr(payload, name)
        for name in ("budget_daily", "budget_monthly")
        if name in payload.model_fields_set
    }


async def _check_budgets(
    op_store: Any,
    user_id: str,
    *,
    daily_usd: Decimal | float | None,
    monthly_usd: Decimal | float | None,
    daily_amount: Decimal | None,
    monthly_amount: Decimal | None,
) -> None:
    """Refuse project budgets above the account's ceilings, in the ledger currency.

    A USD ledger compares the USD values, as before. Another compares the
    native budgets (the USD value at the fixed rate where none was sent) with
    the account's native ceilings, so a native budget is never judged through
    its rounded USD equivalent.
    """
    policy = get_accounting_policy()
    if not policy.native:
        account_daily, account_monthly = await op_store.get_account_budgets(user_id)
        _validate_budgets(account_daily, account_monthly, daily_usd, monthly_usd)
        return
    ceiling_daily, ceiling_monthly = await op_store.get_account_budget_amounts(user_id)
    for window, budget, usd, ceiling in (
        ("daily", daily_amount, daily_usd, ceiling_daily),
        ("monthly", monthly_amount, monthly_usd, ceiling_monthly),
    ):
        if budget is None and usd is not None:
            budget = policy.from_usd(usd)
        if budget is not None and ceiling is not None and budget > ceiling:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A project {window} budget cannot exceed the account's "
                    f"{ceiling:.2f} {policy.currency}."
                ),
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
    return [await project_response(op_store, row) for row in rows]


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
    """Create a project owned by the caller."""
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
    native = _native_budgets(payload)
    await _check_budgets(
        op_store,
        current_user["user_id"],
        daily_usd=payload.budget_daily_usd,
        monthly_usd=payload.budget_monthly_usd,
        daily_amount=native.get("budget_daily_amount"),
        monthly_amount=native.get("budget_monthly_amount"),
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
            **native,
        )
    except Exception as exc:
        if _is_duplicate_name(exc):
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
    return await project_response(op_store, row)


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
    return await project_response(op_store, row)


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
    native = _native_budgets(payload)
    changes.update(native)

    def kept(name: str) -> tuple[Any, Any]:
        """The budget after the change: (USD, native), the native None where not sent."""
        if f"{name}_usd" in changes:
            return changes[f"{name}_usd"], changes.get(f"{name}_amount")
        return existing.get(f"{name}_usd"), existing.get(f"{name}_amount")

    (daily_usd, daily_amount), (monthly_usd, monthly_amount) = (
        kept("budget_daily"),
        kept("budget_monthly"),
    )
    await _check_budgets(
        op_store,
        current_user["user_id"],
        daily_usd=daily_usd,
        monthly_usd=monthly_usd,
        daily_amount=daily_amount,
        monthly_amount=monthly_amount,
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
        if _is_duplicate_name(exc):
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
    return await project_response(op_store, row)


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
