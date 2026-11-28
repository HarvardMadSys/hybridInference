"""OpenInfra's HTTP surface: the reporters' ingest and the public page's two reads.

- ``POST /internal/openinfra/ingest``: one host's snapshot from its reporter
  (:mod:`serving.openinfra.wire`), with ``Authorization: Bearer <token>`` (or
  ``x-ingest-token``) carrying ``OPENINFRA_INGEST_TOKEN``. 404 while the token
  is unset: a deployment that does not collect has no ingest endpoint. The
  answer is 200 once the snapshot is committed; a reporter that gets anything
  else sends its next snapshot as usual.
- ``GET /public-openinfra``: the dashboard document
  (:func:`serving.openinfra.dashboard.build_dashboard`).
- ``GET /public-openinfra/history?hours=2&node=``: the time series of the last
  ``hours``, rounded up to one of :data:`HISTORY_WINDOWS`, for every host or
  one that has reported (any other ``node`` answers 404).

The reads are unauthenticated and serve only what the page shows, but answer
404 unless the distribution publishes OpenInfra (``features.openinfra``).
Each document is computed at most once per :data:`DASHBOARD_TTL_SEC`, or for a
history window the spacing of its points up to :data:`HISTORY_TTL_SEC`
(:func:`history_ttl`), per distinct request, whoever asks and however often,
and callers arriving meanwhile share the read; browsers and the edge may reuse
it as long. The distinct requests are few by construction: a fixed set of
windows times the hosts that exist, so no caller can make every request miss.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from serving.openinfra import config, dashboard
from serving.openinfra.store import SnapshotWrite, history_grain
from serving.openinfra.wire import IngestError, parse_snapshot
from serving.servers.deps import get_db_logger
from serving.servers.routers.internal_auth import error
from serving.utils.logging import get_logger
from serving.utils.single_flight import SingleFlightCache

logger = get_logger(__name__)

router = APIRouter()

#: Largest snapshot accepted. A flush is a few tens of kilobytes; raw
#: Prometheus text from a large engine is the bulk of anything bigger.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: Budget for receiving a snapshot.
BODY_READ_TIMEOUT_SEC = 10.0
#: How long the dashboard document is reused. Reporters flush every second.
DASHBOARD_TTL_SEC = 5.0
#: The longest a history document is reused; one whose points are closer
#: together is reused for the spacing of its points.
HISTORY_TTL_SEC = 30.0
#: The longest ``node`` a history request may name.
MAX_NODE_ID_LENGTH = 200
#: The windows history is served at, in hours. A request for any other gets
#: the next longer one, so the page's 2 h, 24 h and 7 d are served as asked.
HISTORY_WINDOWS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 3.0, 6.0, 12.0, 24.0, 48.0, 72.0, 168.0)


def history_ttl(window: float) -> float:
    """How long the history of a *window* (hours) is reused: its points' spacing, up to 30 s."""
    return min(HISTORY_TTL_SEC, history_grain(window) / 1000)


_DASHBOARDS: SingleFlightCache[bytes] = SingleFlightCache(DASHBOARD_TTL_SEC, max_entries=4)
#: One cache per window, as each is reused for its own time.
_HISTORIES: dict[float, SingleFlightCache[bytes]] = {
    window: SingleFlightCache(history_ttl(window), max_entries=64) for window in HISTORY_WINDOWS
}
#: The ids of the hosts that have reported, for checking a history ``node``.
_KNOWN_NODES: SingleFlightCache[frozenset[str]] = SingleFlightCache(HISTORY_TTL_SEC, max_entries=4)

_UNAVAILABLE = "OpenInfra is temporarily unavailable."


def clear_caches() -> None:
    """Forget every document computed so far."""
    _DASHBOARDS.clear()
    for cache in _HISTORIES.values():
        cache.clear()
    _KNOWN_NODES.clear()


def _published_pool(db: Any) -> Any:
    """The database pool behind a public read, or the refusal.

    Raises:
        HTTPException: 404 unless the distribution publishes OpenInfra, 503
            without a database.
    """
    if not config.published():
        raise HTTPException(status_code=404, detail="Not Found")
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return db.pool


def _render(document: dict[str, Any]) -> bytes:
    return JSONResponse(document).body


def _json_response(body: bytes, ttl: float) -> Response:
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": f"public, max-age={int(ttl)}"},
    )


