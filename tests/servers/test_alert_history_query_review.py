"""Independent HTTP-level review of usable alert-history pagination.

Storage is stubbed here; PostgreSQL behavior is covered separately. The test
checks the public HTTP cursor contract, including the default query window.
"""

import asyncio
import base64
import datetime as dt
import json
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


def _forge(cursor: str, window: list[str]) -> str:
    """The same cursor with its window replaced and its query fingerprint kept."""
    padded = cursor + "=" * (-len(cursor) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    data["w"] = window
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _app(monkeypatch, windows):
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
        "reader", "synthetic-reader", Scope(deployment_ids=("dep",), target_environments=("prod",))
    )
    return app, now


@pytest.mark.parametrize("explicit_window", [False, True])
async def test_a_forged_cursor_window_is_held_to_the_window_rules(monkeypatch, explicit_window):
    windows: list = []
    app, now = _app(monkeypatch, windows)
    params: dict = {"limit": 1}
    if explicit_window:
        params.update(
            since=(now - dt.timedelta(hours=1)).isoformat(),
            until=now.isoformat(),
        )
    iso = dt.datetime.isoformat
    forgeries = {
        # 298 days: what an unsigned cursor used to unlock without any secret.
        "too_wide": [iso(now - dt.timedelta(days=298)), iso(now)],
        "reversed": [iso(now), iso(now - dt.timedelta(hours=1))],
        "empty": [iso(now), iso(now)],
        "naive": [iso(now.replace(tzinfo=None) - dt.timedelta(hours=1)), iso(now)],
    }
    if explicit_window:
        # Within 7 days, but not the window the query named.
        forgeries["moved"] = [
            iso(now - dt.timedelta(days=3)),
            iso(now - dt.timedelta(days=2)),
        ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/admin/alert-history/events", params=params)
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert cursor
        for name, window in forgeries.items():
            forged = await client.get(
                "/admin/alert-history/events",
                params={**params, "cursor": _forge(cursor, window)},
            )
            assert forged.status_code == 422, (name, forged.text)
        genuine = await client.get(
            "/admin/alert-history/events", params={**params, "cursor": cursor}
        )
    assert genuine.status_code == 200
    assert genuine.json()["events"][0]["record_id"] != first.json()["events"][0]["record_id"]
    # Only the first page and the genuine continuation reached storage, both
    # over the window the first page resolved.
    assert len(windows) == 2 and windows[0] == windows[1]
    assert windows[0][1] - windows[0][0] <= routes.MAX_WINDOW


async def test_a_defaulted_window_cursor_may_carry_any_valid_window(monkeypatch):
    """No explicit bound: a valid moved window is no more than the caller could ask for."""
    windows: list = []
    app, now = _app(monkeypatch, windows)
    moved = [
        (now - dt.timedelta(days=7)).isoformat(),
        now.isoformat(),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/admin/alert-history/events", params={"limit": 1})
        cursor = first.json()["next_cursor"]
        moved_page = await client.get(
            "/admin/alert-history/events", params={"limit": 1, "cursor": _forge(cursor, moved)}
        )
    assert moved_page.status_code == 200
    assert windows[1] == (now - dt.timedelta(days=7), now)
