"""Accepted connections close once bytes stop, including before the headers finish."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from serving.servers.idle_connection import (
    IdleWatch,
    install_idle_connection_timeout,
    parse_idle_timeout_seconds,
)

pytestmark = pytest.mark.unit


class _Transport:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_uvicorn_closes_a_partial_request(monkeypatch):
    """A request that never finishes its headers is the keep-alive hole."""
    import uvicorn

    monkeypatch.setattr("serving.servers.idle_connection.idle_timeout_seconds", lambda: 0.2)
    install_idle_connection_timeout()

    async def app(scope, receive, send):
        await asyncio.Event().wait()

    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(64), 1.5)
        assert data == b""
        writer.close()
    finally:
        await _stop_server(server, task)


@pytest.mark.asyncio
async def test_uvicorn_keeps_a_connection_that_keeps_writing(monkeypatch):
    import uvicorn

    monkeypatch.setattr("serving.servers.idle_connection.idle_timeout_seconds", lambda: 0.2)
    install_idle_connection_timeout()

    async def app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        while True:
            await send({"type": "http.response.body", "body": b"data: hi\n\n", "more_body": True})
            await asyncio.sleep(0.05)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        await writer.drain()
        chunks: list[bytes] = []
        eof = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.6
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(1024), deadline - loop.time())
            except TimeoutError:
                break
            if chunk == b"":
                eof = True
                break
            chunks.append(chunk)
        assert b"data: hi" in b"".join(chunks)
        assert not eof
        writer.close()
    finally:
        await _stop_server(server, task)


async def _stop_server(server, task: asyncio.Task[None]) -> None:
    """Ask uvicorn to exit, then cancel it. A handler that ignores disconnect would hang."""
    server.should_exit = True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, 1)


def test_install_patches_both_http_protocols_once():
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

    install_idle_connection_timeout()
    assert HttpToolsProtocol._idle_connection_timeout_installed
    assert H11Protocol._idle_connection_timeout_installed
    made = HttpToolsProtocol.connection_made
    install_idle_connection_timeout()
    assert HttpToolsProtocol.connection_made is made


def test_invalid_idle_timeout_keeps_the_default_and_non_positive_disables():
    assert parse_idle_timeout_seconds(None) == 600.0
    assert parse_idle_timeout_seconds("  ") == 600.0
    assert parse_idle_timeout_seconds("nope") == 600.0
    assert parse_idle_timeout_seconds("0") == 0.0
    assert parse_idle_timeout_seconds("-5") == 0.0
    assert parse_idle_timeout_seconds("12.5") == 12.5


@pytest.mark.asyncio
async def test_silence_closes_the_transport():
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.05, lambda: "peer")
    watch.touch()

    await asyncio.sleep(0.15)

    assert transport.closed


@pytest.mark.asyncio
async def test_later_bytes_postpone_the_close():
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.1, lambda: "peer")
    watch.touch()
    await asyncio.sleep(0.06)
    watch.touch()
    await asyncio.sleep(0.06)

    assert not transport.closed

    await asyncio.sleep(0.15)
    assert transport.closed


@pytest.mark.asyncio
async def test_zero_timeout_does_not_arm():
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0, lambda: "peer")
    watch.touch()

    await asyncio.sleep(0.05)

    assert not transport.closed


@pytest.mark.asyncio
async def test_a_partial_request_is_closed_when_the_client_stops():
    """The keep-alive hole: the first byte cancels uvicorn's timer, and then nothing."""

    class _Protocol:
        client = ("172.16.80.2", 34440)

        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            self.data = data

        def connection_lost(self, exc):
            self.lost = exc

    from serving.servers.idle_connection import _wrap

    _wrap(_Protocol)
    protocol = _Protocol()
    transport = _Transport()
    protocol.connection_made(transport)
    # The production clock is minutes long; this connection uses a short one.
    # data_received touches again, which is what cancels the long timer.
    protocol._idle_watch._timeout_s = lambda: 0.05
    protocol.data_received(b"POST /v1/chat/completions HTTP/1.1\r\n")

    await asyncio.sleep(0.15)

    assert transport.closed
    assert protocol.data.startswith(b"POST")


@pytest.mark.asyncio
async def test_response_bytes_postpone_the_close_and_lost_cancels_it():
    class _Protocol:
        client = None

        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            self.data = data

        def connection_lost(self, exc):
            self.lost = exc

    from serving.servers.idle_connection import _wrap

    _wrap(_Protocol)
    protocol = _Protocol()
    transport = _Transport()
    from serving.servers.idle_connection import _touch_transport

    protocol.connection_made(transport)
    protocol._idle_watch._timeout_s = lambda: 0.1
    # What a response chunk does: the cycle records the write, it does not
    # replace transport.write (uvloop's is read-only).
    _touch_transport(transport)
    await asyncio.sleep(0.06)
    assert not transport.closed

    protocol.connection_lost(None)
    await asyncio.sleep(0.15)
    assert not transport.closed
