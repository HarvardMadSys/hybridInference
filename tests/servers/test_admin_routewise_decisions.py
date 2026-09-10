"""Tests for the ``GET /admin/routewise/decisions`` aggregation endpoint.

The SQL is aggregated server-side, so these tests mock the connection and
return the pre-aggregated rows each GROUP BY query would produce over a small
set of fake ``api_logs`` rows. They assert the handler transforms those rows
into the fixed API contract (counts, LP status mix, selection share, per-bucket
time series), that the correct SQL predicates / bind params are issued, and
that the range parameter and missing model_id are validated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin.routewise import (
    SERVED_ENDPOINT_SQL,
    SERVED_PROVIDER_TYPE_SQL,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _classify(query: str) -> str:
    """Map one of the handler's five queries to a stable key."""
    if "COUNT(*) AS total_requests" in query:
        return "counts"
    if "AS lp_status" in query:
        return "lp_status"
    # The hedge-bucket query also uses to_timestamp, so match it first.
    if "not_hedged" in query:
        return "hedge_buckets"
    if "to_timestamp" in query:
        return "buckets"
    if "final_provider_type" in query:
        return "selection"
    return "unknown"


def _make_db_logger(
    *,
    counts: dict[str, Any] | None = None,
    lp_status: list[dict[str, Any]] | None = None,
    selection: list[dict[str, Any]] | None = None,
    buckets: list[dict[str, Any]] | None = None,
    hedge_buckets: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, list[Any]]]:
    """Build a mock db_logger returning canned aggregation rows per query.

    Returns a ``(logger, calls)`` pair; ``calls`` records every ``(query, args)``
    keyed by ``"fetchrow"`` / ``"fetch"`` so tests can assert the emitted SQL.
    """
    calls: dict[str, list[Any]] = {"fetchrow": [], "fetch": []}

    async def _fetchrow(query: str, *args: Any) -> Any:
        calls["fetchrow"].append((query, args))
        if _classify(query) == "counts":
            return (
                counts
                if counts is not None
                else {
                    "total_requests": 0,
                    "unattributed_requests": 0,
                    "hedged": 0,
                    "backup_won": 0,
                    "median_hedge_delay_ms": None,
                }
            )
        return None

    async def _fetch(query: str, *args: Any) -> list[Any]:
        calls["fetch"].append((query, args))
        kind = _classify(query)
        if kind == "lp_status":
            return lp_status or []
        if kind == "selection":
            return selection or []
        if kind == "buckets":
            return buckets or []
        if kind == "hedge_buckets":
            return hedge_buckets or []
        return []

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(side_effect=_fetch)

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    logger = MagicMock()
    logger.pool = pool
    return logger, calls


