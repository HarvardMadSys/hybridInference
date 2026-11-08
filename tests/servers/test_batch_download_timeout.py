"""Batch downloads outlive the request timeout (M26).

``TimeoutMiddleware`` ends a response at ``REQUEST_TIMEOUT_SECONDS`` even after
its ``200`` went out, unless the response is a stream. A JSONL download of a
large batch is one: every download route marks itself so, and gets the stream
cap instead. Here the request timeout is a fraction of a second and the stores
serve a page every 0.1 s, so an unmarked download would end short.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.timeout import TimeoutMiddleware
from serving.servers.routers import batch_console, batch_console_create, batches, files

PAGES = 6
PAGE_DELAY = 0.1
REQUEST_TIMEOUT = 0.25
OWNER = "alice"


def _item(item_id: int) -> dict[str, Any]:
    return {
        "id": item_id,
        "custom_id": f"req-{item_id}",
        "status": "completed",
        "status_code": 200,
        "response": {"object": "chat.completion", "n": item_id},
        "error": None,
        "request_id": f"r{item_id}",
        "request": {"model": "m", "messages": []},
    }


class SlowBatchStore:
    """A sealed inline batch whose items come one slow page at a time."""

    def __init__(self) -> None:
        self.items = [_item(i) for i in range(1, PAGES + 1)]

    async def get_batch(self, batch_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        return {
            "id": batch_id,
            "user_id": OWNER,
            "status": "completed",
            "phase": "sealed",
            "schema_version": 2,
            "api_surface": "openai_inline",
            "target": "chat",
            "endpoint": "/v1/chat/completions",
            "request_count": PAGES,
            "created_at": datetime(2026, 11, 1, tzinfo=UTC),
        }

    async def take_read_lease(self, **_kwargs: Any) -> str:
        return "lease"

    async def renew_read_lease(self, lease_id: str, *, ttl_seconds: int) -> bool:
        return True

    async def drop_read_lease(self, lease_id: str) -> None:
        return None

    async def batch_intact(self, batch_id: str) -> bool:
        return True

    async def _page(self, after: int) -> tuple[list[dict[str, Any]], bool]:
        await asyncio.sleep(PAGE_DELAY)
        rest = [dict(item) for item in self.items if item["id"] > after]
        return rest[:1], len(rest) > 1

    async def result_page(self, batch_id: str, *, after: int = 0, **_kwargs: Any):
        return await self._page(after)

    async def request_page(self, batch_id: str, *, after: int = 0, **_kwargs: Any):
        return await self._page(after)


class SlowFileStore:
    """An upload whose chunks come one slow read at a time."""

    chunk = b'{"custom_id": "x"}\n'

    async def get_file(self, file_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": file_id,
            "user_id": OWNER,
            "kind": "upload",
            "status": "ready",
            "bytes": len(self.chunk) * PAGES,
            "filename": "in.jsonl",
            "sha256": None,
            "derived_from_file_id": None,
        }

    async def read_chunks(self, file_id: str, *, after_seq: int = -1, limit: int = 8):
        await asyncio.sleep(PAGE_DELAY)
        seq = after_seq + 1
        return [(seq, self.chunk)] if seq < PAGES else []


@pytest.fixture
def app(monkeypatch) -> TimeoutMiddleware:
    monkeypatch.setattr(batch_config_module, "config_value", lambda key, default=None: default)
    application = FastAPI()
    application.state.services = AppServices(
        router=None, batch_store=SlowBatchStore(), batch_file_store=SlowFileStore()
    )

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        return {"authenticated": True, "user_id": OWNER, "role": "internal"}

    application.dependency_overrides[verify_batch_management_identity] = identity
    application.dependency_overrides[get_current_user] = identity
    for module in (batches, files, batch_console, batch_console_create):
        application.include_router(module.router)
    return TimeoutMiddleware(application, timeout_s=REQUEST_TIMEOUT, stream_timeout_s=30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "lines"),
    [
        ("/v1/batches/batch_1/output.jsonl", PAGES),
        ("/v1/files/file-1/content", PAGES),
        ("/user/batches/batch_1/results", PAGES),
        ("/user/batches/batch_1/requests", PAGES),
        ("/user/batch-files/file-1/content", PAGES),
    ],
)
async def test_a_download_slower_than_the_request_timeout_is_served_whole(app, path, lines):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, headers={"Authorization": "Bearer k"})

    assert response.status_code == 200
    assert response.content.count(b"\n") == lines


@pytest.mark.asyncio
async def test_a_route_that_does_not_stream_still_times_out(app):
    """The mark is per response: anything else keeps the request timeout."""
    inner = app.app

    @inner.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(1)
        return {"ok": "late"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/slow")

    assert response.status_code == 504
