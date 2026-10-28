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

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import responses
from serving.stream import done_sentinel, make_final_usage_chunk

RESPONSES_TEST_API_KEY = "hyi-responses-test"
OTHER_USER_API_KEY = "hyi-responses-other"
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
            '"id":null,"type":null,"function":{"name":null,"arguments":"{}"}}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
        )
        yield make_final_usage_chunk(model=mid, messages=messages, total_content="")
        yield done_sentinel()


class _FakeResponseStore:
    """In-memory ResponseStore stand-in for router tests."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.persist_enabled = True

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
    router = FixedRouter()
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
        if token == OTHER_USER_API_KEY:
            return {"authenticated": True, "user_id": "other-user", "role": "internal"}
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
async def test_strict_reasoning_header_stripped_so_reasoning_persists(
    responses_client, responses_store
):
    """An inbound X-Reasoning-Passthrough:false must not strip reasoning_content
    before it is persisted for chaining."""

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

    router = responses_client._transport.app.state.services.router
    router.register_route("reasoning-model", [(_ReasoningAdapter(_cfg("reasoning-model")), 1.0)])

    r = await responses_client.post(
        "/v1/responses",
        json={"model": "reasoning-model", "input": "hi"},
        headers={**_auth(), "X-Reasoning-Passthrough": "false"},
    )
    assert r.status_code == 200
    stored = responses_store.data[r.json()["id"]]
    assert stored["messages"][-1]["reasoning_content"] == "deliberation"


@pytest.mark.asyncio
async def test_privacy_mode_store_disabled_not_persisted(responses_client, responses_store):
    """When the store has persistence disabled (privacy mode), nothing is saved
    and the echoed store flag is false."""
    responses_store.persist_enabled = False
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hi"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["store"] is False
    assert responses_store.data == {}


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
    completed = next(
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ") and '"response.completed"' in line
    )
    function_call = completed["response"]["output"][0]
    assert function_call["call_id"] == "call_1"
    assert function_call["name"] == "get_weather"
    assert function_call["arguments"] == "{}"


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


# --- session identity ------------------------------------------------------


async def _wait_for_log_kwargs(mock_log_store, timeout: float = 2.0) -> dict[str, Any] | None:
    """Wait for the fire-and-forget log task and return its kwargs."""
    import asyncio
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_log_store.log_request.call_count > 0:
            return mock_log_store.log_request.call_args.kwargs
        await asyncio.sleep(0.05)
    return None


@pytest.mark.asyncio
async def test_declared_session_survives_the_translation(responses_client, mock_log_store):
    """A Responses client's ``metadata.session_id`` must not be lost in translation.

    Delegation replaces the body ``chat_completions`` sees with the translated
    chat request, so the declaration only reaches the resolver if this surface
    carries ``metadata`` across.
    """
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello", "metadata": {"session_id": "resp-run-1"}},
        headers=_auth(),
    )
    assert r.status_code == 200

    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None, "log_request was never called"
    assert kwargs["metadata"]["session_id"] == "resp-run-1"
    assert kwargs["metadata"]["session_id_source"] == "metadata.session_id"
    # And the router sees it, which is what session-scoped accounting reads.
    assert TextAdapter.last_params["session_id"] == "resp-run-1"


@pytest.mark.asyncio
async def test_codex_session_header_survives_the_translation(responses_client, mock_log_store):
    """Codex CLI sends its run as a `session-id` header; delegation keeps headers.

    The hyphenated spelling is the one Codex actually sends — Starlette matches
    header names case-insensitively but not across separators, so an
    underscore-only list would miss every Codex request.
    """
    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "hello"},
        headers={**_auth(), "session-id": "codex-run-2", "thread-id": "codex-thread-2"},
    )
    assert r.status_code == 200

    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert kwargs["metadata"]["session_id"] == "codex-run-2"
    assert kwargs["metadata"]["session_id_source"] == "session-id"


@pytest.mark.asyncio
async def test_codex_client_metadata_survives_the_translation(responses_client, mock_log_store):
    """Codex also carries the run in `client_metadata`, which is not a chat field."""
    r = await responses_client.post(
        "/v1/responses",
        json={
            "model": TEXT_MODEL,
            "input": "hello",
            "client_metadata": {"session_id": "codex-body-2"},
        },
        headers=_auth(),
    )
    assert r.status_code == 200

    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert kwargs["metadata"]["session_id"] == "codex-body-2"
    assert kwargs["metadata"]["session_id_source"] == "client_metadata.session_id"
    assert TextAdapter.last_params["session_id"] == "codex-body-2"


# --- one user's stored responses are not another's ---------------------------


async def _stored_by_test_user(client) -> str:
    r = await client.post(
        "/v1/responses", json={"model": TEXT_MODEL, "input": "my secret"}, headers=_auth()
    )
    assert r.status_code == 200
    return r.json()["id"]


def _other() -> dict[str, str]:
    return {"x-api-key": OTHER_USER_API_KEY}


@pytest.mark.asyncio
async def test_another_user_cannot_read_a_stored_response(responses_client):
    resp_id = await _stored_by_test_user(responses_client)

    g = await responses_client.get(f"/v1/responses/{resp_id}", headers=_other())

    assert g.status_code == 404
    assert "my secret" not in g.text


@pytest.mark.asyncio
async def test_another_user_cannot_delete_a_stored_response(responses_client, responses_store):
    resp_id = await _stored_by_test_user(responses_client)

    d = await responses_client.delete(f"/v1/responses/{resp_id}", headers=_other())

    assert d.status_code == 404
    assert resp_id in responses_store.data


@pytest.mark.asyncio
async def test_another_user_cannot_continue_a_stored_conversation(responses_client):
    resp_id = await _stored_by_test_user(responses_client)
    TextAdapter.last_messages = []

    r = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "what did I say?", "previous_response_id": resp_id},
        headers=_other(),
    )

    assert r.status_code == 404
    assert TextAdapter.last_messages == []  # nothing reached a provider


@pytest.mark.asyncio
async def test_deleting_an_unknown_response_is_a_404(responses_client):
    d = await responses_client.delete("/v1/responses/resp_missing", headers=_auth())

    assert d.status_code == 404


# --- without a response store ----------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_store_a_response_is_served_but_not_kept(responses_app, responses_client):
    responses_app.state.services.responses_store = None

    r = await responses_client.post(
        "/v1/responses", json={"model": TEXT_MODEL, "input": "hi"}, headers=_auth()
    )
    assert r.status_code == 200
    assert r.json()["store"] is False
    resp_id = r.json()["id"]

    assert (
        await responses_client.get(f"/v1/responses/{resp_id}", headers=_auth())
    ).status_code == 404
    d = await responses_client.delete(f"/v1/responses/{resp_id}", headers=_auth())
    assert d.status_code == 404
    chained = await responses_client.post(
        "/v1/responses",
        json={"model": TEXT_MODEL, "input": "again", "previous_response_id": resp_id},
        headers=_auth(),
    )
    assert chained.status_code == 404


@pytest.mark.asyncio
async def test_a_store_failure_does_not_fail_the_request(responses_client, responses_store, caplog):
    async def broken_save(**kwargs):
        raise RuntimeError("database down")

    responses_store.save = broken_save

    r = await responses_client.post(
        "/v1/responses", json={"model": TEXT_MODEL, "input": "hi"}, headers=_auth()
    )

    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == "Hello there"
    assert any("Failed to persist response" in rec.getMessage() for rec in caplog.records)


# --- malformed requests -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"{not json", "Invalid JSON in request body"),
        (b'["a", "list"]', "Request body must be a JSON object"),
        (json.dumps({"model": TEXT_MODEL}).encode(), "Missing required parameter: 'input'"),
    ],
    ids=["not-json", "not-an-object", "no-input"],
)
async def test_a_malformed_request_is_a_400(responses_client, content, detail):
    r = await responses_client.post(
        "/v1/responses",
        content=content,
        headers={**_auth(), "content-type": "application/json"},
    )

    assert r.status_code == 400
    assert r.json()["error"]["message"] == detail


@pytest.mark.asyncio
async def test_a_non_dict_chat_result_is_a_502(responses_client, monkeypatch, responses_store):
    from serving.servers.routers.completions import ChatResponse

    async def odd_delegate(*args, **kwargs):
        return ChatResponse("not a completion", None)

    monkeypatch.setattr(responses, "execute_chat", odd_delegate)

    r = await responses_client.post(
        "/v1/responses", json={"model": TEXT_MODEL, "input": "hi"}, headers=_auth()
    )

    assert r.status_code == 502
    assert responses_store.data == {}


# --- a stream the client walks away from ----------------------------------------------


def _chunk(content: str | None = None, finish: str | None = None) -> bytes:
    delta = {"content": content} if content is not None else {}
    payload = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": TEXT_MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _open_stream(monkeypatch, store, chunks: list[bytes], *, keep: bool = True):
    """Re-frame a chat stream that yields ``chunks`` then hangs."""
    import asyncio

    async def upstream():
        for chunk in chunks:
            yield chunk
        await asyncio.Event().wait()  # the client leaves before the upstream finishes

    return responses._response_events(
        upstream(),
        response_store=store,
        body={"model": TEXT_MODEL, "input": "hi", "stream": True},
        model=TEXT_MODEL,
        response_id="resp_walked_away",
        created_at=1,
        previous_response_id=None,
        store=keep,
        convo_messages=[{"role": "user", "content": "hi"}],
        user_id="test-user",
    )


async def _drain_background() -> None:
    import asyncio

    await asyncio.gather(*list(responses._background_tasks))


async def _read_until(events, marker: str) -> str:
    seen = ""
    while marker not in seen:
        seen += (await events.__anext__()).decode()
    return seen


@pytest.mark.asyncio
async def test_a_stream_the_client_leaves_is_kept_as_interrupted(monkeypatch, responses_store):
    """The partial turn stays chainable, but never as a normal completion."""
    events = await _open_stream(monkeypatch, responses_store, [_chunk("Hel"), _chunk("lo")])

    await _read_until(events, '"lo"')
    await events.aclose()
    await _drain_background()

    row = responses_store.data["resp_walked_away"]
    assert row["response"]["status"] == "incomplete"
    assert row["response"]["incomplete_details"] == {"reason": "interrupted"}
    assert row["user_id"] == "test-user"
    assert row["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello"},
    ]


@pytest.mark.asyncio
async def test_a_stream_left_with_store_false_keeps_nothing(monkeypatch, responses_store):
    events = await _open_stream(monkeypatch, responses_store, [_chunk("Hel")], keep=False)

    await _read_until(events, '"Hel"')
    await events.aclose()
    await _drain_background()

    assert responses_store.data == {}


@pytest.mark.asyncio
async def test_a_stream_the_upstream_failed_keeps_nothing(monkeypatch, responses_store):
    """An upstream error mid-stream is a failed turn, whether or not the client stays."""
    error = b'data: {"error": {"code": "overloaded", "message": "try later"}}\n\n'
    # The keepalive after the error tells the test the error has been read.
    events = await _open_stream(monkeypatch, responses_store, [_chunk("Hel"), error, b": ping\n\n"])

    await _read_until(events, "keepalive")
    await events.aclose()
    await _drain_background()

    assert responses_store.data == {}


@pytest.mark.asyncio
async def test_a_failed_stream_ends_with_response_failed_and_keeps_nothing(
    monkeypatch, responses_store
):
    async def upstream():
        yield _chunk("Hel")
        yield b'data: {"error": {"code": "overloaded", "message": "try later"}}\n\n'

    events = responses._response_events(
        upstream(),
        response_store=responses_store,
        body={"model": TEXT_MODEL, "input": "hi", "stream": True},
        model=TEXT_MODEL,
        response_id="resp_failed",
        created_at=1,
        previous_response_id=None,
        store=True,
        convo_messages=[{"role": "user", "content": "hi"}],
        user_id="test-user",
    )

    text = "".join([event.decode() async for event in events])

    assert "event: response.failed" in text
    assert '"code": "overloaded"' in text or '"code":"overloaded"' in text
    assert "response.completed" not in text
    assert responses_store.data == {}
