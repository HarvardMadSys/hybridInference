"""``/admin/ttft-scatter`` served from the refreshing cache.

The scatter ranks every successful streaming request of the last 90 days to keep
the newest points of each series -- seconds on a production-sized ``api_logs``.
The endpoint serves the kept result, says when it was computed
(``generated_at``), and on ``refresh=true`` starts a new computation and answers
at once with ``refreshing`` set. The query itself is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.schemas_admin import (
    AdminTtftScatterModel,
    AdminTtftScatterPoint,
    AdminTtftScatterResponse,
)
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics

pytestmark = pytest.mark.asyncio


class _MemoryStore:
    """Stands in for ``admin_view_results``: these tests have no database."""

    def __init__(self) -> None:
        self.rows: dict = {}

    async def generation(self, key):
        return 0

    async def read(self, key):
        return self.rows.get(key)

    async def write(self, key, value, computed_at, generation):
        self.rows[key] = (value, computed_at, generation)
        return True


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    metrics.TTFT_SCATTER.clear()
    monkeypatch.setattr(metrics.TTFT_SCATTER, "store", _MemoryStore())
    yield
    metrics.TTFT_SCATTER.clear()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _scatter(ttft_ms: int) -> AdminTtftScatterResponse:
    point = AdminTtftScatterPoint(
        prompt_tokens=100, ttft_ms=ttft_ms, cache_hit=False, timestamp=datetime.now(timezone.utc)
    )
    return AdminTtftScatterResponse(
        generated_at=datetime.now(timezone.utc),
        models=[AdminTtftScatterModel(model_id="m", provider="p", points=[point])],
    )


def _client() -> AsyncClient:
    app = FastAPI(title="Admin TTFT scatter cache test")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=RouteExecutor(), db_logger=MagicMock(pool=object())
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_scatter_is_computed_once_and_says_when(monkeypatch):
    loader = AsyncMock(side_effect=[_scatter(111), _scatter(222)])
    monkeypatch.setattr(metrics, "_load_ttft_scatter", loader)

    async with _client() as client:
        first = (await client.get("/admin/ttft-scatter")).json()
        second = (await client.get("/admin/ttft-scatter")).json()
        refreshing = (await client.get("/admin/ttft-scatter?refresh=true")).json()
        await _settle()
        refreshed = (await client.get("/admin/ttft-scatter")).json()

    assert first == second
    assert first["refreshing"] is False
    assert first["models"][0]["points"][0]["ttft_ms"] == 111
    assert datetime.fromisoformat(first["generated_at"].replace("Z", "+00:00"))
    # The refresh answers at once with the kept scatter, flagged.
    assert refreshing == {**first, "refreshing": True}
    assert refreshed["models"][0]["points"][0]["ttft_ms"] == 222
    assert refreshed["generated_at"] >= first["generated_at"]
    assert loader.await_count == 2


async def test_a_failed_refresh_still_serves_the_previous_scatter(monkeypatch):
    loader = AsyncMock(side_effect=[_scatter(111), RuntimeError("statement timeout")])
    monkeypatch.setattr(metrics, "_load_ttft_scatter", loader)

    async with _client() as client:
        first = await client.get("/admin/ttft-scatter")
        await client.get("/admin/ttft-scatter?refresh=true")
        await _settle()
        after_failure = await client.get("/admin/ttft-scatter")

    assert after_failure.status_code == 200
    # Its generated_at still tells the console how old it is, and it says why
    # the refresh failed.
    assert after_failure.json() == {**first.json(), "refresh_error": "statement timeout"}