def _build_client(logger: Any) -> AsyncClient:
    app = FastAPI(title="Admin RouteWise Decisions Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=logger)  # type: ignore[attr-defined]
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def populated_client() -> AsyncGenerator[tuple[AsyncClient, dict[str, list[Any]]], None]:
    """Client backed by a representative aggregation of six fake rows.

    Attribution is winner-aware: a hedge backup win counts toward the endpoint
    that actually served it (``backup_provider``), not the primary
    ``final_endpoint``. The canned selection/bucket rows below are what the
    served-endpoint ``CASE`` yields. Underlying (conceptual) rows for
    ``minimax-fast``:
      * 13:00 -> 2 rows served by ``...[wandb]-api`` (on_demand), lp optimal:
        one not hedged, one hedged with the primary leg winning
      * 13:00 -> 1 row hedged with the backup leg winning: primary was
        ``...[wandb]-api`` but ``backup_provider`` ``...[deepinfra]-api``
        (quota) served it, so it lands under deepinfra
      * 14:00 -> 2 rows served by ``...[highspeed]-api`` (concurrency), lp
        optimal, not hedged
      * 13:00 -> 1 row with no served endpoint (error path), lp cheapest_fallback
    """
    logger, calls = _make_db_logger(
        counts={
            "total_requests": 6,
            "unattributed_requests": 1,
            "hedged": 2,
            "backup_won": 1,
            "median_hedge_delay_ms": 975.0,
        },
        lp_status=[
            {"lp_status": "optimal", "cnt": 5},
            {"lp_status": "cheapest_fallback", "cnt": 1},
        ],
        # Ordered by cnt DESC, endpoint ASC (as the served-endpoint query does).
        # The backup-won row lands under its backup endpoint (deepinfra).
        selection=[
            {
                "endpoint": "minimax-fast:openrouter[minimax/highspeed]-api",
                "provider_type": "concurrency",
                "cnt": 2,
            },
            {
                "endpoint": "minimax-fast:openrouter[wandb]-api",
                "provider_type": "on_demand",
                "cnt": 2,
            },
            {
                "endpoint": "minimax-fast:openrouter[deepinfra]-api",
                "provider_type": "quota",
                "cnt": 1,
            },
        ],
        buckets=[
            {
                "bucket_start": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
                "endpoint": "minimax-fast:openrouter[wandb]-api",
                "cnt": 2,
            },
            {
                "bucket_start": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
                "endpoint": "minimax-fast:openrouter[deepinfra]-api",
                "cnt": 1,
            },
            {
                "bucket_start": datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
                "endpoint": "minimax-fast:openrouter[minimax/highspeed]-api",
                "cnt": 2,
            },
        ],
        hedge_buckets=[
            # 13:00 holds the 3 attributed rows plus the 1 unattributed row (4
            # total): two not hedged, one hedged primary won, one hedged backup
            # won. The hedge query is winner-agnostic, so it is unchanged.
            {
                "bucket_start": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
                "not_hedged": 2,
                "hedged_primary_won": 1,
                "hedged_backup_won": 1,
            },
            {
                "bucket_start": datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
                "not_hedged": 2,
                "hedged_primary_won": 0,
                "hedged_backup_won": 0,
            },
        ],
    )
    async with _build_client(logger) as client:
        yield client, calls


@pytest.mark.asyncio
async def test_decisions_aggregates_from_rows(populated_client):
    client, _calls = populated_client
    resp = await client.get("/admin/routewise/decisions?model_id=minimax-fast")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["model_id"] == "minimax-fast"
    assert body["range"] == "24h"
    assert body["bucket_seconds"] == 3600
    assert body["total_requests"] == 6
    assert body["unattributed_requests"] == 1
    assert body["lp_status_counts"] == {"optimal": 5, "cheapest_fallback": 1}
    # The backup-won row is attributed to its backup endpoint (deepinfra), not to
    # the primary (wandb); wandb therefore holds 2, deepinfra 1.
    assert body["selection_share"] == [
        {
            "endpoint": "minimax-fast:openrouter[minimax/highspeed]-api",
            "provider_type": "concurrency",
            "count": 2,
        },
        {
            "endpoint": "minimax-fast:openrouter[wandb]-api",
            "provider_type": "on_demand",
            "count": 2,
        },
        {
            "endpoint": "minimax-fast:openrouter[deepinfra]-api",
            "provider_type": "quota",
            "count": 1,
        },
    ]
    # hedge_rate is over all requests (2/6); backup_win_rate over hedged (1/2).
    assert body["hedge_summary"]["hedged"] == 2
    assert body["hedge_summary"]["backup_won"] == 1
    assert body["hedge_summary"]["hedge_rate"] == pytest.approx(2 / 6)
    assert body["hedge_summary"]["backup_win_rate"] == pytest.approx(0.5)
    assert body["hedge_summary"]["median_hedge_delay_ms"] == 975.0
    # The backup-won row lands under deepinfra in its 13:00 bucket, alongside the
    # two wandb-served rows.
    assert body["buckets"] == [
        {
            "bucket_start": "2026-07-01T13:00:00+00:00",
            "counts": {
                "minimax-fast:openrouter[wandb]-api": 2,
                "minimax-fast:openrouter[deepinfra]-api": 1,
            },
            "hedge": {"not_hedged": 2, "hedged_primary_won": 1, "hedged_backup_won": 1},
        },
        {
            "bucket_start": "2026-07-01T14:00:00+00:00",
            "counts": {"minimax-fast:openrouter[minimax/highspeed]-api": 2},
            "hedge": {"not_hedged": 2, "hedged_primary_won": 0, "hedged_backup_won": 0},
        },
    ]


@pytest.mark.asyncio
async def test_decisions_issues_expected_sql(populated_client):
    client, calls = populated_client
    resp = await client.get("/admin/routewise/decisions?model_id=minimax-fast")
    assert resp.status_code == 200, resp.text

    # Counts query: exact model match, routewise-key presence, 24h window, and
    # the hedge aggregates (hedge count + backup-won via hedge_winner, median).
    count_query, count_args = calls["fetchrow"][0]
    assert "model_id = $1" in count_query
    assert "metadata ? 'routewise'" in count_query
    assert "($2::int * interval '1 second')" in count_query
    assert "(metadata->'routewise'->>'hedged') = 'true'" in count_query
    assert "(metadata->'routewise'->>'hedge_winner') = 'backup'" in count_query
    assert "percentile_cont(0.5)" in count_query
    assert count_args == ("minimax-fast", 86_400)
    # unattributed = no SERVED endpoint (not merely no final_endpoint), so a
    # backup win with a backup_provider stays attributed. The CASE falls back to
    # final_endpoint via COALESCE when backup_provider is NULL.
    assert f"WHERE ({SERVED_ENDPOINT_SQL}) IS NULL" in count_query
    assert (
        "COALESCE(metadata->'routewise'->>'backup_provider', "
        "metadata->'routewise'->>'final_endpoint')"
    ) in SERVED_ENDPOINT_SQL

    # Selection-distribution bucket query must receive bucket_seconds as $3 and
    # attribute each row to the served endpoint (the backup on a backup win).
    bucket_calls = [
        (q, a) for q, a in calls["fetch"] if "to_timestamp" in q and "not_hedged" not in q
    ]
    assert bucket_calls, "expected the buckets query to run"
    bucket_query, bucket_args = bucket_calls[0]
    assert bucket_args == ("minimax-fast", 86_400, 3_600)
    assert f"{SERVED_ENDPOINT_SQL} AS endpoint" in bucket_query
    assert f"({SERVED_ENDPOINT_SQL}) IS NOT NULL" in bucket_query

    # Selection query groups the served endpoint and its served provider type.
    selection_calls = [(q, a) for q, a in calls["fetch"] if "final_provider_type" in q]
    assert selection_calls, "expected the selection query to run"
    selection_query, _selection_args = selection_calls[0]
    assert f"{SERVED_ENDPOINT_SQL} AS endpoint" in selection_query
    assert f"{SERVED_PROVIDER_TYPE_SQL} AS provider_type" in selection_query
    assert f"({SERVED_ENDPOINT_SQL}) IS NOT NULL" in selection_query

    # Hedge-bucket query buckets over ALL routewise rows (winner-agnostic): no
    # served-endpoint CASE and no final_endpoint gate.
    hedge_calls = [(q, a) for q, a in calls["fetch"] if "not_hedged" in q]
    assert hedge_calls, "expected the hedge-bucket query to run"
    hedge_query, hedge_args = hedge_calls[0]
    assert "final_endpoint" not in hedge_query
    assert SERVED_ENDPOINT_SQL not in hedge_query
    assert hedge_args == ("minimax-fast", 86_400, 3_600)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("range_value", "window_seconds", "bucket_seconds"),
    [("24h", 86_400, 3_600), ("7d", 604_800, 21_600), ("30d", 2_592_000, 86_400)],
)
async def test_decisions_range_maps_to_window_and_bucket(
    range_value, window_seconds, bucket_seconds
):
    logger, calls = _make_db_logger()
    async with _build_client(logger) as client:
        resp = await client.get(f"/admin/routewise/decisions?model_id=m&range={range_value}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["range"] == range_value
    assert body["bucket_seconds"] == bucket_seconds

    _count_query, count_args = calls["fetchrow"][0]
    assert count_args == ("m", window_seconds)
    bucket_calls = [(q, a) for q, a in calls["fetch"] if "to_timestamp" in q]
    _bucket_query, bucket_args = bucket_calls[0]
    assert bucket_args == ("m", window_seconds, bucket_seconds)


@pytest.mark.asyncio
async def test_decisions_empty_window_returns_zeros_not_error():
    logger, _calls = _make_db_logger()  # defaults: zero counts, empty lists
    async with _build_client(logger) as client:
        resp = await client.get("/admin/routewise/decisions?model_id=quiet-model")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_requests"] == 0
    assert body["unattributed_requests"] == 0
    assert body["lp_status_counts"] == {}
    assert body["selection_share"] == []
    assert body["buckets"] == []
    # No requests and no hedges -> rates are 0.0 and the median is null.
    assert body["hedge_summary"] == {
        "hedged": 0,
        "hedge_rate": 0.0,
        "backup_won": 0,
        "backup_win_rate": 0.0,
        "median_hedge_delay_ms": None,
    }


@pytest.mark.asyncio
async def test_decisions_zero_hedge_window_keeps_rate_zero_and_median_null():
    """A window with traffic but no hedges: hedge_rate 0.0, median null."""
    logger, _calls = _make_db_logger(
        counts={
            "total_requests": 12,
            "unattributed_requests": 0,
            "hedged": 0,
            "backup_won": 0,
            "median_hedge_delay_ms": None,
        },
        hedge_buckets=[
            {
                "bucket_start": datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc),
                "not_hedged": 12,
                "hedged_primary_won": 0,
                "hedged_backup_won": 0,
            },
        ],
    )
    async with _build_client(logger) as client:
        resp = await client.get("/admin/routewise/decisions?model_id=calm-model")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hedge_summary"]["hedged"] == 0
    assert body["hedge_summary"]["hedge_rate"] == 0.0
    assert body["hedge_summary"]["backup_win_rate"] == 0.0
    assert body["hedge_summary"]["median_hedge_delay_ms"] is None
    # The bucket still surfaces (>= 1 routewise row) with an empty counts map.
    assert body["buckets"] == [
        {
            "bucket_start": "2026-07-01T15:00:00+00:00",
            "counts": {},
            "hedge": {"not_hedged": 12, "hedged_primary_won": 0, "hedged_backup_won": 0},
        },
    ]


