"""Declared upload sizes must never crash on valid large or malformed headers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving.batch.admission import read_body
from serving.servers.routers.files import store_upload


def _request(length: bytes, body: bytes = b"{}") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/files",
            "headers": [
                (b"content-length", length),
                (b"content-type", b"multipart/form-data; boundary=test"),
            ],
        },
        receive,
    )


@pytest.mark.parametrize(
    "length", [b"9" * 5000, b"0" * 5000 + b"999999"], ids=["huge", "zero-padded-huge"]
)
@pytest.mark.parametrize("file_upload", [False, True])
async def test_enormous_declared_lengths_are_413_before_reading(length, file_upload):
    request = _request(length)
    request._receive = AsyncMock(side_effect=AssertionError("body must not be read"))
    store = SimpleNamespace(create_upload=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        if file_upload:
            await store_upload(
                request,
                file_store=store,
                owner="owner",
                check_owner=False,
                config=SimpleNamespace(max_body_bytes=1024),
            )
        else:
            await read_body(request, 1024)

    assert raised.value.status_code == 413
    request._receive.assert_not_awaited()
    store.create_upload.assert_not_awaited()


@pytest.mark.parametrize(
    "length", [b"0" * 5000 + b"2", b"\xb2", b"nonsense"], ids=["zero-padded", "non-ascii", "text"]
)
async def test_small_or_malformed_lengths_keep_the_streamed_limit(length):
    assert await read_body(_request(length), 2) == b"{}"
    with pytest.raises(HTTPException) as raised:
        await read_body(_request(length, b"too big"), 2)
    assert raised.value.status_code == 413
