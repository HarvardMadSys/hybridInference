"""End-of-body handling for the SSE branch of ``stream_post``.

The SSE reader used to drop whatever frame the parser was still holding when
the response body ended, while the NDJSON branch beside it flushed its tail.
Upstreams that close the body right after ``data: [DONE]`` (or after the final
``finish_reason`` chunk) without SSE's trailing blank line therefore lost their
terminal frame, and ``openai_compat`` reported a finished generation as
``_INCOMPLETE_STREAM_ERROR`` -- a ``stream_exception`` charged against the
endpoint's availability until the circuit breaker opened.
"""

from __future__ import annotations

import aiohttp
import pytest

from serving.http import AsyncHTTPClient


class _Resp:
    """Minimal aiohttp response stand-in whose body ends however we choose."""

    def __init__(self, chunks: list[bytes], *, error: BaseException | None = None):
        self._chunks = chunks
        self._error = error
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def iter_chunked(self, _n: int):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _patch_session(monkeypatch, resp: _Resp) -> None:
    async def fake_ensure(self):
        class _Session:
            def post(self, *_a, **_k):
                return resp

        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure)


async def _collect(client, resp, monkeypatch) -> list[str]:
    _patch_session(monkeypatch, resp)
    return [
        line
        async for line in client.stream_post(
            "http://example/sse", json={"stream": True}, mode="sse"
        )
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_done_without_trailing_blank_line_still_reaches_the_consumer(monkeypatch):
    """``data: [DONE]\\n`` at end of body is the exact production failure."""
    resp = _Resp([b'data: {"x":1}\n\n', b"data: [DONE]\n"])

    lines = await _collect(AsyncHTTPClient.shared(), resp, monkeypatch)

    assert lines == ['data: {"x":1}', "data: [DONE]"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_finish_reason_frame_survives_a_bodiless_ending(monkeypatch):
    """A provider that ends on ``finish_reason`` and never sends ``[DONE]``."""
    terminal = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
    resp = _Resp([b'data: {"x":1}\n\n', terminal.encode()])

    lines = await _collect(AsyncHTTPClient.shared(), resp, monkeypatch)

    assert lines == ['data: {"x":1}', terminal]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_properly_terminated_stream_is_unchanged(monkeypatch):
    """The happy path must not gain a frame, or duplicate its last one."""
    resp = _Resp([b'data: {"x":1}\n\n', b"data: [DONE]\n\n", b"data: ignored\n\n"])

    lines = await _collect(AsyncHTTPClient.shared(), resp, monkeypatch)

    # [DONE] still terminates the iteration -- anything after it is not read.
    assert lines == ['data: {"x":1}', "data: [DONE]"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_only_residue_is_not_emitted_as_a_bare_data_frame(monkeypatch):
    """A dangling ``id:`` line must not become ``data: `` on the wire."""
    resp = _Resp([b'data: {"x":1}\n\n', b"id: 42\n"])

    lines = await _collect(AsyncHTTPClient.shared(), resp, monkeypatch)

    assert lines == ['data: {"x":1}']


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aborted_read_does_not_flush_a_half_received_frame(monkeypatch):
    """A real truncation stays a truncation.

    The flush is placed after the read loop precisely so an aborted body raises
    past it. Emitting the residue here would cap a half-received frame as if the
    upstream had finished -- the failure mode the incomplete-stream check exists
    to catch.
    """
    resp = _Resp(
        [b'data: {"x":1}\n\n', b'data: {"choices":[{"delta":{"content":"hel'],
        error=aiohttp.ClientPayloadError("connection reset"),
    )

    _patch_session(monkeypatch, resp)
    lines: list[str] = []
    with pytest.raises(aiohttp.ClientPayloadError):
        async for line in AsyncHTTPClient.shared().stream_post(
            "http://example/sse", json={"stream": True}, mode="sse"
        ):
            lines.append(line)

    assert lines == ['data: {"x":1}']


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_body_yields_nothing(monkeypatch):
    """No frames in, no frames out -- the flush must not invent one."""
    resp = _Resp([])

    lines = await _collect(AsyncHTTPClient.shared(), resp, monkeypatch)

    assert lines == []