@pytest.mark.asyncio
async def test_decisions_bucket_with_only_unattributed_rows_still_appears():
    """A bucket holding only error-path (unattributed) rows appears with empty counts."""
    logger, _calls = _make_db_logger(
        counts={
            "total_requests": 3,
            "unattributed_requests": 3,
            "hedged": 1,
            "backup_won": 1,
            "median_hedge_delay_ms": 420.0,
        },
        selection=[],  # every row was unattributed, so no selection share
        buckets=[],  # ... and no attributed per-endpoint counts
        hedge_buckets=[
            {
                "bucket_start": datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
                "not_hedged": 2,
                "hedged_primary_won": 0,
                "hedged_backup_won": 1,
            },
        ],
    )
    async with _build_client(logger) as client:
        resp = await client.get("/admin/routewise/decisions?model_id=erroring-model")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["selection_share"] == []
    assert body["buckets"] == [
        {
            "bucket_start": "2026-07-01T16:00:00+00:00",
            "counts": {},
            "hedge": {"not_hedged": 2, "hedged_primary_won": 0, "hedged_backup_won": 1},
        },
    ]
    assert body["hedge_summary"]["backup_win_rate"] == pytest.approx(1.0)
    assert body["hedge_summary"]["median_hedge_delay_ms"] == 420.0


@pytest.mark.asyncio
async def test_decisions_invalid_range_is_422():
    logger, _calls = _make_db_logger()
    async with _build_client(logger) as client:
        resp = await client.get("/admin/routewise/decisions?model_id=m&range=90d")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decisions_missing_model_id_is_422():
    logger, _calls = _make_db_logger()
    async with _build_client(logger) as client:
        resp = await client.get("/admin/routewise/decisions")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_decisions_requires_admin_auth():
    """Without an admin token the endpoint must reject the caller."""
    logger, _calls = _make_db_logger()
    app = FastAPI(title="Admin RouteWise Decisions Auth Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=logger)  # type: ignore[attr-defined]
    # No verify_admin_access override: the real dependency runs.
    app.include_router(admin.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/routewise/decisions?model_id=m")
    assert resp.status_code in (401, 403), resp.text
