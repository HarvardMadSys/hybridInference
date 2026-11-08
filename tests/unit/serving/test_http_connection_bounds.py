"""How long a stream may wait for a connection, and who bounds how many there are.

The shared connector allowed 50 connections per host, and stream opens had no
timeout at all, so the 51st concurrent stream to one provider waited forever
for a pooled connection -- holding its user's slot and the provider's outbound
slot while it did. The gateway's own limiters bound concurrency now, and opening
a stream's connection is bounded in time.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web

from serving.http import (
    STREAM_CONNECT_TIMEOUT_S,
    STREAM_SOCK_CONNECT_TIMEOUT_S,
    AsyncHTTPClient,
    stream_timeout,
    upstream_connection_limit,
)
from serving.utils.metadata_addresses import refuse_metadata_socket

pytestmark = pytest.mark.unit


def _patch_session_returning(monkeypatch, resp, seen: list | None = None) -> None:
    class _Session:
        def post(self, *_a, **kwargs):
            if seen is not None:
                seen.append(kwargs)
            return resp

    async def _ensure(_self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _ensure)


class _DoneResp:
    """An SSE response that is just the terminator."""

    def __init__(self) -> None:
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def iter_chunked(self, _n: int):
        yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# The connector: the gateway's limiters bound concurrency, not the pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shared_connector_has_no_per_host_limit_and_keeps_its_socket_guard():
    client = AsyncHTTPClient()
    try:
        session = await client._ensure_session()
        connector = session.connector
        # Only a safety net in total, far above the gateway's own limits.
        assert connector.limit == upstream_connection_limit() >= 256
        assert connector.limit_per_host == 0
        # The metadata refusal still applies to every upstream socket.
        assert connector._socket_factory is refuse_metadata_socket
    finally:
        await client.close()


def test_stream_opens_get_a_bounded_connect_and_keep_everything_else():
    bare = stream_timeout()
    assert bare.total is None
    assert bare.connect == STREAM_CONNECT_TIMEOUT_S
    assert bare.sock_connect == STREAM_SOCK_CONNECT_TIMEOUT_S

    first_byte = stream_timeout(aiohttp.ClientTimeout(total=None, sock_read=90))
    assert (first_byte.total, first_byte.sock_read) == (None, 90)
    assert first_byte.connect == STREAM_CONNECT_TIMEOUT_S

    explicit = stream_timeout(aiohttp.ClientTimeout(total=120, connect=3, sock_connect=2))
    assert (explicit.total, explicit.connect, explicit.sock_connect) == (120, 3, 2)


@pytest.mark.asyncio
async def test_stream_post_opens_with_the_connect_bounds(monkeypatch):
    seen: list[dict] = []
    _patch_session_returning(monkeypatch, _DoneResp(), seen)

    first_byte = aiohttp.ClientTimeout(total=None, sock_read=45)
    lines = [
        line
        async for line in AsyncHTTPClient.shared().stream_post(
            "http://up/sse", json={}, timeout=first_byte
        )
    ]

    assert lines == ["data: [DONE]"]
    (kwargs,) = seen
    assert kwargs["timeout"].connect == STREAM_CONNECT_TIMEOUT_S
    assert kwargs["timeout"].sock_connect == STREAM_SOCK_CONNECT_TIMEOUT_S
    assert kwargs["timeout"].sock_read == 45
    assert kwargs["timeout"].total is None


@pytest.mark.asyncio
async def test_a_connect_timeout_is_an_ordinary_upstream_failure(monkeypatch):
    """Raised at once, without the stale-socket retry, as a ClientError and a timeout.

    Those are what the adapters and the router already treat as a failed
    attempt that sent nothing: release the slots, record it, fall back.
    """
    calls = []

    class _CM:
        async def __aenter__(self):
            calls.append(1)
            raise aiohttp.ConnectionTimeoutError("Connection timeout to host http://up/sse")

        async def __aexit__(self, *_exc):
            return False

    _patch_session_returning(monkeypatch, _CM())

    with pytest.raises(aiohttp.ClientError) as caught:
        async for _ in AsyncHTTPClient.shared().stream_post("http://up/sse", json={}):
            pass

    assert isinstance(caught.value, TimeoutError)
    assert calls == [1]


@pytest.mark.asyncio
async def test_more_streams_than_the_old_per_host_limit_all_open():
    """Sixty concurrent streams to one host all start; the old pool held ten back forever.

    Each stream sends its first frame and then stays open, as a generation does,
    until every one of them has started.
    """
    started = 0
    all_started = asyncio.Event()
    streams = 60

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal started
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"x":1}\n\n')
        started += 1
        if started == streams:
            all_started.set()
        await all_started.wait()
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/sse", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/sse"
    client = AsyncHTTPClient()

    async def one() -> list[str]:
        return [line async for line in client.stream_post(url, json={})]

    try:
        results = await asyncio.wait_for(asyncio.gather(*(one() for _ in range(streams))), 20)
    finally:
        all_started.set()  # let any stream still open finish, so cleanup is quick
        await client.close()
        await runner.cleanup()

    assert all(lines == ['data: {"x":1}', "data: [DONE]"] for lines in results)


# ---------------------------------------------------------------------------
# A safety net on the pool's size, and bounded opens for non-streaming calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("soft", "expected"),
    [(1024, 512), (65536, 4096), (100, 256), (None, 4096)],
    ids=["docker-default", "raised", "tiny", "unlimited"],
)
def test_the_pool_holds_at_most_half_the_open_file_limit(monkeypatch, soft, expected):
    """Each relayed request also holds its client's socket; EMFILE fails the whole process."""
    import resource

    limit = resource.RLIM_INFINITY if soft is None else soft
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (limit, resource.RLIM_INFINITY))

    assert upstream_connection_limit() == expected


