"""Independent HTTP-level review of usable alert-history pagination.

Storage is stubbed here; PostgreSQL behavior is covered separately. The test
checks the public HTTP cursor contract, including the default query window.
"""

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.routers.admin import alert_history as routes
from serving.servers.routers.alert_history_auth import ReaderIdentity, require_history_reader
from serving.storage.alert_history_store import Scope


@pytest.mark.parametrize("explicit_window", [False, True])
async def test_next_cursor_preserves_the_initial_query_window(monkeypatch, explicit_window):
    now = dt.datetime.now(dt.UTC)
    rows = [
        {
            "record_id": f"00000000-0000-0000-0000-{index:012d}",
            "source": "backend",
            "source_namespace": "review-test",
            "occurred_at": now - dt.timedelta(minutes=2),
            "recorded_at": now - dt.timedelta(minutes=1, seconds=-index),
        }
        for index in (1, 2)
    ]
    windows = []

    async def list_events(conn, **kwargs):
        windows.append((kwargs["since"], kwargs["until"]))
        return rows if kwargs["after"] is None else rows[1:]

    @asynccontextmanager
    async def acquire():
        yield object()

    runtime = SimpleNamespace(read_slots=asyncio.Semaphore(2), acquire=acquire)
    monkeypatch.setattr(routes, "_runtime_or_error", lambda: runtime)
    monkeypatch.setattr(routes.store, "list_events", list_events)
    monkeypatch.setattr(routes.store, "coverage", AsyncMock(return_value={}))
    monkeypatch.setattr(routes.store, "known_gaps", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_history_reader] = lambda: ReaderIdentity(
        "admin", "synthetic-reviewer", Scope()
    )
    params = {"limit": 1}
    if explicit_window:
        params.update(
            since=(now - dt.timedelta(hours=1)).isoformat(),
            until=now.isoformat(),
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/admin/alert-history/events", params=params)
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert cursor
        second = await client.get(
            "/admin/alert-history/events", params={**params, "cursor": cursor}
        )
    assert second.status_code == 200, (
        "The returned next_cursor must continue the initial window even when "
        f"since/until were defaulted: {second.text}"
    )
    assert first.json()["events"][0]["record_id"] != second.json()["events"][0]["record_id"]
    assert windows[0] == windows[1]
