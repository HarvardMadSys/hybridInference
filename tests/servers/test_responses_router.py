"""End-to-end tests for the OpenAI Responses API router (``/v1/responses``).

Exercises the full delegation path through ``chat_completions`` with stub
adapters: request translation, response shaping, streaming SSE re-framing,
tools, and statefulness (``store`` / ``previous_response_id`` / GET / DELETE).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import responses
from serving.stream import done_sentinel, make_final_usage_chunk

RESPONSES_TEST_API_KEY = "hyi-responses-test"
TEXT_MODEL = "glm-4.7"
TOOL_MODEL = "tool-model"


def _auth() -> dict[str, str]:
    return {"x-api-key": RESPONSES_TEST_API_KEY}


def _rc(messages: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """(role, content) pairs — chat_completions adds None defaults via model_dump."""
    return [(m.get("role"), m.get("content")) for m in messages]


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supports_tools=True,
        supported_params=["temperature", "top_p", "max_tokens", "tools", "tool_choice"],
        pricing={"prompt": "0", "completion": "0"},
    )


class TextAdapter(BaseAdapter):
    """Echoes the last captured request params; returns a fixed text reply."""

    last_params: dict[str, Any] = {}  # noqa: RUF012
    last_messages: list[dict[str, Any]] = []  # noqa: RUF012

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        type(self).last_params = params
        type(self).last_messages = messages
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "created": 1,
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello there"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        type(self).last_params = params
        type(self).last_messages = messages
        yield self.format_stream_chunk(model=self.config.id, content="Hello ")
        yield self.format_stream_chunk(model=self.config.id, content="there")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="Hello there"
        )
        yield done_sentinel()


class ToolAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return {
            "id": "chatcmpl-t",
            "object": "chat.completion",
            "created": 1,
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"sf"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        mid = self.config.id
        yield (
            'data: {"id":"t","object":"chat.completion.chunk","created":1,"model":"'
            + mid
            + '","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"type":"function","function":{"name":"get_weather","arguments":""}}]},'
            '"finish_reason":null}]}\n\n'
        )
        yield (
            'data: {"id":"t","object":"chat.completion.chunk","created":1,"model":"'
            + mid
            + '","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        )
        yield make_final_usage_chunk(model=mid, messages=messages, total_content="")
        yield done_sentinel()


class _FakeResponseStore:
    """In-memory ResponseStore stand-in for router tests."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    async def save(
        self,
        *,
        response_id: str,
        user_id: str,
        response: dict[str, Any],
        messages: list[dict[str, Any]],
        previous_response_id: str | None = None,
        model: str | None = None,
    ) -> None:
        self.data[response_id] = {
            "id": response_id,
            "user_id": user_id,
            "response": response,
            "messages": messages,
            "previous_response_id": previous_response_id,
            "model": model,
        }

    async def get(self, response_id: str) -> dict[str, Any] | None:
        return self.data.get(response_id)

    async def delete(self, response_id: str, *, user_id: str | None = None) -> bool:
        row = self.data.get(response_id)
        if not row or (user_id is not None and row["user_id"] != user_id):
            return False
        del self.data[response_id]
        return True


@pytest_asyncio.fixture
async def responses_store() -> _FakeResponseStore:
    return _FakeResponseStore()


@pytest_asyncio.fixture
async def responses_app(responses_store, mock_db_logger, mock_operational_store, mock_log_store):
    router = RouteExecutor()
    router.register_route(TEXT_MODEL, [(TextAdapter(_cfg(TEXT_MODEL)), 1.0)])
    router.register_route(TOOL_MODEL, [(ToolAdapter(_cfg(TOOL_MODEL)), 1.0)])

    services = AppServices(
        router=router,
        db_logger=mock_db_logger,
        operational_store=mock_operational_store,
        log_store=mock_log_store,
        responses_store=responses_store,
        routing_manager=None,
    )

    app = FastAPI(title="Responses Compat Test App")
    app.state.services = services

    async def fake_verify(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None, alias="X-API-Key"),
    ):
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer ") :]
        elif x_api_key:
            token = x_api_key
        if token != RESPONSES_TEST_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, "user_id": "test-user", "role": "internal"}

    app.dependency_overrides[verify_api_key] = fake_verify
    app.dependency_overrides[enforce_user_concurrency] = lambda: None

    install_error_handlers(app)
    app.include_router(responses.router)
    return app


@pytest_asyncio.fixture
async def responses_client(responses_app):
    transport = ASGITransport(app=responses_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- non-streaming ---------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_text_response(responses_client):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello", "instructions": "be brief"},
        headers=_auth(),
    )
    assert r.status_code == 200
    out = r.json()
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["id"].startswith("resp_")
    assert out["model"] == TEXT_MODEL
    assert out["instructions"] == "be brief"
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"][0]["type"] == "output_text"
    assert out["output"][0]["content"][0]["text"] == "Hello there"
    assert out["usage"]["input_tokens"] == 5
    assert out["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_instructions_become_system_message(responses_client):
    await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hi", "instructions": "you are a pirate"},
        headers=_auth(),
    )
    assert _rc(TextAdapter.last_messages)[0] == ("system", "you are a pirate")
    assert _rc(TextAdapter.last_messages)[1] == ("user", "hi")


