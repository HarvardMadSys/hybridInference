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

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from serving.analytics.public_stats import LATEST_SNAPSHOT_SQL
from serving.config.distribution import DistributionConfigError, get_active_distribution_config
from serving.servers.deps import get_db_logger
from serving.utils.single_flight import SingleFlightCache

logger = logging.getLogger(__name__)

router = APIRouter()

# The snapshot changes once a day; let browsers and the edge reuse it briefly.
_CACHE_CONTROL = "public, max-age=600"

#: How long the gateway itself answers from the snapshot it last read, in
#: seconds. Anyone may ask, as often as they like; callers arriving while a read
#: runs share it, so the database sees one read per window.
SNAPSHOT_TTL_SEC = 60.0

#: The last snapshot read, rendered, or None when none is published; keyed by
#: the pool it was read from.
_SNAPSHOTS: SingleFlightCache[bytes | None] = SingleFlightCache(SNAPSHOT_TTL_SEC, max_entries=1)


def public_stats_enabled() -> bool:
    """Return True only when the active distribution opts in to public stats."""
    try:
        distribution = get_active_distribution_config()
    except DistributionConfigError:
        return False
    return distribution is not None and distribution.features.public_stats is True


@router.get("/public-stats")
async def get_public_stats(db=Depends(get_db_logger)) -> Response:
    """Return the newest public usage snapshot, as read at most :data:`SNAPSHOT_TTL_SEC` ago."""
    if not public_stats_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Usage stats are temporarily unavailable.")
    pool = db.pool
    try:
        body = await _SNAPSHOTS.get(pool, lambda: _read_snapshot(pool))
    except Exception:
        logger.warning("public stats read failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Usage stats are temporarily unavailable."
        ) from None
    if body is None:
        raise HTTPException(status_code=404, detail="No usage stats have been published yet.")
    return Response(
        content=body, media_type="application/json", headers={"Cache-Control": _CACHE_CONTROL}
    )


async def _read_snapshot(pool: Any) -> bytes | None:
    """The newest snapshot, rendered as the response body once; None when none is published."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(LATEST_SNAPSHOT_SQL)
    if row is None:
        return None
    payload: Any = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return JSONResponse(payload).body
