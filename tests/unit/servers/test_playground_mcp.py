"""The playground MCP proxy: URL safety, framing, and the endpoint contract."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving import mcp_client
from serving.servers.deps import get_current_user
from serving.servers.routers import playground_mcp

INTERNAL = {"user_id": "u-internal", "role": "internal", "is_admin": False}
FREE = {"user_id": "u-free", "role": "free", "is_admin": False}


def _client(user: dict[str, Any]) -> AsyncClient:
    app = FastAPI()
    app.include_router(playground_mcp.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sse(*frames: dict[str, Any]) -> str:
    return "".join(f"event: message\ndata: {json.dumps(frame)}\n\n" for frame in frames)


class _Server:
    """A fake httpx client that answers the MCP handshake and one method."""

    def __init__(self, results: list[dict[str, Any]], *, content_type: str = "application/json"):
        self.results = results
        self.content_type = content_type
        self.requests: list[dict[str, Any]] = []

    async def post(
        self, _url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        assert json is not None
        self.requests.append(json)
        if "id" not in json:
            return httpx.Response(202, headers={"mcp-session-id": "s-1"})
        result = self.results.pop(0)
        frame = {"jsonrpc": "2.0", "id": json["id"], "result": result}
        if self.content_type == "text/event-stream":
            return httpx.Response(
                200,
                text=_sse(frame),
                headers={"content-type": "text/event-stream", "mcp-session-id": "s-1"},
            )
        return httpx.Response(200, json=frame, headers={"mcp-session-id": "s-1"})


@pytest.mark.asyncio
async def test_list_tools_performs_the_handshake_and_returns_tool_definitions() -> None:
    server = _Server([{"protocolVersion": "2025-06-18"}, {"tools": [{"name": "search"}]}])
    session = mcp_client._Session(server, "https://mcp.example/rpc", {})  # type: ignore[arg-type]
    await session.initialize()
    result = await session.request("tools/list")
    assert result == {"tools": [{"name": "search"}]}
    assert [request["method"] for request in server.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.asyncio
async def test_an_sse_answer_is_parsed() -> None:
    server = _Server(
        [{"protocolVersion": "2025-06-18"}, {"tools": [{"name": "search"}]}],
        content_type="text/event-stream",
    )
    session = mcp_client._Session(server, "https://mcp.example/rpc", {})  # type: ignore[arg-type]
    await session.initialize()
    assert await session.request("tools/list") == {"tools": [{"name": "search"}]}


@pytest.mark.asyncio
async def test_an_error_frame_becomes_an_mcp_error() -> None:
    class _Failing(_Server):
        async def post(
            self,
            _url: str,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            assert json is not None
            if "id" not in json:
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "error": {"code": -32602, "message": "bad arguments"},
                },
            )

    session = mcp_client._Session(_Failing([]), "https://mcp.example/rpc", {})  # type: ignore[arg-type]
    with pytest.raises(mcp_client.McpError, match="bad arguments"):
        await session.initialize()


def test_url_validation_rejects_non_http_and_link_local_addresses() -> None:
    for url in ("ftp://mcp.example", "file:///etc/passwd", "http://169.254.169.254/latest"):
        with pytest.raises(mcp_client.McpError):
            mcp_client.validate_mcp_url(url)
    assert mcp_client.validate_mcp_url("http://127.0.0.1:8000/mcp")


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                mcp_client.socket.AF_INET,
                mcp_client.socket.SOCK_STREAM,
                6,
                "",
                ("169.254.169.254", 0),
            )
        ],
    )
    with pytest.raises(mcp_client.McpError, match="not allowed"):
        await mcp_client._refuse_metadata_resolution("https://mcp.example/rpc")


def test_tool_content_is_flattened_to_text() -> None:
    assert (
        mcp_client._flatten_content(
            [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        )
        == "first\nsecond"
    )
    assert mcp_client._flatten_content([{"type": "image", "data": "..."}]) == (
        '{"type": "image", "data": "..."}'
    )


@pytest.mark.asyncio
async def test_listing_tools_is_available_to_internal_users() -> None:
    async def _tools(_url: str, _headers: Any = None) -> list[dict[str, Any]]:
        return [{"name": "search", "description": "Find pages", "inputSchema": {"type": "object"}}]

    original = mcp_client.list_tools
    mcp_client.list_tools = _tools  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/tools", json={"url": "https://mcp.example/rpc"}
            )
    finally:
        mcp_client.list_tools = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json()["tools"] == [
        {"name": "search", "description": "Find pages", "inputSchema": {"type": "object"}}
    ]


@pytest.mark.asyncio
async def test_a_free_user_cannot_use_the_proxy() -> None:
    async with _client(FREE) as client:
        response = await client.post(
            "/internal/playground/mcp/tools", json={"url": "https://mcp.example/rpc"}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_tool_call_returns_the_flattened_result() -> None:
    async def _call(
        _url: str, _name: str, _arguments: dict[str, Any], _headers: Any = None
    ) -> dict[str, Any]:
        return {"content": "42", "isError": False}

    original = mcp_client.call_tool
    mcp_client.call_tool = _call  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/call",
                json={"url": "https://mcp.example/rpc", "name": "search", "arguments": {}},
            )
    finally:
        mcp_client.call_tool = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json() == {"content": "42", "isError": False}


@pytest.mark.asyncio
async def test_an_unreachable_server_is_a_bad_gateway() -> None:
    async def _tools(_url: str, _headers: Any = None) -> list[dict[str, Any]]:
        raise mcp_client.McpError("Could not reach the MCP server")

    original = mcp_client.list_tools
    mcp_client.list_tools = _tools  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/tools", json={"url": "https://mcp.example/rpc"}
            )
    finally:
        mcp_client.list_tools = original  # type: ignore[assignment]

    assert response.status_code == 502
    assert "Could not reach" in response.json()["detail"]


@pytest.mark.asyncio
async def test_chat_forwards_tools_and_tool_choice() -> None:
    from serving.servers.routers.playground import PlaygroundChatRequest, _public_chat_request

    body = PlaygroundChatRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "search"}}],
            "tool_choice": "auto",
        }
    )
    payload = _public_chat_request(body)
    assert payload["tools"] == [{"type": "function", "function": {"name": "search"}}]
    assert payload["tool_choice"] == "auto"