@pytest.mark.asyncio
async def test_developer_role_input_does_not_400(responses_client):
    r = await responses_client.post(
        "/v1/responses",
        json={
            "model": TEXT_MODEL,
            "input": [{"type": "message", "role": "developer", "content": "be terse"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert _rc(TextAdapter.last_messages)[0] == ("system", "be terse")


@pytest.mark.asyncio
async def test_max_output_tokens_mapped(responses_client):
    await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hi", "max_output_tokens": 42, "temperature": 0.3},
        headers=_auth(),
    )
    assert TextAdapter.last_params.get("max_tokens") == 42
    assert TextAdapter.last_params.get("temperature") == 0.3


@pytest.mark.asyncio
async def test_tool_call_response(responses_client):
    r = await responses_client.post(
        "/v1/responses",
        json={
            "model": TOOL_MODEL,
            "input": "weather in sf?",
            "tools": [
                {"type": "function", "name": "get_weather", "parameters": {"type": "object"}}
            ],
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    out = r.json()
    fc = out["output"][0]
    assert fc["type"] == "function_call"
    assert fc["name"] == "get_weather"
    assert fc["call_id"] == "call_1"
    assert fc["arguments"] == '{"city":"sf"}'


@pytest.mark.asyncio
async def test_json_schema_structured_output_reaches_adapter(responses_client):
    """text.format json_schema must survive ChatCompletionRequest validation and
    reach the adapter as response_format with the nested schema intact."""
    await responses_client.post(
        "/v1/responses",
        json={
            "model": TEXT_MODEL,
            "input": "hi",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "Foo",
                    "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
                    "strict": True,
                }
            },
        },
        headers=_auth(),
    )
    rf = TextAdapter.last_params.get("response_format")
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "Foo"
    assert rf["json_schema"]["schema"] == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }


@pytest.mark.asyncio
async def test_unknown_model_returns_404_error_envelope(responses_client):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": "no-such-model", "input": "hi"},
        headers=_auth(),
    )
    assert r.status_code == 404
    assert "error" in r.json()


@pytest.mark.asyncio
async def test_missing_model_returns_400(responses_client):
    r = await responses_client.post("/v1/responses", json={"input": "hi"}, headers=_auth())
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_missing_auth_returns_401(responses_client):
    r = await responses_client.post("/v1/responses", json={"model": TEXT_MODEL, "input": "hi"})
    assert r.status_code == 401


# --- streaming -------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_text_events(responses_client):
    body = {"model": TEXT_MODEL, "input": "hi", "stream": True}
    collected = b""
    async with responses_client.stream("POST", "/v1/responses", json=body, headers=_auth()) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            collected += chunk
    text = collected.decode()
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "event: response.completed" in text
    # Reassemble streamed text from output_text.delta events.
    deltas = [
        json.loads(line[len("data: ") :])["delta"]
        for line in text.splitlines()
        if line.startswith("data: ") and '"response.output_text.delta"' in line
    ]
    assert "".join(deltas) == "Hello there"


@pytest.mark.asyncio
async def test_streaming_tool_call_events(responses_client):
    body = {"model": TOOL_MODEL, "input": "weather?", "stream": True}
    collected = b""
    async with responses_client.stream("POST", "/v1/responses", json=body, headers=_auth()) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            collected += chunk
    text = collected.decode()
    assert '"type": "function_call"' in text
    assert "event: response.function_call_arguments.done" in text
    assert "event: response.completed" in text


# --- statefulness ----------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_get_roundtrip(responses_client, responses_store):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello"},
        headers=_auth(),
    )
    assert r.status_code == 200
    resp_id = r.json()["id"]
    assert resp_id in responses_store.data

    g = await responses_client.get(f"/v1/responses/{resp_id}", headers=_auth())
    assert g.status_code == 200
    assert g.json()["id"] == resp_id
    assert g.json()["output"][0]["content"][0]["text"] == "Hello there"


@pytest.mark.asyncio
async def test_streaming_response_persisted_by_stream_close(responses_client, responses_store):
    """A stored streaming response must be persisted before the terminal events,
    so an immediate GET after the stream closes cannot race the write."""
    body = {"model": TEXT_MODEL, "input": "hi", "stream": True}
    collected = b""
    async with responses_client.stream("POST", "/v1/responses", json=body, headers=_auth()) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            collected += chunk
    resp_id = None
    for line in collected.decode().splitlines():
        if line.startswith("data: ") and '"response.created"' in line:
            resp_id = json.loads(line[len("data: ") :])["response"]["id"]
            break
    assert resp_id is not None
    # Persisted synchronously before the stream closed — no background race.
    assert resp_id in responses_store.data
    g = await responses_client.get(f"/v1/responses/{resp_id}", headers=_auth())
    assert g.status_code == 200


@pytest.mark.asyncio
async def test_store_false_not_persisted(responses_client, responses_store):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello", "store": False},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert responses_store.data == {}


@pytest.mark.asyncio
async def test_previous_response_id_chains_conversation(responses_client, responses_store):
    first = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "my name is Sam"},
        headers=_auth(),
    )
    resp_id = first.json()["id"]

    await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "what is my name?", "previous_response_id": resp_id},
        headers=_auth(),
    )
    # The second turn must replay prior user + assistant turns, then the new input.
    pairs = _rc(TextAdapter.last_messages)
    assert ("user", "my name is Sam") in pairs
    assert ("assistant", "Hello there") in pairs
    assert pairs[-1] == ("user", "what is my name?")


@pytest.mark.asyncio
async def test_previous_response_id_unknown_returns_404(responses_client):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hi", "previous_response_id": "resp_nope"},
        headers=_auth(),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_response(responses_client, responses_store):
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello"},
        headers=_auth(),
    )
    resp_id = r.json()["id"]
    d = await responses_client.delete(f"/v1/responses/{resp_id}", headers=_auth())
    assert d.status_code == 200
    assert d.json() == {"id": resp_id, "object": "response.deleted", "deleted": True}
    assert resp_id not in responses_store.data

    g = await responses_client.get(f"/v1/responses/{resp_id}", headers=_auth())
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_get_unknown_response_404(responses_client):
    g = await responses_client.get("/v1/responses/resp_missing", headers=_auth())
    assert g.status_code == 404
