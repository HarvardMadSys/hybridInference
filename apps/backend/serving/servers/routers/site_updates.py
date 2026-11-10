"""Public read endpoint for homepage site updates.

Exposes the admin-managed announcements that drive the public homepage:

- ``GET /site-updates`` returns the single active banner (newest published
  ``placement='banner'`` row, or ``null``) plus the chronological feed of
  published ``placement='feed'`` entries.

This endpoint is intentionally unauthenticated — it only ever returns
published content and never drafts. Being anyone's to call, as often as they
like, it answers from what it last read for :data:`SITE_UPDATES_TTL_SEC`, and
callers arriving while a read runs share it: the database sees one read per
window, however many callers there are. An administrator's change shows within
that window.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from serving.schemas_admin import PublicSiteUpdate, PublicSiteUpdatesResponse
from serving.servers.deps import get_db_logger
from serving.utils.single_flight import SingleFlightCache

logger = logging.getLogger(__name__)

router = APIRouter()

_EMPTY = PublicSiteUpdatesResponse(banner=None, updates=[])

# Cap on the number of feed entries returned to the homepage.
_FEED_LIMIT = 20

_PUBLIC_COLUMNS = "id, title, body, link_url, link_label, created_at"

#: How long the public answer is kept, in seconds.
SITE_UPDATES_TTL_SEC = 10.0

#: The last answer, keyed by the pool it was read from.
_PUBLIC_UPDATES: SingleFlightCache[PublicSiteUpdatesResponse] = SingleFlightCache(
    SITE_UPDATES_TTL_SEC, max_entries=1
)


def _row_to_public(row) -> PublicSiteUpdate:
    """Convert a ``site_updates`` row to the public schema."""
    return PublicSiteUpdate(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        link_url=row["link_url"],
        link_label=row["link_label"],
        created_at=row["created_at"],
    )


@router.get("/site-updates", response_model=PublicSiteUpdatesResponse)
async def get_public_site_updates(
    db=Depends(get_db_logger),
) -> PublicSiteUpdatesResponse:
    """Return the active banner and published update feed for the homepage.

    Degrades gracefully when the database is unavailable by returning an
    empty payload rather than an error, so a transient DB outage never
    breaks the public landing page.
    """
    if not db or not db.pool:
        return _EMPTY

    # A live but unreachable DB (network partition, overload) raises on
    # acquire()/fetch — catch it so a transient outage degrades to an empty
    # payload rather than 500-ing the public landing page. A failure is not
    # kept: the next caller reads again.
    pool = db.pool
    try:
        return await _PUBLIC_UPDATES.get(pool, lambda: _read_public_updates(pool))
    except Exception:
        logger.warning("Failed to load public site updates; serving empty payload", exc_info=True)
        return _EMPTY


async def _read_public_updates(pool: Any) -> PublicSiteUpdatesResponse:
    """The active banner and the published feed, as stored now."""
    async with pool.acquire() as conn:
        banner_row = await conn.fetchrow(
            f"""
            SELECT {_PUBLIC_COLUMNS} FROM site_updates
            WHERE published = TRUE AND placement = 'banner'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        feed_rows = await conn.fetch(
            f"""
            SELECT {_PUBLIC_COLUMNS} FROM site_updates
            WHERE published = TRUE AND placement = 'feed'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            _FEED_LIMIT,
        )
    return PublicSiteUpdatesResponse(
        banner=_row_to_public(banner_row) if banner_row else None,
        updates=[_row_to_public(r) for r in feed_rows],
    )