async def _read(conn: Any, build: Any) -> bytes:
    """Build a document in one read-only snapshot of the tables, rendered once."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        return _render(await build(conn))


@router.get("/public-openinfra")
async def get_dashboard(db=Depends(get_db_logger)) -> Response:
    """The current state of the fleet, its served totals and recent requests."""
    pool = _published_pool(db)
    retired, order = config.retired_engines(), config.engine_order()

    async def compute() -> bytes:
        async with pool.acquire() as conn:
            return await _read(
                conn, lambda c: dashboard.build_dashboard(c, retired=retired, order=order)
            )

    try:
        body = await _DASHBOARDS.get((pool, retired, order), compute)
    except Exception:
        logger.warning("openinfra dashboard read failed", exc_info=True)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
    return _json_response(body, DASHBOARD_TTL_SEC)


def history_hours(raw: str | None) -> float:
    """The window a ``hours`` parameter is served: the shortest of :data:`HISTORY_WINDOWS` it fits.

    Anything unreadable is the default two hours, as the original Worker had it.
    """
    try:
        hours = float(raw) if raw is not None else dashboard.DEFAULT_HISTORY_HOURS
    except ValueError:
        hours = dashboard.DEFAULT_HISTORY_HOURS
    if not math.isfinite(hours):
        hours = dashboard.DEFAULT_HISTORY_HOURS
    hours = dashboard.clamp_hours(hours)
    return next(window for window in HISTORY_WINDOWS if window >= hours)


async def _load_known_nodes(pool: Any) -> frozenset[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM openinfra_nodes")
    return frozenset(row["id"] for row in rows)


@router.get("/public-openinfra/history")
async def get_history(
    hours: str | None = Query(None),
    node: str = Query(""),
    db=Depends(get_db_logger),
) -> Response:
    """Time series for every host, GPU and engine (or one host's) over the last *hours*."""
    pool = _published_pool(db)
    if len(node) > MAX_NODE_ID_LENGTH:
        raise HTTPException(status_code=400, detail="node is too long")
    if node:
        try:
            known = await _KNOWN_NODES.get(pool, lambda: _load_known_nodes(pool))
        except Exception:
            logger.warning("openinfra host list read failed", exc_info=True)
            raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
        if node not in known:
            raise HTTPException(status_code=404, detail="No such node has reported.")
    window = history_hours(hours)
    retired = config.retired_engines()

    async def compute() -> bytes:
        async with pool.acquire() as conn:
            return await _read(
                conn,
                lambda c: dashboard.build_history(c, window, node=node, retired=retired),
            )

    try:
        body = await _HISTORIES[window].get((pool, node, retired), compute)
    except Exception:
        logger.warning("openinfra history read failed", exc_info=True)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
    return _json_response(body, history_ttl(window))


def require_ingest_token(
    authorization: str | None = Header(None),
    x_ingest_token: str | None = Header(None),
) -> None:
    """Admit a reporter by the shared ingest token, compared in constant time.

    Raises:
        HTTPException: 404 while no token is configured, 401 when it is absent
            or wrong.
    """
    expected = config.ingest_token()
    if not expected:
        raise error(
            status.HTTP_404_NOT_FOUND,
            "openinfra_ingest_not_configured",
            "This deployment does not collect OpenInfra metrics.",
        )
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:]
    else:
        presented = x_ingest_token or ""
    # As bytes: compare_digest refuses two str with non-ASCII characters.
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Invalid ingest token.")


async def _bounded_body(request: Request) -> bytes | None:
    """The raw body, or ``None`` once it passes :data:`MAX_BODY_BYTES` (never buffered past it)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/internal/openinfra/ingest", dependencies=[Depends(require_ingest_token)])
async def ingest(request: Request, db=Depends(get_db_logger)) -> dict[str, Any]:
    """Store one host's snapshot; the counts of GPU and engine rows it wrote."""
    if not db or not db.pool:
        raise error(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", _UNAVAILABLE)
    try:
        body = await asyncio.wait_for(_bounded_body(request), timeout=BODY_READ_TIMEOUT_SEC)
    except TimeoutError:
        raise error(
            status.HTTP_408_REQUEST_TIMEOUT, "request_timeout", "The body was not received in time."
        ) from None
    if body is None:
        raise error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "payload_too_large",
            f"Snapshots are limited to {MAX_BODY_BYTES} bytes.",
        )
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # Never logged with the input: a body is whatever the sender chose.
        raise error(
            status.HTTP_400_BAD_REQUEST, "invalid_request", "The body is not valid JSON."
        ) from None
    try:
        write = SnapshotWrite(parse_snapshot(payload), dashboard.now_ms())
    except IngestError as exc:
        raise error(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc)) from None
    try:
        async with db.pool.acquire() as conn:
            await write.apply(conn)
    except Exception:
        logger.warning("openinfra ingest failed", exc_info=True)
        raise error(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", _UNAVAILABLE) from None
    return {"ok": True, "gpus": len(write.gpus), "sglang": len(write.engines)}
