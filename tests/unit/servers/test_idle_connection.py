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


# -- a release handoff winds connections down -------------------------------------


@pytest.fixture
def draining():
    from serving.servers import idle_connection

    idle_connection.set_draining(True)
    yield
    idle_connection.set_draining(False)


@pytest.mark.asyncio
async def test_while_draining_an_idle_connection_gets_the_keep_alive_limit(monkeypatch, draining):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.05)
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 600.0, lambda: "peer", lambda: False)
    watch.touch()

    await asyncio.sleep(0.15)
    assert transport.closed


@pytest.mark.asyncio
async def test_while_draining_a_connection_with_a_request_keeps_the_normal_limit(
    monkeypatch, draining
):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.05)
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 600.0, lambda: "peer", lambda: True)
    watch.touch()

    await asyncio.sleep(0.15)
    assert not transport.closed
    watch.cancel()


@pytest.mark.asyncio
async def test_a_request_begun_after_the_short_limit_was_armed_is_not_cut(monkeypatch, draining):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.05)
    busy = [False]
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 600.0, lambda: "peer", lambda: busy[0])
    watch.touch()  # idle: the short limit
    busy[0] = True  # headers arrived, a quiet stream follows

    await asyncio.sleep(0.15)
    assert not transport.closed
    watch.cancel()


@pytest.mark.asyncio
async def test_draining_applies_even_when_the_idle_timeout_is_disabled(monkeypatch, draining):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.05)
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0, lambda: "peer", lambda: False)
    watch.touch()

    await asyncio.sleep(0.15)
    assert transport.closed


@pytest.mark.asyncio
async def test_while_draining_a_request_with_nothing_moving_is_closed_at_the_normal_limit(
    monkeypatch, draining
):
    """Draining never extends the normal limit: a stalled request is closed as ever."""
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.02)
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.1, lambda: "peer", lambda: True)
    watch.touch()

    await asyncio.sleep(0.06)
    assert not transport.closed  # neither the short limit nor yet the normal one
    await asyncio.sleep(0.15)
    assert transport.closed


@pytest.mark.asyncio
async def test_a_request_begun_after_the_short_limit_gets_the_normal_limit_from_its_last_byte(
    monkeypatch, draining
):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.03)
    busy = [False]
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.15, lambda: "peer", lambda: busy[0])
    watch.touch()  # idle: the short limit
    busy[0] = True  # a request began, then nothing moved

    await asyncio.sleep(0.08)
    assert not transport.closed  # past the short limit, inside the normal one
    await asyncio.sleep(0.2)
    assert transport.closed  # the normal limit, from the last byte


@pytest.mark.asyncio
async def test_a_request_begun_after_the_short_limit_keeps_a_disabled_normal_limit(
    monkeypatch, draining
):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.02)
    busy = [False]
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0, lambda: "peer", lambda: busy[0])
    watch.touch()
    busy[0] = True

    await asyncio.sleep(0.15)
    assert not transport.closed
    watch.cancel()


@pytest.mark.asyncio
async def test_while_draining_a_stream_that_keeps_sending_is_never_cut(monkeypatch, draining):
    from serving.servers import idle_connection

    monkeypatch.setattr(idle_connection, "DRAIN_IDLE_SECONDS", 0.02)
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.2, lambda: "peer", lambda: True)
    watch.touch()

    for _ in range(15):  # 0.45 s of frames, each well inside the normal limit
        await asyncio.sleep(0.03)
        watch.touch()
        assert not transport.closed
    await asyncio.sleep(0.4)
    assert transport.closed  # and once it stops, the normal limit applies


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_without_a_drain_the_normal_limit_applies_whatever_the_connection_carries(busy):
    from serving.servers import idle_connection

    assert not idle_connection._draining
    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.1, lambda: "peer", lambda: busy)
    watch.touch()

    await asyncio.sleep(0.05)
    assert not transport.closed
    await asyncio.sleep(0.15)
    assert transport.closed


@pytest.mark.asyncio
async def test_starting_a_drain_does_not_restart_a_connection_s_clock(monkeypatch):
    from serving.servers import idle_connection

    transport = _Transport()
    watch = IdleWatch(transport, lambda: 0.15, lambda: "peer", lambda: True)
    idle_connection._BY_TRANSPORT[id(transport)] = watch
    try:
        watch.touch()
        await asyncio.sleep(0.1)
        idle_connection.set_draining(True)
        await asyncio.sleep(0.1)
        assert transport.closed  # 0.15 from its last byte, not from the drain
    finally:
        idle_connection.set_draining(False)
        idle_connection._BY_TRANSPORT.pop(id(transport), None)


class _Closing(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.closed = True


@pytest.mark.asyncio
async def test_connections_are_judged_by_the_request_each_carries(monkeypatch):
    """A probe's connection is control while it carries the probe; a gone one counts nowhere."""
    from serving.servers import handoff, idle_connection

    def watch(transport, path):
        return IdleWatch(
            transport, lambda: 600.0, lambda: "peer", lambda: path is not None, lambda: path
        )

    watches = {
        1: watch(_Transport(), "/health/ready"),  # a probe in progress
        2: watch(_Transport(), "/internal/handoff/seal"),  # the release's call
        3: watch(_Transport(), "/v1/chat/completions"),  # a request
        4: watch(_Transport(), None),  # idle, kept alive
        5: watch(_Closing(), "/health"),  # a probe whose client has gone
    }
    monkeypatch.setattr(idle_connection, "_BY_TRANSPORT", watches)

    assert idle_connection.connection_counts(handoff.is_control_path) == {
        "open": 4,
        "busy": 3,
        "control": 2,
        "other": 2,
    }