class _JsonResp:
    def __init__(self) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.content_length = None
        self.charset = None
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def iter_chunked(self, _n: int):
        yield b'{"ok": true}'


@pytest.mark.asyncio
async def test_a_non_streaming_post_opens_with_the_connect_bounds(monkeypatch):
    """A completion's 600 s total was all that bounded its connect: ~127 s per dead key."""
    seen: list[dict] = []

    class _Session:
        def post(self, *_a, **kwargs):
            seen.append(kwargs)
            return _JsonResp()

    async def _ensure(_self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _ensure)

    out = await AsyncHTTPClient.shared().json_post(
        "http://up/v1/chat/completions", json={}, timeout=aiohttp.ClientTimeout(total=600)
    )

    assert out == {"ok": True}
    (kwargs,) = seen
    assert kwargs["timeout"].total == 600
    assert kwargs["timeout"].connect == STREAM_CONNECT_TIMEOUT_S
    assert kwargs["timeout"].sock_connect == STREAM_SOCK_CONNECT_TIMEOUT_S


@pytest.mark.asyncio
async def test_the_session_default_bounds_the_connect_too():
    client = AsyncHTTPClient()
    try:
        session = await client._ensure_session()
        assert session.timeout.total == 60
        assert session.timeout.connect == STREAM_CONNECT_TIMEOUT_S
        assert session.timeout.sock_connect == STREAM_SOCK_CONNECT_TIMEOUT_S
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_request_past_the_pool_limit_fails_over_within_its_connect_timeout(monkeypatch):
    """It waits for a pooled connection only as long as it may wait to connect."""
    import serving.http as http_mod

    monkeypatch.setattr(http_mod, "upstream_connection_limit", lambda: 1)
    release = asyncio.Event()

    async def handle(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"x":1}\n\n')
        await release.wait()
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_post("/sse", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/sse"
    client = AsyncHTTPClient()
    stream = client.stream_post(url, json={})
    try:
        assert await stream.__anext__() == 'data: {"x":1}'  # holds the only connection
        with pytest.raises(aiohttp.ConnectionTimeoutError):
            await asyncio.wait_for(
                client.json_post(
                    url, json={}, timeout=aiohttp.ClientTimeout(total=30, connect=0.2)
                ),
                5,
            )
    finally:
        release.set()
        await stream.aclose()
        await client.close()
        await runner.cleanup()
