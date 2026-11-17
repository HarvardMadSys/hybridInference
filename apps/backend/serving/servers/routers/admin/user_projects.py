"""Admin read of one account's projects and the keys in them.

Read-only. An account's owner manages its projects; an administrator sees them
to explain a refusal (``scope=project``) or a spend, and changes the account's
own quota through ``PATCH /admin/users/{user_id}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

# Pydantic resolves these annotations at runtime.
from serving.schemas_auth import APIKeyListItem, ProjectResponse  # noqa: TC001
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.servers.routers.projects import project_response
from serving.servers.routers.user_routes import key_list_item

router = APIRouter(prefix="/admin")


class UserProjectsResponse(BaseModel):
    """An account's active projects, and every key it holds, revoked ones included."""

    projects: list[ProjectResponse]
    keys: list[APIKeyListItem]


@router.get("/users/{user_id}/projects", response_model=UserProjectsResponse)
async def get_user_projects(
    user_id: str,
    _admin_id: str = Depends(verify_admin_access),
    op_store: Any = Depends(get_operational_store),
) -> UserProjectsResponse:
    """Return the account's projects with their spend, and its keys, all masked."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if not await op_store.get_user_by_id(user_id):
        raise HTTPException(404, f"User '{user_id}' not found")
    projects = [
        await project_response(op_store, row) for row in await op_store.list_projects(user_id)
    ]
    keys = [
        key_list_item(row, revealable=False) for row in await op_store.list_account_keys(user_id)
    ]
    return UserProjectsResponse(projects=projects, keys=keys)
