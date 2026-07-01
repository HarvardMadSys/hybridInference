from __future__ import annotations

import json

import aiohttp
import pytest

from serving.http import AsyncHTTPClient


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_post_with_retry_succeeds_on_third_attempt_after_two_failures(monkeypatch):
    client = AsyncHTTPClient.shared()

    calls = {"n": 0}

    async def fake_json_post(self, url: str, *, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            import aiohttp

            raise aiohttp.ClientError("transient")
        return {"ok": True}

    monkeypatch.setattr(AsyncHTTPClient, "json_post", fake_json_post)
    out = await client.json_post_with_retry("http://example/api", json={})
    assert out == {"ok": True}
    assert calls["n"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_post_with_retry_exhausts_all_attempts(monkeypatch):
    """Verify retry stops and raises after max attempts are exhausted."""
    client = AsyncHTTPClient.shared()

    async def always_fail(self, url: str, *, json=None, headers=None, timeout=None):
        import aiohttp

        raise aiohttp.ClientError("transient")

    monkeypatch.setattr(AsyncHTTPClient, "json_post", always_fail)

    with pytest.raises(aiohttp.ClientError):
        await client.json_post_with_retry("http://example/api", json={}, retries=3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_sse_wraps_lines(monkeypatch):
    client = AsyncHTTPClient.shared()

    class _Resp:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks
            self.content = self
            self.headers = {}
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def iter_chunked(self, n: int):
            for c in self._chunks:
                yield c

        def raise_for_status(self):
            return None

    def fake_post(*args, **kwargs):
        # Two events + DONE
        data = [b"id: 1\n\n", b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
        return _Resp(data)

    async def fake_ensure(self):  # return object with post()
        class S:
            def post(self, *a, **k):
                return fake_post()

        return S()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure)

    lines: list[str] = []
    async for line in client.stream_post("http://example/sse", json={"stream": True}, mode="sse"):
        lines.append(line)

    assert lines[0].startswith("data: ")
    assert lines[-1] == "data: [DONE]"
    # JSON payload is preserved without trailing newlines
    assert json.loads(lines[0][6:]) == {"x": 1}


class _FakeResp:
    """Minimal aiohttp ClientResponse stand-in for stream_post tests."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    async def iter_chunked(self, _n: int):
        for c in self._chunks:
            yield c


class _FakeCM:
    """Fake ``session.post(...)`` context manager.

    Behavior is parameterized per-call by the ``script`` callable, which the
    fake session advances each time ``post()`` is called. On ``__aenter__``,
    the script either raises ``ServerDisconnectedError`` (to model a stale
    pooled socket) or returns a ``_FakeResp``.
    """

    def __init__(self, behavior):
        # behavior is a connect-error token (see _connect_phase_error) or a
        # list[bytes] of body chunks for a successful open.
        self._behavior = behavior
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        if isinstance(self._behavior, str):
            raise _connect_phase_error(self._behavior)
        return _FakeResp(self._behavior)

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


def _connect_phase_error(token: str) -> BaseException:
    """Build a connect-phase exception a stale/failed socket can raise.

    aiohttp wraps socket errors (ECONNRESET, EPIPE) in ``ClientOSError``;
    ``connect_error`` is a genuine fresh-connection failure that must NOT
    retry. ``ClientConnectorError`` has a version-specific constructor, so
    fall back to ``__new__`` when the simple form isn't accepted (keeps the
    test robust whether ``aiohttp`` is the real package or the unit stub).
    """
    if token == "disconnect":
        return aiohttp.ServerDisconnectedError()
    if token == "reset":
        return aiohttp.ClientOSError(104, "Connection reset by peer")
    if token == "broken_pipe":
        return aiohttp.ClientOSError(32, "Broken pipe")
    if token == "connect_error":
        cls = aiohttp.ClientConnectorError
        try:
            return cls("Connection refused")
        except TypeError:
            return cls.__new__(cls)
    raise ValueError(f"unknown connect-phase token: {token!r}")


def _fake_session_with_script(script: list):
    """Build a fake session whose ``post()`` consumes ``script`` in order."""
    cms: list[_FakeCM] = []

    class _S:
        def post(self, *_a, **_k):
            cm = _FakeCM(script.pop(0))
            cms.append(cm)
            return cm

    async def _ensure(_self):
        return _S()

    return _ensure, cms


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_retries_once_on_stale_keepalive(monkeypatch):
    """First post() returns a CM whose __aenter__ raises (stale pooled
    socket); retry on a fresh CM succeeds and the consumer gets a clean
    stream with no duplicates."""
    client = AsyncHTTPClient.shared()
    body = [b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
    ensure, cms = _fake_session_with_script(["disconnect", body])
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    lines: list[str] = []
    async for line in client.stream_post("http://example/sse", json={}, mode="sse"):
        lines.append(line)

    assert len(cms) == 2
    assert cms[0].entered is True  # __aexit__ is not called when __aenter__ raises
    assert cms[1].entered is True and cms[1].exited is True
    assert lines == ['data: {"x":1}', "data: [DONE]"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reset", "broken_pipe"])
async def test_stream_post_retries_once_on_stale_socket_reset(monkeypatch, failure):
    """A stale pooled socket the upstream reset (ECONNRESET) or that we wrote
    into after a half-close (EPIPE) surfaces as ClientOSError, not a clean
    ServerDisconnectedError. It happens in the connect phase (before any
    response byte), so it must retry once on a fresh connection — mirroring
    the ServerDisconnectedError path — instead of surfacing a spurious 502."""
    client = AsyncHTTPClient.shared()
    body = [b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
    ensure, cms = _fake_session_with_script([failure, body])
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    lines: list[str] = []
    async for line in client.stream_post("http://example/sse", json={}, mode="sse"):
        lines.append(line)

    assert len(cms) == 2  # first attempt failed, retry succeeded
    assert cms[1].entered is True and cms[1].exited is True
    assert lines == ['data: {"x":1}', "data: [DONE]"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_does_not_retry_on_connect_error(monkeypatch):
    """A ClientConnectorError (DNS / refused / TLS on a *fresh* connection) is
    a real connectivity failure, not a stale pooled socket — even though it
    subclasses ClientOSError. It must propagate on the first attempt with no
    retry, so a real upstream outage surfaces promptly."""
    client = AsyncHTTPClient.shared()
    ensure, cms = _fake_session_with_script(["connect_error", "connect_error"])
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    with pytest.raises(aiohttp.ClientConnectorError):
        async for _ in client.stream_post("http://example/sse", json={}, mode="sse"):
            pass

    assert len(cms) == 1  # no retry on a fresh-connection failure


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_gives_up_after_two_consecutive_resets(monkeypatch):
    """If both connect attempts hit a reset, the second error propagates."""
    client = AsyncHTTPClient.shared()
    ensure, cms = _fake_session_with_script(["reset", "reset"])
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    with pytest.raises(aiohttp.ClientOSError):
        async for _ in client.stream_post("http://example/sse", json={}, mode="sse"):
            pass

    assert len(cms) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_does_not_retry_after_aenter_succeeds(monkeypatch):
    """Once __aenter__ returns, the upstream has begun responding. A failure
    while reading the body must NOT trigger a retry, even if no chunk has
    yet been yielded to the caller (e.g. SSE comments/keepalives that the
    parser silently consumes)."""
    client = AsyncHTTPClient.shared()

    class _BadResp(_FakeResp):
        async def iter_chunked(self, _n: int):
            # SSE comment line — parser consumes it without emitting anything.
            yield b": keepalive\n\n"
            raise aiohttp.ServerDisconnectedError()

    class _CM:
        def __init__(self):
            self.entered = False
            self.exited = False

        async def __aenter__(self):
            self.entered = True
            return _BadResp([])

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True
            return False

    cms: list[_CM] = []

    class _S:
        def post(self, *_a, **_k):
            cm = _CM()
            cms.append(cm)
            return cm

    async def ensure(_self):
        return _S()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    received: list[str] = []
    with pytest.raises(aiohttp.ServerDisconnectedError):
        async for line in client.stream_post("http://example/sse", json={}, mode="sse"):
            received.append(line)

    assert len(cms) == 1  # exactly one connect attempt — no retry
    assert cms[0].entered is True and cms[0].exited is True
    assert received == []  # parser consumed the comment, nothing reached caller


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_gives_up_after_two_consecutive_disconnects(monkeypatch):
    """If both connect attempts hit a stale socket, the second error propagates."""
    client = AsyncHTTPClient.shared()
    ensure, cms = _fake_session_with_script(["disconnect", "disconnect"])
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure)

    with pytest.raises(aiohttp.ServerDisconnectedError):
        async for _ in client.stream_post("http://example/sse", json={}, mode="sse"):
            pass

    assert len(cms) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_post_ndjson(monkeypatch):
    client = AsyncHTTPClient.shared()

    class _Resp:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks
            self.content = self
            self.headers = {}
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def iter_chunked(self, n: int):
            for c in self._chunks:
                yield c

        def raise_for_status(self):
            return None

    def fake_post(*args, **kwargs):
        # Two NDJSON lines split across chunks + trailing partial flush
        data = [b'{"a":1}\n{"b":2}', b"\n  ", b"\n"]
        return _Resp(data)

    async def fake_ensure(self):
        class S:
            def post(self, *a, **k):
                return fake_post()

        return S()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure)

    lines = []
    async for line in client.stream_post("http://example/ndjson", json={}, mode="ndjson"):
        lines.append(line)

    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]
