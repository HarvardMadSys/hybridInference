"""Admin endpoints for organization ceilings."""

from __future__ import annotations

from decimal import Decimal  # noqa: TC003 — Pydantic resolves this annotation at runtime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from serving.servers.auth import log_admin_action
from serving.servers.deps import get_admin_audit_actor, get_operational_store, verify_admin_access

if TYPE_CHECKING:
    from serving.audit import AuditActor

router = APIRouter(prefix="/admin")


class OrgUpdateRequest(BaseModel):
    """Change an organization's ceilings or status."""

    budget_daily_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    budget_monthly_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    max_concurrent_requests: int | None = Field(default=None, ge=1)
    status: Literal["active", "suspended"] | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str:
        """An omitted status is unchanged; an explicit null is invalid."""
        if value is None:
            raise ValueError("Organization status cannot be null.")
        return value


@router.patch("/orgs/{org_id}")
async def update_org(
    org_id: str,
    payload: OrgUpdateRequest,
    _admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Update an organization's budgets or suspension state."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "No fields to update")
    row = await op_store.update_org(org_id, changes)
    if not row:
        raise HTTPException(404, f"Organization '{org_id}' not found")
    await log_admin_action(
        op_store,
        action="org.update",
        details={"actor": "admin", "org_id": org_id, "changes": list(changes)},
        actor=actor,
    )
    return row
