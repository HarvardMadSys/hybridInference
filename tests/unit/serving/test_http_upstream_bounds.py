"""What an upstream can make the shared HTTP client hold.

The SSE parser kept an unterminated frame without limit and searched the whole
of it again on every read; NDJSON lines likewise. A non-streaming body and an
error body were read whole. An upstream that never ended a frame, or never
stopped sending, filled memory for as long as the request lived.
"""

from __future__ import annotations

import codecs
import json
import random
import time

import aiohttp
import pytest
from aiohttp import web

import serving.http as http_mod
from serving.http import AsyncHTTPClient, UpstreamPayloadTooLarge, _LineSplitter
from serving.servers.sse import SSEFrameTooLarge, SSEMessage, SSEParser

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The SSE parser: incremental, and bounded
# ---------------------------------------------------------------------------


class _ReferenceParser:
    """The parser as it was before the frame limit: the behaviour to keep.

    Appends every read to one string and searches all of it for the blank line.
    Quadratic in a frame's length, which is what the rewrite removes -- but its
    output is the definition of correct.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._pending_cr = ""

    def feed(self, chunk: bytes) -> list[SSEMessage]:
        data = self._pending_cr + self._decoder.decode(chunk, final=False)
        self._pending_cr = ""
        if data.endswith("\r"):
            self._pending_cr = "\r"
            data = data[:-1]
        self._buffer += data.replace("\r\n", "\n").replace("\r", "\n")
        messages = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            if frame.strip():
                messages.append(SSEParser._parse_frame(frame))
        return messages

    def flush(self) -> list[SSEMessage]:
        residue = self._buffer + ("\n" if self._pending_cr else "")
        self._buffer = self._pending_cr = ""
        if not residue.strip():
            return []
        message = SSEParser._parse_frame(residue)
        return [message] if message.data else []


_TOKENS = [
    b"data: ",
    b"data:",
    b"event: delta",
    b"id: 7",
    b": keepalive",
    b"\n",
    b"\n",
    b"\r",
    b"\r\n",
    b"\n\n",
    b"\r\n\r\n",
    b'{"a":1}',
    b"[DONE]",
    b"x",
    b" ",
    "中".encode(),
    b"\xe2\x80\xa8",  # U+2028, which JSON carries raw
    b"\xff",
]


@pytest.mark.parametrize("seed", range(300))
def test_the_incremental_parser_frames_every_stream_as_before(seed):
    """Same frames, in the same reads, as the parser it replaces.

    Random token streams cut at random byte offsets: blank lines and CRLF pairs
    straddling reads, multibyte characters split, frames of every length.
    """
    rng = random.Random(seed)
    body = b"".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 80)))
    cuts = sorted(rng.sample(range(len(body) + 1), k=min(len(body) + 1, rng.randint(0, 12))))
    pieces = [body[a:b] for a, b in zip([0, *cuts], [*cuts, len(body)], strict=True)]

    new, old = SSEParser(), _ReferenceParser()
    for piece in pieces:
        assert new.feed(piece) == old.feed(piece)
    assert new.flush() == old.flush()


def test_a_frame_over_the_limit_is_refused():
    parser = SSEParser(max_frame_chars=1000)

    parser.feed(b"data: " + b"x" * 900)
    with pytest.raises(SSEFrameTooLarge):
        parser.feed(b"x" * 200)


def test_frames_under_the_limit_pass_however_many_there_are():
    """The limit is per frame: a long stream of ordinary frames never reaches it."""
    parser = SSEParser(max_frame_chars=1000)
    frame = b'data: {"choices":[{"delta":{"content":"' + b"y" * 500 + b'"}}]}\n\n'

    messages = [m for _ in range(100) for m in parser.feed(frame)]

    assert len(messages) == 100


def test_a_frame_split_over_many_reads_costs_linear_time():
    """An 8 MiB frame in 4 KiB reads: the old parser took seconds, this milliseconds.

    The bound is generous -- tens of times what the linear parser needs -- so
    only a return to rescanning and recopying the held frame can fail it.
    """
    parser = SSEParser()
    chunk = b"x" * 4096
    start = time.perf_counter()
    parser.feed(b"data: ")
    for _ in range(8 * 1024 * 1024 // len(chunk)):
        parser.feed(chunk)
    (message,) = parser.feed(b"\n\n")
    elapsed = time.perf_counter() - start

    assert len(message.data) == 8 * 1024 * 1024
    assert elapsed < 1.5, f"8 MiB frame took {elapsed:.2f}s to parse"


def test_ndjson_lines_split_like_before_and_are_bounded():
    lines = _LineSplitter(max_chars=100)

    assert lines.feed('{"a":1}\n{"b"') == ['{"a":1}']
    assert lines.feed(":2}\n\n") == ['{"b":2}', ""]
    assert lines.feed("tail") == []
    assert lines.flush() == "tail"
    lines.feed("z" * 100)
    with pytest.raises(UpstreamPayloadTooLarge):
        lines.feed("z")


# ---------------------------------------------------------------------------
# stream_post: a frame over the limit fails the stream as an upstream error
# ---------------------------------------------------------------------------


class _EndlessFrameResp:
    """An SSE response whose frame never ends."""

    def __init__(self, reads: int) -> None:
        self._reads = reads
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200
        self.read_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def iter_chunked(self, n: int):
        yield b'data: {"choices":[{"delta":{"content":"'
        for _ in range(self._reads):
            self.read_count += 1
            yield b"x" * n


def _patch_session_returning(monkeypatch, resp, seen: list | None = None) -> None:
    class _Session:
        def post(self, *_a, **kwargs):
            if seen is not None:
                seen.append(kwargs)
            return resp

    async def _ensure(_self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _ensure)


@pytest.mark.asyncio
async def test_a_frame_that_never_ends_fails_the_stream_with_a_payload_error(monkeypatch):
    monkeypatch.setattr(http_mod, "MAX_FRAME_CHARS", 64 * 1024)
    resp = _EndlessFrameResp(reads=10_000)
    _patch_session_returning(monkeypatch, resp)

    with pytest.raises(UpstreamPayloadTooLarge) as caught:
        async for _ in AsyncHTTPClient.shared().stream_post("http://up/sse", json={}, mode="sse"):
            pass

    # The error every adapter already handles as a broken upstream payload.
    assert isinstance(caught.value, aiohttp.ClientPayloadError)
    # Stopped at the limit, not at the end of what the upstream would send.
    assert resp.read_count * 4096 < 64 * 1024 + 2 * 4096


# ---------------------------------------------------------------------------
# Bodies read whole: non-streaming responses, error responses, proxied ones
# ---------------------------------------------------------------------------


class _Upstream:
    """A local server whose answers the tests choose."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b"{}"
        self.content_type = "application/json"
        self.chunked = False
        self.sent = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not self.chunked:
            return web.Response(status=self.status, body=self.body, content_type=self.content_type)
        # No Content-Length: the size is only known by reading.
        response = web.StreamResponse(
            status=self.status, headers={"Content-Type": self.content_type}
        )
        response.enable_chunked_encoding()
        await response.prepare(request)
        try:
            for start in range(0, len(self.body), 65536):
                await response.write(self.body[start : start + 65536])
                self.sent += len(self.body[start : start + 65536])
            await response.write_eof()
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            pass
        return response


