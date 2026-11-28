"""``GET /admin/openinfra/export``: an administrator's download of the OpenInfra data.

The identities of every host, GPU and engine, the served totals, and the
history of the last ``days`` (1 to 30; hourly points from a week on), as one
JSON attachment. It answers whether or not the distribution publishes the
``/openinfra`` page, so an administrator can look at what is collected first.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from serving.openinfra import config, dashboard
from serving.servers.deps import get_db_logger, verify_admin_access
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/openinfra", tags=["Admin"])


@router.get("/export")
async def export_openinfra(
    days: float = Query(30, ge=1, le=dashboard.MAX_EXPORT_DAYS),
    _admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
) -> Response:
    """The OpenInfra data of the last *days*, as a JSON attachment."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="OpenInfra is temporarily unavailable.")
    try:
        async with (
            db.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            document = await dashboard.build_export(
                conn, days, retired=config.retired_engines(), order=config.engine_order()
            )
    except Exception:
        logger.warning("openinfra export failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="OpenInfra is temporarily unavailable."
        ) from None
    stamp = dt.datetime.fromtimestamp(document["exported_at"] / 1000, tz=dt.UTC).date()
    filename = f"openinfra-export-{document['days']}d-{stamp.isoformat()}.json"
    return Response(
        content=JSONResponse(document).body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
