"""Public read endpoint for the daily usage snapshot.

``GET /public-stats`` returns the newest document written by
:mod:`serving.analytics.public_stats` (tokens served, countries, languages and
agent clients). It holds aggregates only and is intentionally unauthenticated,
but a deployment must opt in with ``features.public_stats: true`` in its
distribution manifest; otherwise the endpoint answers 404 as if it did not exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from serving.analytics.public_stats import LATEST_SNAPSHOT_SQL
from serving.config.distribution import DistributionConfigError, get_active_distribution_config
from serving.servers.deps import get_db_logger

logger = logging.getLogger(__name__)

router = APIRouter()

# The snapshot changes once a day; let browsers and the edge reuse it briefly.
_CACHE_CONTROL = "public, max-age=600"


def public_stats_enabled() -> bool:
    """Return True only when the active distribution opts in to public stats."""
    try:
        distribution = get_active_distribution_config()
    except DistributionConfigError:
        return False
    return distribution is not None and distribution.features.public_stats is True


@router.get("/public-stats")
async def get_public_stats(db=Depends(get_db_logger)) -> JSONResponse:
    """Return the newest public usage snapshot."""
    if not public_stats_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Usage stats are temporarily unavailable.")
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(LATEST_SNAPSHOT_SQL)
    except Exception:
        logger.warning("public stats read failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Usage stats are temporarily unavailable."
        ) from None
    if row is None:
        raise HTTPException(status_code=404, detail="No usage stats have been published yet.")
    payload: Any = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return JSONResponse(payload, headers={"Cache-Control": _CACHE_CONTROL})
