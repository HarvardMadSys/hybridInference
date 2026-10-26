"""Chunked Files uploads must bound the whole multipart body, not only file bytes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving.servers.routers import files


@pytest.mark.parametrize("padding_location", ["preamble", "epilogue"])
async def test_chunked_upload_enforces_total_body_limit(monkeypatch, padding_location):
    config = SimpleNamespace(
        enabled=True,
        min_role="internal",
        expansion_enabled=True,
        max_body_bytes=1024,
        files_max_total_bytes=1024 * 1024,
        file_uploads_per_hour=10,
        file_retention_days=7,
    )
    monkeypatch.setattr(files, "batch_config", lambda: config)
    store = SimpleNamespace(
        create_upload=AsyncMock(return_value="file-review"),
        append_chunk=AsyncMock(),
        abort_upload=AsyncMock(),
        finish_upload=AsyncMock(return_value={"id": "file-review", "bytes": 3}),
    )
    multipart = (
        b'--review\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
        b'--review\r\nContent-Disposition: form-data; name="file"; filename="batch.jsonl"'
        b"\r\n\r\n{}\n\r\n--review--\r\n"
    )
    padding = [b"x" * 8192 for _ in range(32)]
    chunks = [*padding, multipart] if padding_location == "preamble" else [multipart, *padding]
    consumed = 0

    async def receive():
        nonlocal consumed
        index = consumed
        consumed += 1
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/files",
            "headers": [(b"content-type", b"multipart/form-data; boundary=review")],
        },
        receive,
    )
    with pytest.raises(HTTPException) as raised:
        await files.upload_file(
            request, user_ctx={"user_id": "review", "role": "internal"}, file_store=store
        )
    assert raised.value.status_code == 413
    assert consumed < len(chunks), "Oversized stream was consumed through its end"
    store.finish_upload.assert_not_awaited()
    store.abort_upload.assert_awaited_once()
