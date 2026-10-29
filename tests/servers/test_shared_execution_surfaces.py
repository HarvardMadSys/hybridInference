"""Cross-surface behaviour the shared protocol execution must preserve (issue #1690).

Characterisation written against the handlers as they were before the
extraction, kept green through it: the online surfaces keep counting a
streamed request on its concrete model until the stream ends, the Responses
API takes one user slot and is routed, billed and logged once through the chat
path, a Messages stream refused before its first byte is refused with an HTTP
status, header lookups stay case-insensitive (first value wins on a repeated
header), and the bytes of a stream are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import UserConcurrencyLimiter
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions, embeddings, responses
from serving.servers.routers.completions_cost import CostTracker
from serving.servers.routers.completions_logging import CompletionsLogger

from .test_auto_mode_surfaces import (
    MID,
    _anthropic,
    _capture_interactive_load,
    _chat,
    _Gateway,
    _NativeRecorder,
)

INTERNAL = {"user_id": "user-1", "role": "internal", "authenticated": True, "is_admin": False}
ADMIN = {**INTERNAL, "is_admin": True}


@pytest.fixture
def gateway(mock_db_logger, mock_log_store) -> _Gateway:
    return _Gateway(mock_db_logger, mock_log_store)


async def _rows(log_store, count: int = 1, timeout: float = 2.0) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while log_store.log_request.call_count < count:
        if loop.time() > deadline:
            raise AssertionError(f"{log_store.log_request.call_count} of {count} rows logged")
        await asyncio.sleep(0.01)
    # Let any stray extra row land before the caller counts them.
    for _ in range(20):
        await asyncio.sleep(0)
    return [call.kwargs for call in log_store.log_request.call_args_list]


# -- streamed requests count on the concrete model until EOF ---------------------


def _hold_stream(gateway: _Gateway, monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = gateway.adapters[MID]

    async def held_stream(messages, **params):
        adapter._record(messages, params)
        entered.set()
        yield adapter.format_stream_chunk(model=MID, content="ok")
        await release.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(adapter, "stream_chat_completion", held_stream)
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/v1/messages", _anthropic("auto-balance", stream=True), id="messages"),
        pytest.param(
            "/v1/responses",
            {"model": "auto-balance", "input": "hi", "stream": True},
            id="responses",
        ),
        pytest.param("/completion", _chat("auto-balance", stream=True), id="compat"),
    ],
)
async def test_a_streamed_auto_request_counts_on_the_concrete_model_until_eof(
    gateway, monkeypatch, path, body
):
    load = _capture_interactive_load(gateway, monkeypatch)
    entered, release = _hold_stream(gateway, monkeypatch)

    task = asyncio.create_task(gateway.post(path, body))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        assert load.in_flight(MID) == 1
        assert load.in_flight("auto-balance") == 0
    finally:
        release.set()

    assert (await task).status_code == 200
    assert load.in_flight(MID) == 0


# -- the Responses API delegates to chat exactly once -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
async def test_a_responses_request_takes_one_slot_and_is_billed_logged_and_observed_once(
    gateway, monkeypatch, stream
):
    _capture_interactive_load(gateway, monkeypatch)
    limiter = gateway.app.state.services.user_concurrency_limiter
    acquired: list[str] = []
    real_acquire = limiter.try_acquire

    async def counting_acquire(slot_key, *args, **kwargs):
        acquired.append(slot_key)
        return await real_acquire(slot_key, *args, **kwargs)

    monkeypatch.setattr(limiter, "try_acquire", counting_acquire)
    charges: list[dict[str, Any]] = []
    real_charge = CostTracker.schedule_increment

    async def counting_charge(self, **kwargs):
        charges.append(kwargs)
        return await real_charge(self, **kwargs)

    monkeypatch.setattr(CostTracker, "schedule_increment", counting_charge)
    observed: list[str] = []
    real_observe = CompletionsLogger.record_routing_observation

    def counting_observe(self, router, model, *args, **kwargs):
        observed.append(model)
        return real_observe(self, router, model, *args, **kwargs)

    monkeypatch.setattr(CompletionsLogger, "record_routing_observation", counting_observe)

    response = await gateway.post(
        "/v1/responses", {"model": MID, "input": "hi", "stream": stream, "store": False}
    )

    assert response.status_code == 200, response.text
    rows = await _rows(gateway.log_store)
    assert len(rows) == 1
    assert rows[0]["model_id"] == MID
    assert acquired == ["user-1"]
    if not stream:
        # The recorder reports no usage on a unary answer, so nothing is
        # charged; the stream estimates usage, and charges once.
        assert charges == []
    else:
        assert len(charges) == 1
    assert observed == [MID]
    assert isinstance(limiter, UserConcurrencyLimiter)


# -- a Messages stream refused before its first byte is an HTTP status -------------


@pytest.mark.asyncio
async def test_a_messages_stream_for_an_unknown_model_is_a_404_not_a_stream(gateway):
    response = await gateway.post("/v1/messages", _anthropic("no-such-model", stream=True))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_a_messages_stream_with_every_provider_down_is_a_503_not_a_stream(gateway):
    gateway.down(MID)

    response = await gateway.post("/v1/messages", _anthropic(MID, stream=True))

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["type"] == "overloaded_error"


# -- header lookups: case-insensitive, first value of a repeated header -------------


_SESSION_VARIANTS = [
    pytest.param([("X-Session-ID", "s-1")], id="canonical"),
    pytest.param([("x-session-id", "s-1")], id="lower"),
    pytest.param([("X-SESSION-ID", "s-1")], id="upper"),
    pytest.param([("x-SeSsIoN-iD", "s-1"), ("X-Session-Id", "s-2")], id="repeated"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _SESSION_VARIANTS)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/v1/chat/completions", _chat(MID), id="chat"),
        pytest.param("/v1/messages", _anthropic(MID), id="messages"),
        pytest.param(
            "/v1/responses", {"model": MID, "input": "hi", "store": False}, id="responses"
        ),
        pytest.param("/completion", _chat(MID), id="compat"),
    ],
)
async def test_the_session_header_is_read_in_any_case_and_first_value_wins(
    gateway, path, body, headers
):
    transport = ASGITransport(app=gateway.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path, json=body, headers=headers)

    assert response.status_code == 200, response.text
    row = (await _rows(gateway.log_store))[0]
    assert row["metadata"]["session_id"] == "s-1"
    assert row["metadata"]["session_id_source"] == "x-session-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["X-Route-Pin", "x-route-pin", "X-ROUTE-PIN"])
async def test_an_admin_route_pin_is_honoured_in_any_case(mock_db_logger, mock_log_store, name):
    gateway = _Gateway(mock_db_logger, mock_log_store, user=ADMIN)

    response = await gateway.post("/v1/chat/completions", _chat(MID), headers={name: "nowhere"})

    assert response.status_code == 400
    assert "Pinned provider 'nowhere' not found" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["X-Route-Pin", "x-route-pin", "X-ROUTE-PIN"])
async def test_a_route_pin_from_a_non_admin_is_ignored_in_any_case(gateway, name):
    response = await gateway.post("/v1/chat/completions", _chat(MID), headers={name: "nowhere"})

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Probe": "synthetic"}, id="canonical"),
        pytest.param({"x-probe": "SYNTHETIC"}, id="lower-name-upper-value"),
        pytest.param({"X-PROBE": "Synthetic"}, id="upper"),
    ],
)
async def test_a_trusted_probe_marker_is_read_in_any_case(mock_db_logger, mock_log_store, headers):
    gateway = _Gateway(mock_db_logger, mock_log_store, user=INTERNAL)

    response = await gateway.post("/v1/chat/completions", _chat(MID), headers=headers)

    assert response.status_code == 200, response.text
    # A trusted probe on a single-backend route is told which provider served.
    assert response.headers["x-provider"] == f"provider-{MID}"
    # And, with probe logging off, it writes no request log row.
    for _ in range(50):
        await asyncio.sleep(0)
    assert mock_log_store.log_request.call_count == 0


class _BetaRecorder(_NativeRecorder):
    def __init__(self, config: ModelConfig, served: list[str]) -> None:
        super().__init__(config, served)
        self.extra_headers: list[dict[str, str] | None] = []

    async def messages(self, body, *, request_id, extra_headers=None) -> dict[str, Any]:
        self.extra_headers.append(extra_headers)
        return await super().messages(body, request_id=request_id, extra_headers=extra_headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "forwarded"),
    [
        pytest.param([("anthropic-beta", "b-1")], "b-1", id="lower"),
        pytest.param([("Anthropic-Beta", "b-1")], "b-1", id="title"),
        pytest.param([("ANTHROPIC-BETA", "b-1")], "b-1", id="upper"),
        pytest.param([("Anthropic-Beta", "b-1"), ("anthropic-beta", "b-2")], "b-1", id="repeated"),
    ],
)
async def test_the_anthropic_beta_header_is_forwarded_in_any_case(
    mock_db_logger, mock_log_store, headers, forwarded
):
    gateway = _Gateway(mock_db_logger, mock_log_store)
    native = _BetaRecorder(gateway.adapters[MID].config, [])
    native.native_format = "anthropic"
    gateway.router.register_route("native-model", [(native, 1.0)])

    transport = ASGITransport(app=gateway.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/messages", json=_anthropic("native-model"), headers=headers
        )

    assert response.status_code == 200, response.text
    assert native.extra_headers == [{"anthropic-beta": forwarded}]


# -- the Responses API strips every spelling of X-Reasoning-Passthrough ---------------


class _ReasoningAdapter(BaseAdapter):
    async def chat_completion(self, messages, **params):
        return {
            "id": "c",
            "object": "chat.completion",
            "created": 1,
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "reasoning_content": "deliberation",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        if False:
            yield ""


class _Store:
    persist_enabled = True

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    async def save(self, *, response_id, user_id, response, messages, **_: Any) -> None:
        self.saved[response_id] = {"user_id": user_id, "messages": messages}

    async def get(self, response_id):
        return self.saved.get(response_id)


def _responses_app(mock_db_logger, mock_log_store) -> tuple[FastAPI, _Store]:
    router = FixedRouter()
    config = ModelConfig(
        id="reasoning-model", name="reasoning-model", provider="test", base_url="http://test"
    )
    router.register_route("reasoning-model", [(_ReasoningAdapter(config), 1.0)])
    store = _Store()
    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
        responses_store=store,
    )
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: INTERNAL
    app.include_router(responses.router)
    return app, store


async def _raw_post(app: FastAPI, path: str, body: dict[str, Any], raw_headers) -> tuple[int, Any]:
    """POST through ASGI with header names exactly as given (no client lower-casing)."""
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            *raw_headers,
        ],
        "client": ("127.0.0.1", 5000),
        "server": ("test", 80),
    }
    sent = False
    status: list[int] = []
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.sleep(3600)
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status[0], json.loads(b"".join(chunks))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_headers",
    [
        pytest.param([(b"x-reasoning-passthrough", b"false")], id="lower"),
        pytest.param([(b"X-Reasoning-Passthrough", b"false")], id="title-raw"),
        pytest.param([(b"X-REASONING-PASSTHROUGH", b"0")], id="upper-raw"),
        pytest.param(
            [(b"x-reasoning-passthrough", b"no"), (b"X-Reasoning-PassThrough", b"false")],
            id="repeated-mixed",
        ),
    ],
)
async def test_the_responses_api_strips_every_spelling_of_reasoning_passthrough(
    mock_db_logger, mock_log_store, raw_headers
):
    app, store = _responses_app(mock_db_logger, mock_log_store)

    status, body = await _raw_post(
        app, "/v1/responses", {"model": "reasoning-model", "input": "hi"}, raw_headers
    )

    assert status == 200, body
    assert store.saved[body["id"]]["messages"][-1]["reasoning_content"] == "deliberation"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no"])
async def test_chat_still_honours_strict_reasoning_in_any_header_case(
    mock_db_logger, mock_log_store, value
):
    app, _store = _responses_app(mock_db_logger, mock_log_store)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "reasoning-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-REASONING-PASSTHROUGH": value},
        )

    assert response.status_code == 200, response.text
    assert "reasoning_content" not in response.json()["choices"][0]["message"]


# -- embeddings: the session header, in any case -----------------------------------


class _Embedder:
    def __init__(self) -> None:
        self.config = ModelConfig(id="emb", name="emb", provider="test", base_url="http://e")

    async def embeddings(self, inputs, **params):
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}],
            "model": "emb",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _SESSION_VARIANTS)
async def test_the_embeddings_session_header_is_read_in_any_case(
    mock_db_logger, mock_log_store, headers
):
    embedder = _Embedder()
    router = FixedRouter()
    router.register_embedding_route("emb", [(embedder, 1.0)], aliases=[], required_role="free")
    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
        embedding_adapters={"emb": embedder},
    )
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: INTERNAL
    app.include_router(embeddings.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/embeddings", json={"model": "emb", "input": "x"}, headers=headers
        )

    assert response.status_code == 200, response.text
    row = (await _rows(mock_log_store))[0]
    assert row["metadata"]["session_id"] == "s-1"


# -- stream bytes ---------------------------------------------------------------------


_IDS = re.compile(r'"(id|created|created_at|item_id|response_id|completion_id)":\s*("[^"]*"|\d+)')


def _normalized(text: str) -> str:
    return _IDS.sub(lambda m: f'"{m.group(1)}":"<{m.group(1)}>"', text)


async def _stream_text(gateway: _Gateway, path: str, body: dict[str, Any]) -> tuple[int, str, Any]:
    transport = ASGITransport(app=gateway.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path, json=body)
    return response.status_code, _normalized(response.text), response.headers


# The streams as the handlers wrote them before the extraction, ids and
# timestamps normalised. Byte-for-byte otherwise: spacing, key order, frames.
_CHAT_SSE = (
    'data: {"id":"<id>", "object": "chat.completion.chunk", "created":"<created>", "model": "mid-b", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}\n\n'
    'data: {"id":"<id>", "object": "chat.completion.chunk", "created":"<created>", "model": "mid-b", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": null}]}\n\n'
    "data: [DONE]\n\n"
)

_MESSAGES_SSE = (
    'event: message_start\ndata: {"type": "message_start", "message": {"id":"<id>", "type": "message", "role": "assistant", "model": "mid-b", "content": [], "stop_reason": null, "stop_sequence": null, "usage": {"input_tokens": 9, "output_tokens": 0}}}\n\n'
    'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}\n\n'
    'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}\n\n'
    'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
    'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null}, "usage": {"input_tokens": 9, "output_tokens": 0}}\n\n'
    'event: message_stop\ndata: {"type": "message_stop"}\n\n'
)

_RESPONSES_SSE = (
    'event: response.created\ndata: {"type": "response.created", "sequence_number": 0, "response": {"id":"<id>", "object": "response", "created_at":"<created_at>", "status": "in_progress", "error": null, "incomplete_details": null, "model": "mid-b", "output": [], "previous_response_id": null, "store": false, "instructions": null, "max_output_tokens": null, "temperature": null, "top_p": null, "tools": [], "tool_choice": "auto", "parallel_tool_calls": true, "text": {"format": {"type": "text"}}, "reasoning": {"effort": null, "summary": null}, "metadata": {}, "truncation": "disabled"}}\n\n'
    'event: response.in_progress\ndata: {"type": "response.in_progress", "sequence_number": 1, "response": {"id":"<id>", "object": "response", "created_at":"<created_at>", "status": "in_progress", "error": null, "incomplete_details": null, "model": "mid-b", "output": [], "previous_response_id": null, "store": false, "instructions": null, "max_output_tokens": null, "temperature": null, "top_p": null, "tools": [], "tool_choice": "auto", "parallel_tool_calls": true, "text": {"format": {"type": "text"}}, "reasoning": {"effort": null, "summary": null}, "metadata": {}, "truncation": "disabled"}}\n\n'
    'event: response.output_item.added\ndata: {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0, "item": {"type": "message", "id":"<id>", "status": "in_progress", "role": "assistant", "content": []}}\n\n'
    'event: response.content_part.added\ndata: {"type": "response.content_part.added", "sequence_number": 3, "item_id":"<item_id>", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}\n\n'
    'event: response.output_text.delta\ndata: {"type": "response.output_text.delta", "sequence_number": 4, "item_id":"<item_id>", "output_index": 0, "content_index": 0, "delta": "ok"}\n\n'
    'event: response.output_text.done\ndata: {"type": "response.output_text.done", "sequence_number": 5, "item_id":"<item_id>", "output_index": 0, "content_index": 0, "text": "ok"}\n\n'
    'event: response.content_part.done\ndata: {"type": "response.content_part.done", "sequence_number": 6, "item_id":"<item_id>", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "ok", "annotations": []}}\n\n'
    'event: response.output_item.done\ndata: {"type": "response.output_item.done", "sequence_number": 7, "output_index": 0, "item": {"type": "message", "id":"<id>", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}}\n\n'
    'event: response.completed\ndata: {"type": "response.completed", "sequence_number": 8, "response": {"id":"<id>", "object": "response", "created_at":"<created_at>", "status": "completed", "error": null, "incomplete_details": null, "model": "mid-b", "output": [{"type": "message", "id":"<id>", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}], "previous_response_id": null, "store": false, "instructions": null, "max_output_tokens": null, "temperature": null, "top_p": null, "tools": [], "tool_choice": "auto", "parallel_tool_calls": true, "text": {"format": {"type": "text"}}, "reasoning": {"effort": null, "summary": null}, "metadata": {}, "truncation": "disabled"}}\n\n'
)


@pytest.mark.asyncio
async def test_chat_sse_bytes_and_headers(gateway):
    status, text, headers = await _stream_text(
        gateway, "/v1/chat/completions", _chat(MID, stream=True)
    )

    assert status == 200
    assert text == _CHAT_SSE
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache, no-transform"
    assert headers["x-accel-buffering"] == "no"


@pytest.mark.asyncio
async def test_a_chat_stream_failing_before_its_first_byte_is_an_http_status(gateway, monkeypatch):
    adapter = gateway.adapters[MID]

    class _Refused(Exception):
        status_code = 422

    async def refusing_stream(messages, **params):
        raise _Refused("bad tool schema")
        yield ""

    monkeypatch.setattr(adapter, "stream_chat_completion", refusing_stream)

    status, text, headers = await _stream_text(
        gateway, "/v1/chat/completions", _chat(MID, stream=True)
    )

    assert status == 422
    assert headers["content-type"].startswith("application/json")
    assert "error" in json.loads(text)


@pytest.mark.asyncio
async def test_a_responses_stream_failing_before_its_first_byte_is_an_http_status(
    gateway, monkeypatch
):
    adapter = gateway.adapters[MID]

    class _Refused(Exception):
        status_code = 422

    async def refusing_stream(messages, **params):
        raise _Refused("bad tool schema")
        yield ""

    monkeypatch.setattr(adapter, "stream_chat_completion", refusing_stream)

    status, _text, headers = await _stream_text(
        gateway, "/v1/responses", {"model": MID, "input": "hi", "stream": True, "store": False}
    )

    assert status == 422
    assert headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_messages_sse_bytes_through_a_translating_adapter(gateway):
    status, text, headers = await _stream_text(
        gateway, "/v1/messages", _anthropic(MID, stream=True)
    )

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert text == _MESSAGES_SSE


@pytest.mark.asyncio
async def test_responses_sse_events_and_headers(gateway):
    status, text, headers = await _stream_text(
        gateway, "/v1/responses", {"model": MID, "input": "hi", "stream": True, "store": False}
    )

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache, no-transform"
    assert headers["x-accel-buffering"] == "no"
    assert "x-provider" not in headers
    assert text == _RESPONSES_SSE


@pytest.mark.asyncio
async def test_compat_routes_reach_chat_with_their_runtime_settings_and_shapes(gateway):
    single = await gateway.post("/completion", _chat(MID))
    legacy = await gateway.post("/v1/completions", {"model": MID, "prompt": "hi"})

    assert single.status_code == legacy.status_code == 200
    assert single.json()["object"] == legacy.json()["object"] == "chat.completion"
    rows = await _rows(gateway.log_store, 2)
    sent = [{"role": "user", "content": "hi"}]
    for row in rows:
        assert row["prompt"] == [
            {
                "role": "user",
                "content": "hi",
                "tool_call_id": None,
                "name": None,
                "reasoning_content": None,
                "tool_calls": None,
            }
        ]
        # The legacy route's prompt arrives translated, as the chat body it became.
        assert row["request_payload"] == {"model": MID, "messages": sent}
        assert {k: v for k, v in row["params"].items() if k != "request_id"} == {
            "stream": False,
            "max_tokens": 8000,
        }


# -- Messages execution, called directly ------------------------------------------


def _direct_context() -> Any:
    from serving.surfaces.context import UNTRACKED, ClientOrigin, ExecutionContext
    from serving.utils.request_ip import ClientIpInfo

    ip = ClientIpInfo(
        client_ip="unknown",
        peer_ip="unknown",
        source="test",
        trusted_proxy_headers=False,
        resolved=False,
    )
    return ExecutionContext(
        user_ctx={"user_id": "user-1", "role": "free", "authenticated": True},
        origin=ClientOrigin.for_batch(path="/v1/messages", ip=ip, user_agent="test"),
        admission=UNTRACKED,
        batch=None,
        timeout_probe=None,
    )


def _messages_services(gateway: _Gateway) -> Any:
    return anthropic_messages.MessagesServices(
        router=gateway.router,
        log_store=None,
        op_store=None,
        visibility=None,
        completions_logger=None,
        app_services=gateway.app.state.services,
    )


def _count_end_dispatch(gateway: _Gateway, monkeypatch) -> list[Any]:
    registry = gateway.router.endpoint_health_registry
    ended: list[Any] = []
    real_end = registry.end_dispatch

    def counting_end(claim):
        ended.append(claim)
        return real_end(claim)

    monkeypatch.setattr(registry, "end_dispatch", counting_end)
    return ended


@pytest.mark.asyncio
async def test_a_messages_stream_closed_before_it_starts_releases_its_claim_once(
    gateway, monkeypatch
):
    ended = _count_end_dispatch(gateway, monkeypatch)
    called: list[Any] = []
    monkeypatch.setattr(
        gateway.adapters[MID], "stream_chat_completion", lambda *a, **k: called.append(a)
    )

    outcome = await anthropic_messages.execute_messages(
        _anthropic(MID, stream=True),
        _direct_context(),
        _messages_services(gateway),
        anthropic_messages.MessagesConstraints.online(),
    )

    assert isinstance(outcome, anthropic_messages.MessagesStream)
    assert ended == []  # held by the stream
    await outcome.aclose()
    assert len(ended) == 1
    assert called == []  # the upstream was never contacted


@pytest.mark.asyncio
async def test_a_messages_stream_closed_after_it_starts_releases_its_claim_once(
    gateway, monkeypatch
):
    ended = _count_end_dispatch(gateway, monkeypatch)

    outcome = await anthropic_messages.execute_messages(
        _anthropic(MID, stream=True),
        _direct_context(),
        _messages_services(gateway),
        anthropic_messages.MessagesConstraints.online(),
    )
    first = await outcome.frames.__anext__()
    await outcome.aclose()

    assert first.startswith(b"event: message_start")
    assert len(ended) == 1


@pytest.mark.asyncio
async def test_a_unary_messages_request_is_served_within_the_call(gateway, monkeypatch):
    ended = _count_end_dispatch(gateway, monkeypatch)

    outcome = await anthropic_messages.execute_messages(
        _anthropic(MID),
        _direct_context(),
        _messages_services(gateway),
        anthropic_messages.MessagesConstraints.online(),
    )

    assert isinstance(outcome, anthropic_messages.MessagesResponse)
    assert outcome.body["type"] == "message"
    assert len(ended) == 1


@pytest.mark.asyncio
async def test_a_malformed_messages_body_is_a_400_after_the_request_id(gateway):
    outcome = await anthropic_messages.execute_messages(
        anthropic_messages.MALFORMED_BODY,
        _direct_context(),
        _messages_services(gateway),
        anthropic_messages.MessagesConstraints.online(),
    )

    assert isinstance(outcome, anthropic_messages.MessagesError)
    assert (outcome.status, outcome.error_type) == (400, "invalid_request_error")
    assert outcome.message == "Invalid JSON in request body"


def test_messages_constraints_narrow_only_a_batch_item():
    online = anthropic_messages.MessagesConstraints.online()
    body = {"model": MID, "messages": [], "max_tokens": 1}
    item = anthropic_messages.MessagesConstraints.for_batch_item(body, "2026-10-21")

    assert (online.adapter_requirement, online.pin_model, online.anthropic_version) == (
        None,
        False,
        None,
    )
    assert item.adapter_requirement.body is body
    assert (item.pin_model, item.anthropic_version) == (True, "2026-10-21")
    assert (
        anthropic_messages.MessagesConstraints.for_batch_item(body, None).anthropic_version is None
    )


def test_no_batch_policy_travels_through_the_request_scope():
    import inspect

    from serving.batch import dispatch
    from serving.utils import context as req_ctx

    for module in (anthropic_messages, dispatch, req_ctx):
        assert "BATCH_EXECUTION" not in inspect.getsource(module)
    assert "batch_execution" not in req_ctx.REQUEST_SCOPED_KEYS


@pytest.mark.parametrize(
    "execute",
    [
        anthropic_messages.execute_messages,
        completions.execute_chat,
        embeddings.execute_embeddings,
        responses.execute_responses,
    ],
    ids=lambda fn: fn.__name__,
)
def test_an_execution_function_holds_no_http_request_or_response(execute):
    """The shared execution is called with values, never with the HTTP objects."""
    import ast
    import inspect
    import textwrap

    http_types = re.compile(r"\b(Request|Response|StreamingResponse|JSONResponse)\b")
    parameters = inspect.signature(execute).parameters
    for name, parameter in parameters.items():
        assert not http_types.search(str(parameter.annotation)), name
    tree = ast.parse(textwrap.dedent(inspect.getsource(execute)))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    forbidden = {
        "request",
        "http_request",
        "http_response",
        "Request",
        "StreamingResponse",
        "JSONResponse",
        "Depends",
        "Header",
        "call_handler",
    }
    # A parameter of that name holding a protocol value (embeddings' validated
    # ``EmbeddingRequest``) is not an HTTP request.
    assert not (used & forbidden) - set(parameters), used & forbidden


# -- a client that leaves before the stream starts (T7) -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/v1/chat/completions", _chat(MID, stream=True), id="chat"),
        pytest.param(
            "/v1/responses",
            {"model": MID, "input": "hi", "stream": True, "store": False},
            id="responses",
        ),
    ],
)
async def test_a_primed_stream_the_client_left_is_still_finalized(
    gateway, monkeypatch, path, body, spec_version
):
    """The chat stream is read before the response starts; leaving then still closes it.

    The session stream has started (its first item was pulled to decide the
    status) when Starlette may never iterate the response body: the client is
    already gone. The session must still end as a client disconnect -- a 499
    row, no charge -- the upstream stream must be closed, and the request's
    interactive load released. The close comes from the event loop finalizing
    the abandoned session stream; ``gc.collect`` makes that deterministic here.
    """
    import gc

    load = _capture_interactive_load(gateway, monkeypatch)
    adapter = gateway.adapters[MID]
    closed: list[bool] = []

    async def held_stream(messages, **params):
        try:
            yield adapter.format_stream_chunk(model=MID, content="ok")
            await asyncio.sleep(30)
            yield "data: [DONE]\n\n"
        finally:
            closed.append(True)

    monkeypatch.setattr(adapter, "stream_chat_completion", held_stream)
    charges: list[Any] = []
    real_charge = CostTracker.schedule_increment

    async def counting_charge(self, **kwargs):
        charges.append(kwargs)
        return await real_charge(self, **kwargs)

    monkeypatch.setattr(CostTracker, "schedule_increment", counting_charge)
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 5001),
        "server": ("test", 80),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}  # already gone

    async def send(message):
        if spec_version == "2.4":
            raise OSError("peer closed")

    try:
        await asyncio.wait_for(gateway.app(scope, receive, send), timeout=10)
    except Exception as exc:  # the 2.4 path surfaces the dead peer
        assert spec_version == "2.4", exc
    for _ in range(3):
        gc.collect()
        for _ in range(20):
            await asyncio.sleep(0)

    assert closed == [True]
    assert charges == []
    assert load.in_flight(MID) == 0
    rows = await _rows(gateway.log_store)
    assert [row["status_code"] for row in rows] == [499]
    assert rows[0]["metadata"]["terminal_state"] == "client_disconnect"


@pytest.mark.asyncio
async def test_close_if_unstarted_closes_only_a_body_that_never_ran():
    """The release at the end of a request never cuts a body that is streaming."""
    closed: list[str] = []

    async def tail(name: str):
        try:
            yield f"{name}-1"
            yield f"{name}-2"
        finally:
            closed.append(name)

    def stream() -> Any:
        return completions.ChatStream(
            session=None,
            adapter_chunks=None,
            buffered=True,
            request_id="r",
            model=MID,
            request_headers={},
            provider_header=None,
        )

    # Read ahead (as prime() and the buffered path both do), body never iterated.
    unstarted = stream()
    rest = tail("unstarted")
    first = await rest.__anext__()
    unstarted.adopt(first, rest)
    await unstarted.close_if_unstarted()
    assert closed == ["unstarted"]

    # Body iterated: only its own finally closes the stream, once.
    started = stream()
    rest = tail("started")
    started.adopt(await rest.__anext__(), rest)
    frames = started.frames()
    assert await frames.__anext__() == "started-1"
    await started.close_if_unstarted()
    assert closed == ["unstarted"]
    assert [item async for item in frames] == ["started-2"]
    assert closed == ["unstarted", "started"]
