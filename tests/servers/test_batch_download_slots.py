"""Rendered batch downloads are refused, not queued, when the process has no slot free (M27)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module, delivery
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import batch_console, batches, files
from tests.servers.test_batch_download_timeout import OWNER, SlowBatchStore, SlowFileStore

RENDERED = [
    "/v1/batches/batch_1/output.jsonl",
    "/user/batches/batch_1/results",
    "/user/batches/batch_1/requests",
]


class _OutputFiles(SlowFileStore):
    """A result file of a sealed batch, rendered from its rows."""

    async def get_file(self, file_id: str, **_kwargs: Any) -> dict[str, Any]:
        row = await super().get_file(file_id)
        if file_id == "file-output":
            return {**row, "kind": "output", "batch_id": "batch_1", "bytes": 0, "sha256": None}
        return row


@pytest.fixture
def app(monkeypatch) -> FastAPI:
    monkeypatch.setattr(batch_config_module, "config_value", lambda key, default=None: default)
    monkeypatch.setattr(delivery, "_downloads", delivery.Counter())
    monkeypatch.setattr(delivery, "MAX_DOWNLOADS_PER_OWNER", 1)
    application = FastAPI()
    application.state.services = AppServices(
        router=None, batch_store=SlowBatchStore(), batch_file_store=_OutputFiles()
    )

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        return {"authenticated": True, "user_id": OWNER, "role": "internal"}

    application.dependency_overrides[verify_batch_management_identity] = identity
    application.dependency_overrides[get_current_user] = identity
    for module in (batches, files, batch_console):
        application.include_router(module.router)
    return application


async def _never(lines: int = 0):
    for _n in range(lines):
        yield b"x\n"


@pytest.mark.asyncio
async def test_a_download_past_the_owners_slots_is_a_429_with_retry_after(app):
    held = delivery.hold_download(_never(), OWNER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in [*RENDERED, "/v1/files/file-output/content"]:
            refused = await client.get(path, headers={"Authorization": "Bearer k"})
            assert refused.status_code == 429, path
            assert refused.headers["retry-after"] == str(delivery.DOWNLOAD_RETRY_AFTER_SECONDS)
            assert "Too many batch downloads" in refused.json()["detail"]
        # An upload is sent as stored, not rendered: it takes no slot.
        upload = await client.get("/v1/files/file-1/content", headers={"Authorization": "Bearer k"})
        assert upload.status_code == 200

        await held.aclose()
        for path in RENDERED:
            served = await client.get(path, headers={"Authorization": "Bearer k"})
            assert served.status_code == 200, path
    # Every download gave its slot back as its response ended.
    assert dict(delivery._downloads) == {}
