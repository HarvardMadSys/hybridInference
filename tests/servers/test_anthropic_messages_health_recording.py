"""Endpoint-health recording contracts for the Anthropic Messages surface.

/v1/messages picks its own adapter and dispatches directly, so nothing in
FixedRouter observes the outcome. Until this surface recorded its own, an
endpoint served only through /v1/messages -- the bulk of Claude Code traffic --
could fail every request without ever opening the breaker that the chat path
reads on the same ``endpoint_id``.

Companion to test_anthropic_messages_provider_admission.py, which covers the
read side (which adapter the surface is allowed to pick).
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from routing.endpoint_health import _CircuitState
from routing.endpoints import endpoint_id_for_adapter

NATIVE_MODEL = "claude-opus-4.7"


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _body(**overrides):
    body = {
        "model": NATIVE_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


def _endpoint_id(router):
    """Return the endpoint id the surface will dispatch NATIVE_MODEL to."""
    adapter, _weight = router.routes[NATIVE_MODEL].adapters[0]
    return endpoint_id_for_adapter(adapter)


@pytest.fixture
def quiet_alerts(monkeypatch):
    """Keep a tripped breaker from trying to page Slack during a test."""
    from unittest.mock import AsyncMock

    from serving.observability import alerts

    monkeypatch.setattr(alerts, "alert_slack", AsyncMock())
    yield


@pytest.fixture
def no_log_store(monkeypatch):
    """Swap DB logging for an in-memory capture so tests stay unit-fast."""
    captured: dict = {}
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kwargs: captured.update(kwargs)
    )
    return captured


def _fake_session_from_iter(iter_factory):
    """Monkeypatch target for AsyncHTTPClient._ensure_session driving an SSE body."""

    class _FakeContent:
        def iter_any(self):
            return iter_factory()

    class _FakeResp:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp()

    async def fake_ensure_session(self):
        return _FakeSession()

    return fake_ensure_session


_MESSAGE_START = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_h","model":"claude-opus-4-7",'
    b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
)
_TEXT_DELTA = (
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
_MESSAGE_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


# --- non-streaming ---------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_success_closes_an_open_circuit(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """A completed non-streaming response must report success to the registry.

    Asserted through the circuit rather than a spy: a breaker left open by the
    chat path is exactly what this surface's success has to be able to close.
    Zero cooldown so the seeded-open circuit admits the request as a half-open
    probe -- ``_pick_dispatch_adapter`` refuses an endpoint still in its
    cooldown, so a probe is the only way a success ever reaches an open circuit.
    """
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)
    for _ in range(5):
        registry.record_failure(endpoint_id, reason="seeded")
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN

    upstream_resp = {
        "id": "msg_ok",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        return upstream_resp

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert r.status_code == 200

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_non_streaming_key_pool_exhausted_opens_the_circuit(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """KeyPoolExhausted on this surface must trip the breaker.

    It carries no HTTP status, so the registry's client-error exemption never
    applies -- every key is muted and nothing sent here can succeed.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "300")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    from serving.adapters.key_pool import KeyPoolExhausted

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise KeyPoolExhausted("all keys muted")

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    for _ in range(2):
        r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
        assert r.status_code == 429

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    assert registry.allow_request(endpoint_id) is False
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_non_streaming_client_error_leaves_the_circuit_closed(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """An upstream 400 is one caller's bad request, not an endpoint outage.

    The exception is handed to ``record_failure`` as ``exc=`` precisely so the
    registry can apply that exemption; passing only a detail string would let a
    single malformed request open the circuit for every other caller.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise aiohttp.ClientResponseError(
            request_info=None, history=None, status=400, message="bad request"
        )

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    for _ in range(4):
        r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
        assert r.status_code == 400

    snapshot = registry.snapshot()[endpoint_id]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    # Exempted before the EWMA update, so availability is untouched too.
    assert snapshot["availability"] == 1.0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_non_streaming_upstream_5xx_records_failure(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """A 5xx is an endpoint fault and must degrade its recorded availability."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        raise aiohttp.ClientResponseError(
            request_info=None, history=None, status=502, message="bad gateway"
        )

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert r.status_code == 502

    assert registry.snapshot()[endpoint_id]["availability"] < 1.0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_non_streaming_registers_endpoint_before_dispatch(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """The endpoint must appear in the health snapshot even with no outcome yet."""
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)
    assert registry.snapshot() == {}

    seen: dict = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        seen["snapshot"] = registry.snapshot()
        raise ConnectionError("network gone")

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert endpoint_id in seen["snapshot"]
    await asyncio.sleep(0)


# --- streaming -------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_first_content_delta_closes_an_open_circuit(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """First non-empty content delta is the stream's success signal.

    Mirrors FixedRouter.stream_chat_completion, which treats the first chunk
    carrying content -- not the accepted connection -- as proof of health.
    Zero cooldown so the seeded-open circuit admits this as a half-open probe.
    """
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)
    for _ in range(5):
        registry.record_failure(endpoint_id, reason="seeded")
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA + _MESSAGE_STOP

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for _ in r.aiter_bytes():
            pass

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_streaming_success_recorded_once_per_stream(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """Many content deltas must not inflate the endpoint's success count."""
    registry = anthropic_compat_router.endpoint_health_registry
    calls: list[str] = []
    monkeypatch.setattr(registry, "record_success", lambda endpoint_id: calls.append(endpoint_id))

    async def _iter():
        yield _MESSAGE_START
        yield _TEXT_DELTA
        yield _TEXT_DELTA
        yield _MESSAGE_STOP

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
    ) as r:
        async for _ in r.aiter_bytes():
            pass

    assert calls == [_endpoint_id(anthropic_compat_router)]


