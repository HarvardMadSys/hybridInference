"""Admin CRUD endpoints for homepage site updates (announcements / banner)."""

from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException

from serving.schemas_admin import (
    CreateSiteUpdateRequest,
    DeleteSiteUpdateResponse,
    ListSiteUpdatesResponse,
    SiteUpdateItem,
    UpdateSiteUpdateRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_db_logger, verify_admin_access

router = APIRouter(prefix="/admin")

# Columns selected for every admin response; keeps SELECTs consistent.
_COLUMNS = (
    "id, title, body, placement, published, link_url, link_label, "
    "created_by, created_at, updated_at"
)


def _row_to_item(row) -> SiteUpdateItem:
    """Convert a ``site_updates`` row to the API schema."""
    return SiteUpdateItem(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        placement=row["placement"],
        published=row["published"],
        link_url=row["link_url"],
        link_label=row["link_label"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/site-updates", response_model=ListSiteUpdatesResponse)
async def list_site_updates(
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
) -> ListSiteUpdatesResponse:
    """List all site updates (published and drafts), newest first.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {_COLUMNS} FROM site_updates ORDER BY created_at DESC")
    items = [_row_to_item(r) for r in rows]
    return ListSiteUpdatesResponse(total=len(items), updates=items)


@router.post("/site-updates", response_model=SiteUpdateItem, status_code=201)
async def create_site_update(
    req: CreateSiteUpdateRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
) -> SiteUpdateItem:
    """Create a new site update.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    update_id = str(_uuid.uuid4())
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO site_updates
                (id, title, body, placement, published, link_url, link_label, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING {_COLUMNS}
            """,
            update_id,
            req.title,
            req.body,
            req.placement,
            req.published,
            req.link_url,
            req.link_label,
            admin,
        )

    await log_admin_action(
        db, admin, "site_update_create", None, {"id": update_id, "title": req.title}
    )
    return _row_to_item(row)


@router.patch("/site-updates/{update_id}", response_model=SiteUpdateItem)
async def update_site_update(
    update_id: str,
    req: UpdateSiteUpdateRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
) -> SiteUpdateItem:
    """Update one or more fields of an existing site update.

    Only fields present in the request body are modified. Returns 404 when
    the update does not exist.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Build a parameterized SET clause from the supplied fields only. Column
    # names come from a fixed pydantic model, never user input, so they are
    # safe to interpolate.
    set_parts = [f"{col} = ${i}" for i, col in enumerate(fields, start=1)]
    set_parts.append("updated_at = NOW()")
    values = list(fields.values())
    values.append(update_id)

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE site_updates SET {', '.join(set_parts)} "
            f"WHERE id = ${len(values)} RETURNING {_COLUMNS}",
            *values,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Site update not found")

    await log_admin_action(
        db, admin, "site_update_update", None, {"id": update_id, "fields": list(fields)}
    )
    return _row_to_item(row)


@router.delete("/site-updates/{update_id}", response_model=DeleteSiteUpdateResponse)
async def delete_site_update(
    update_id: str,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
) -> DeleteSiteUpdateResponse:
    """Delete a site update. Returns 404 when it does not exist.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("DELETE FROM site_updates WHERE id = $1 RETURNING id", update_id)
    if not row:
        raise HTTPException(status_code=404, detail="Site update not found")

    await log_admin_action(db, admin, "site_update_delete", None, {"id": update_id})
    return DeleteSiteUpdateResponse(message="Site update deleted")