@pytest.fixture
async def upstream(monkeypatch):
    client = AsyncHTTPClient()  # one session per test, on this test's loop
    monkeypatch.setattr(AsyncHTTPClient, "_shared", client)
    server = _Upstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server.url = f"http://127.0.0.1:{port}/v1/chat/completions"
    try:
        yield server
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_json_body_is_still_decoded_as_before(upstream):
    upstream.body = json.dumps({"choices": [{"message": {"content": "héllo"}}]}).encode()

    out = await AsyncHTTPClient.shared().json_post(upstream.url, json={})

    assert out == {"choices": [{"message": {"content": "héllo"}}]}


@pytest.mark.asyncio
async def test_a_non_json_body_still_raises_the_content_type_error(upstream):
    upstream.body, upstream.content_type = b"<html>oops</html>", "text/html"

    with pytest.raises(aiohttp.ContentTypeError):
        await AsyncHTTPClient.shared().json_post(upstream.url, json={})


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_a_body_over_the_limit_is_refused(upstream, monkeypatch, chunked):
    """Declared over the limit: refused unread. Undeclared: refused as it passes it."""
    monkeypatch.setattr(http_mod, "MAX_RESPONSE_BODY_BYTES", 256 * 1024)
    upstream.body = json.dumps({"data": "z" * (4 * 1024 * 1024)}).encode()
    upstream.chunked = chunked

    with pytest.raises(UpstreamPayloadTooLarge):
        await AsyncHTTPClient.shared().json_post(upstream.url, json={})


@pytest.mark.asyncio
async def test_a_proxied_body_over_the_limit_is_refused(upstream, monkeypatch):
    monkeypatch.setattr(http_mod, "MAX_RESPONSE_BODY_BYTES", 1024)
    upstream.body = b"q" * 4096

    with pytest.raises(UpstreamPayloadTooLarge):
        await AsyncHTTPClient.shared().request("GET", upstream.url)

    upstream.body = b"q" * 512
    status, body, _ = await AsyncHTTPClient.shared().request("GET", upstream.url)
    assert (status, body) == (200, b"q" * 512)


@pytest.mark.asyncio
async def test_only_the_start_of_an_error_body_is_read(upstream):
    """The provider's message survives; a huge error page is not held whole."""
    message = '{"error":{"message":"model overloaded"}}'
    upstream.status = 503
    upstream.body = (message + " " * (8 * 1024 * 1024)).encode()
    upstream.chunked = True

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await AsyncHTTPClient.shared().json_post(upstream.url, json={})

    assert caught.value.status == 503
    assert caught.value.error_body.startswith(message)
    assert len(caught.value.error_body) == http_mod.MAX_ERROR_BODY_BYTES


@pytest.mark.asyncio
async def test_a_stream_error_body_is_capped_too(monkeypatch):
    from tests.unit.serving.test_http import _ErrorResp

    resp = _ErrorResp(502, "bad gateway " + "#" * (1024 * 1024), reason="Bad Gateway")

    class _CM:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *_exc):
            return False

    _patch_session_returning(monkeypatch, _CM())

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        async for _ in AsyncHTTPClient.shared().stream_post("http://up/sse", json={}):
            pass

    assert caught.value.status == 502
    assert caught.value.error_body.startswith("bad gateway ")
    assert resp.content.read_bytes <= http_mod.MAX_ERROR_BODY_BYTES + 64 * 1024
