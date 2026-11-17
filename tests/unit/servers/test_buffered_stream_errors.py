"""Errors on a forced stream answered as one JSON body.

With ``force_chat_completions_streaming`` a non-streaming request is streamed
upstream and buffered by ``_streaming_response_with_keepalive``. A failure
reaches it as the session's error frame, whose message is already scrubbed for
the user. The caller has to get that message, as a streaming caller does: an
on-demand model's "queued, retry later" must not turn into a generic error.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from serving.servers.routers import completions
from serving.servers.routers.completions import _streaming_response_with_keepalive

_QUEUED = (
    "Llama-3.1-8B is queued on the cluster, waiting for a GPU. Retry later; "
    "batch requests wait on their own. (request_id: req_1)"
)


def _error_frame(**error: object) -> str:
    return f"data: {json.dumps({'error': {'type': 'server_error', **error}})}\n\n"


async def _drain(chunks) -> bytes:
    gen = _streaming_response_with_keepalive(
        chunks, request_id="req_1", model="llama-3.1-8b", request_headers={}
    )
    return b"".join([part async for part in gen])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_queued_model_keeps_its_status_and_message():
    async def _chunks():
        yield _error_frame(message=_QUEUED, code=503)

    with pytest.raises(HTTPException) as caught:
        await _drain(_chunks())

    assert caught.value.status_code == 503
    assert caught.value.detail == _QUEUED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_frame_without_a_message_gets_the_generic_one():
    async def _chunks():
        yield _error_frame(code=502)

    with pytest.raises(HTTPException) as caught:
        await _drain(_chunks())

    assert caught.value.status_code == 502
    assert caught.value.detail.startswith("Internal server error")
    assert "req_1" in caught.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_code_that_is_not_an_http_status_becomes_a_500():
    async def _chunks():
        yield _error_frame(message="boom (request_id: req_1)", code=42)

    with pytest.raises(HTTPException) as caught:
        await _drain(_chunks())

    assert caught.value.status_code == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_after_a_keepalive_the_message_goes_in_the_body(monkeypatch):
    monkeypatch.setattr(completions, "_FORCE_STREAMING_KEEPALIVE_S", 0.01)

    async def _chunks():
        await asyncio.sleep(0.1)
        yield _error_frame(message=_QUEUED, code=503)

    body = await _drain(_chunks())

    assert body.startswith(b" ")
    assert json.loads(body) == {"error": {"message": _QUEUED, "code": 503}}
