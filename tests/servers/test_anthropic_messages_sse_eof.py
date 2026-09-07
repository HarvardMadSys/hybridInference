"""End-of-body handling for the /v1/messages streaming surface.

This surface has its own SSE splitter (``_parse_sse_chunk``) and never goes
through ``stream_post``, so the parser flush added for the chat path cannot
reach it. Its splitter only emits an event once it sees the blank line SSE
delimits events with, and the residue it hands back as ``sse_buffer`` was
discarded when the reader hit end of body. An upstream that closed straight
after its last event therefore lost it -- and that event is ``message_delta`` /
``message_stop``, carrying ``stop_reason`` and the output-token total. When it
also held the only content-bearing delta, the request produced no success
record either, so a completed stream gave the endpoint zero success credit.
"""

from __future__ import annotations

import asyncio

import pytest

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
    adapter, _weight = router.routes[NATIVE_MODEL].adapters[0]
    return endpoint_id_for_adapter(adapter)


@pytest.fixture
def quiet_alerts(monkeypatch):
    from unittest.mock import AsyncMock

    from serving.observability import alerts

    monkeypatch.setattr(alerts, "alert_slack", AsyncMock())
    yield


@pytest.fixture
def captured_log(monkeypatch):
    """Capture what would have been persisted, so usage is inspectable."""
    captured: dict = {}
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kwargs: captured.update(kwargs)
    )
    return captured


def _fake_session_from_iter(iter_factory):
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
    b'data: {"type":"message_start","message":{"id":"msg_eof","model":"claude-opus-4-7",'
    b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
)
_TEXT_DELTA = (
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
# A terminal event deliberately missing SSE's trailing blank line.
_UNTERMINATED_MESSAGE_DELTA = (
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":7}}\n'
)


async def _drain(client, body=None):
    chunks: list[bytes] = []
    async with client.stream(
        "POST", "/v1/messages", json=body or _body(stream=True), headers=_auth()
    ) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_unterminated_terminal_event_is_still_accounted(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, captured_log
):
    """``message_delta``'s output tokens survive a body that ends without a blank line."""

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA
        yield _UNTERMINATED_MESSAGE_DELTA

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    await _drain(anthropic_test_client)
    await asyncio.sleep(0)

    # Both halves of what the dropped event carried.
    assert captured_log["usage"]["output_tokens"] == 7
    assert captured_log["response"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_client_bytes_are_unchanged_by_the_drain(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, captured_log
):
    """Completing the frame is our accounting's business, not the wire's."""
    body_bytes = _MESSAGE_START + _TEXT_DELTA + _UNTERMINATED_MESSAGE_DELTA

    async def _iter():
        yield body_bytes

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    out = await _drain(anthropic_test_client)
    await asyncio.sleep(0)

    assert out == body_bytes


@pytest.mark.asyncio
async def test_success_is_recorded_when_the_only_delta_is_unterminated(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, captured_log
):
    """A completed stream must not give the endpoint zero success credit.

    The content-bearing delta is the last event and carries no trailing blank
    line, so it used to be dropped -- ``_is_non_empty_content_event`` never saw
    it and the surface recorded no success, leaving this endpoint's availability
    to be driven only by its failures.
    """
    registry = anthropic_compat_router.endpoint_health_registry
    calls: list[str] = []
    monkeypatch.setattr(registry, "record_success", lambda endpoint_id: calls.append(endpoint_id))

    async def _iter():
        yield _MESSAGE_START
        yield _TEXT_DELTA.rstrip(b"\n")  # the sole delta, unterminated

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    await _drain(anthropic_test_client)
    await asyncio.sleep(0)

    assert calls == [_endpoint_id(anthropic_compat_router)]


@pytest.mark.asyncio
async def test_properly_terminated_stream_records_success_once(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, captured_log
):
    """The drain must not double-count a stream that ended correctly."""
    registry = anthropic_compat_router.endpoint_health_registry
    calls: list[str] = []
    monkeypatch.setattr(registry, "record_success", lambda endpoint_id: calls.append(endpoint_id))

    async def _iter():
        yield _MESSAGE_START + _TEXT_DELTA + _UNTERMINATED_MESSAGE_DELTA + b"\n"

    from serving.http import AsyncHTTPClient

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", _fake_session_from_iter(_iter))

    await _drain(anthropic_test_client)
    await asyncio.sleep(0)

    assert calls == [_endpoint_id(anthropic_compat_router)]
    assert captured_log["usage"]["output_tokens"] == 7
