"""``/admin/ttft-scatter`` served from the refreshing cache.

The scatter ranks every successful streaming request of the last 90 days to keep
the newest points of each series -- seconds on a production-sized ``api_logs``.
The endpoint now computes it once per process, says when (``generated_at``),
and recomputes on ``refresh=true``. The query itself is unchanged.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _reset_cache():
    metrics.TTFT_SCATTER.clear()
    yield
    metrics.TTFT_SCATTER.clear()


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
        refreshed = (await client.get("/admin/ttft-scatter?refresh=true")).json()

    assert first == second
    assert first["models"][0]["points"][0]["ttft_ms"] == 111
    assert datetime.fromisoformat(first["generated_at"].replace("Z", "+00:00"))
    assert refreshed["models"][0]["points"][0]["ttft_ms"] == 222
    assert refreshed["generated_at"] >= first["generated_at"]
    assert loader.await_count == 2


async def test_a_failed_refresh_still_serves_the_previous_scatter(monkeypatch):
    loader = AsyncMock(side_effect=[_scatter(111), RuntimeError("statement timeout")])
    monkeypatch.setattr(metrics, "_load_ttft_scatter", loader)

    async with _client() as client:
        first = await client.get("/admin/ttft-scatter")
        after_failure = await client.get("/admin/ttft-scatter?refresh=true")

    assert after_failure.status_code == 200
    # Its generated_at still tells the console how old it is.
    assert after_failure.json() == first.json()