@pytest.mark.asyncio
async def test_streaming_without_content_records_no_success(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """A well-formed stream that produced nothing is not evidence of health.

    Same disposition as the chat path: it is not a failure either, so nothing is
    recorded at all and an open circuit stays open.
    """
    registry = anthropic_compat_router.endpoint_health_registry
    success_calls: list[str] = []
    failure_calls: list[str] = []
    monkeypatch.setattr(registry, "record_success", lambda eid: success_calls.append(eid))
    monkeypatch.setattr(registry, "record_failure", lambda eid, **kw: failure_calls.append(eid))

    empty_delta = (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":""}}\n\n'
    )

    async def _iter():
        yield _MESSAGE_START + empty_delta + _MESSAGE_STOP

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    async with anthropic_test_client.stream(
        "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
    ) as r:
        async for _ in r.aiter_bytes():
            pass

    assert success_calls == []
    assert failure_calls == []


@pytest.mark.asyncio
async def test_streaming_upstream_exception_opens_the_circuit(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """An upstream that dies mid-dispatch must trip the breaker."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "300")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    async def _iter():
        raise RuntimeError("upstream exploded")
        yield b""  # make this an async generator

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    for _ in range(2):
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
        ) as r:
            assert r.status_code == 200  # SSE error event, not an HTTP status
            async for _ in r.aiter_bytes():
                pass

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_streaming_client_error_leaves_the_circuit_closed(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """An upstream 400 mid-stream is exempt here too, via ``exc=``."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    async def _iter():
        raise aiohttp.ClientResponseError(
            request_info=None, history=None, status=400, message="bad request"
        )
        yield b""

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    for _ in range(4):
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
        ) as r:
            async for _ in r.aiter_bytes():
                pass

    snapshot = registry.snapshot()[endpoint_id]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    assert snapshot["availability"] == 1.0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_streaming_idle_timeout_records_failure(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """An upstream silent for the whole ceiling is a fault, not a quiet success.

    It arrives as a timer rather than an exception, so it needs its own record
    call; without one the endpoint that stalls every stream never trips.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "300")
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)

    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(amod, "_KEEPALIVE_INTERVAL", 0.02)
    monkeypatch.setattr(amod, "_MAX_STREAM_IDLE", 0.08)

    async def _iter():
        await asyncio.sleep(5)  # effectively never within the test window
        yield b""

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    for _ in range(2):
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
        ) as r:
            async for _ in r.aiter_bytes():
                pass

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    await asyncio.sleep(0)


# --- client disconnect must never be charged to the upstream ---------------


@pytest.mark.asyncio
async def test_streaming_client_disconnect_is_not_an_upstream_failure(
    anthropic_test_app, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """Cancelling the server task mid-stream must not degrade endpoint health.

    This is the real shape of a client disconnect: the ASGI task is cancelled
    while the generator awaits the next chunk, raising CancelledError inside it.
    Recording that would let flaky client networks open a circuit every other
    caller -- including the chat path -- then routes around.

    Driven over raw ASGI rather than the httpx test client, which buffers the
    whole response and so can never be cancelled mid-stream.
    """
    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)
    failure_calls: list[str] = []
    success_calls: list[str] = []
    monkeypatch.setattr(registry, "record_failure", lambda eid, **kw: failure_calls.append(eid))
    monkeypatch.setattr(registry, "record_success", lambda eid: success_calls.append(eid))

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA
        await asyncio.sleep(30)  # upstream still open; the client gives up first

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    payload = json.dumps(_body(stream=True)).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"x-api-key", _auth()["x-api-key"].encode()),
        ],
        "client": ("127.0.0.1", 45678),
        "server": ("test", 80),
    }

    disconnected = asyncio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        # Starlette's StreamingResponse races the body against this; returning
        # http.disconnect is what cancels the generator mid-stream.
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"content_block_delta" in message.get(
            "body", b""
        ):
            disconnected.set()

    await asyncio.wait_for(anthropic_test_app(scope, receive, send), timeout=10)

    # The delivered content still counted as a success -- and proves the stream
    # really was cut mid-flight rather than never starting.
    assert success_calls == [endpoint_id]
    assert failure_calls == []


@pytest.mark.asyncio
async def test_streaming_upstream_cancellation_is_not_recorded(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """A CancelledError raised by the upstream iterator is not a failure either."""
    registry = anthropic_compat_router.endpoint_health_registry
    failure_calls: list[str] = []
    monkeypatch.setattr(registry, "record_failure", lambda eid, **kw: failure_calls.append(eid))

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA
        raise asyncio.CancelledError()

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    try:
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=_body(stream=True), headers=_auth()
        ) as r:
            async for _ in r.aiter_bytes():
                pass
    except Exception:
        # The client may observe the truncated stream as a transport error; the
        # server-side health accounting is what this asserts on.
        pass

    assert failure_calls == []


# --- helper unit -----------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "data", "expected"),
    [
        ("content_block_delta", '{"delta":{"type":"text_delta","text":"hi"}}', True),
        ("content_block_delta", '{"delta":{"type":"thinking_delta","thinking":"hm"}}', True),
        ("content_block_delta", '{"delta":{"type":"input_json_delta","partial_json":"{"}}', True),
        ("content_block_delta", '{"delta":{"type":"text_delta","text":""}}', False),
        ("content_block_delta", '{"delta":{}}', False),
        ("content_block_delta", "not json", False),
        ("content_block_delta", "[1, 2]", False),
        ("message_start", '{"message":{"content":[]}}', False),
        ("ping", "{}", False),
        ("", "", False),
    ],
)
def test_is_non_empty_content_event(event_type, data, expected):
    """Only a delta that actually produced output counts as content."""
    from serving.servers.routers.anthropic_messages import _is_non_empty_content_event

    assert _is_non_empty_content_event(event_type, data) is expected
