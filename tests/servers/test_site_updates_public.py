"""The public ``/site-updates``: anyone may ask, as often as they like, for one read a window."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_db_logger
from serving.servers.routers import site_updates

pytestmark = pytest.mark.asyncio


def _row(update_id: str, title: str) -> dict:
    return {
        "id": update_id,
        "title": title,
        "body": "",
        "link_url": None,
        "link_label": None,
        "created_at": datetime(2026, 11, 8, tzinfo=UTC),
    }


def _db(*, fail: bool = False):
    conn = MagicMock()
    if fail:
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("down"))
    else:
        conn.fetchrow = AsyncMock(return_value=_row("b1", "Banner"))
    conn.fetch = AsyncMock(return_value=[_row("f1", "Feed one")])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.pool.acquire = MagicMock(return_value=acquire)
    return db, conn


async def _get_many(db, count: int):
    app = FastAPI()
    app.include_router(site_updates.router)
    app.dependency_overrides[get_db_logger] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return [await client.get("/site-updates") for _ in range(count)]


async def test_callers_within_the_window_share_one_read():
    db, conn = _db()

    responses = await _get_many(db, 5)

    assert {r.status_code for r in responses} == {200}
    body = responses[0].json()
    assert body["banner"]["title"] == "Banner"
    assert [u["title"] for u in body["updates"]] == ["Feed one"]
    assert all(r.json() == body for r in responses)
    assert (conn.fetchrow.await_count, conn.fetch.await_count) == (1, 1)


async def test_a_failed_read_degrades_to_empty_and_is_not_kept():
    db, conn = _db(fail=True)

    responses = await _get_many(db, 2)

    assert [r.json() for r in responses] == [{"banner": None, "updates": []}] * 2
    assert conn.fetchrow.await_count == 2


def test_the_window_is_seconds():
    assert 1 <= site_updates.SITE_UPDATES_TTL_SEC <= 60
