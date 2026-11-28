"""Rendered batch downloads are refused, not queued, when the process has no slot free (M27).

A download holding a slot is ended once its client reads nothing of it for a
while: a client that stopped reading would otherwise keep the slot until the
stream cap, an hour by default.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from serving.batch import config as batch_config_module, delivery
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import batch_console, batches, files
from serving.servers.streaming_response import SEND_PART_BYTES
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


def _scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"spec_version": "2.3"},
        "method": "GET",
        "path": "/v1/files/file-output/content",
        "headers": [],
        "query_string": b"",
    }


async def _connected() -> dict[str, Any]:
    """An ASGI ``receive`` whose client never goes away."""
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_a_client_that_stops_reading_ends_its_download_and_frees_its_slot(monkeypatch):
    monkeypatch.setattr(delivery, "_downloads", delivery.Counter())
    monkeypatch.setattr(files, "DOWNLOAD_SEND_TIMEOUT_SECONDS", 0.05)
    closed: list[bool] = []

    async def body():
        try:
            yield b"x" * (3 * SEND_PART_BYTES)
            yield b"never sent\n"
        finally:
            closed.append(True)

    scope = _scope()
    response = files.jsonl_download(Request(scope), body(), headers={}, owner=OWNER)
    assert dict(delivery._downloads) == {OWNER: 1}
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        if len(sent) == 2:
            await asyncio.Event().wait()  # the client stops reading
        sent.append(message)

    await asyncio.wait_for(response(scope, _connected, send), timeout=2)

    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert len(sent[1]["body"]) == SEND_PART_BYTES and sent[1]["more_body"]
    assert closed == [True]
    assert dict(delivery._downloads) == {}


@pytest.mark.asyncio
async def test_a_held_download_goes_out_whole_in_parts(monkeypatch):
    monkeypatch.setattr(delivery, "_downloads", delivery.Counter())
    large = bytes(range(256)) * (2 * SEND_PART_BYTES // 256) + b"tail"

    async def body():
        yield large
        yield b"end\n"

    scope = _scope()
    response = files.jsonl_download(Request(scope), body(), headers={}, owner=OWNER)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await asyncio.wait_for(response(scope, _connected, send), timeout=2)

    parts = [message for message in sent if message["type"] == "http.response.body"]
    assert all(len(part["body"]) <= SEND_PART_BYTES for part in parts)
    assert b"".join(part["body"] for part in parts) == large + b"end\n"
    assert [part["more_body"] for part in parts] == [True] * (len(parts) - 1) + [False]
    assert dict(delivery._downloads) == {}
