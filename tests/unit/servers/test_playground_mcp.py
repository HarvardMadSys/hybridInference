"""The playground MCP proxy: destination policy, framing, and the endpoints."""

from __future__ import annotations

import asyncio
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
TARGET = mcp_client._Target("https://mcp.example/rpc", "mcp.example", "mcp.example")


@pytest.fixture(autouse=True)
def _clear_mcp_sessions():
    mcp_client._session_cache.clear()
    yield
    mcp_client._session_cache.clear()


def _client(user: dict[str, Any]) -> AsyncClient:
    app = FastAPI()
    app.include_router(playground_mcp.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sse(*frames: dict[str, Any]) -> str:
    return "".join(f"event: message\ndata: {json.dumps(frame)}\n\n" for frame in frames)


class _FakeStream:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, *_args: Any) -> None:
        pass


class _FakeClient:
    """An httpx client that answers the MCP handshake and one method."""

    def __init__(self, results: list[dict[str, Any]], *, content_type: str = "application/json"):
        self.results = results
        self.content_type = content_type
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def stream(
        self,
        _method: str,
        _url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> _FakeStream:
        assert json is not None
        self.requests.append(json)
        self.headers.append(headers or {})
        if "id" not in json:
            return _FakeStream(httpx.Response(202, headers={"mcp-session-id": "s-1"}))
        result = self.results.pop(0)
        frame = {"jsonrpc": "2.0", "id": json["id"], "result": result}
        if self.content_type == "text/event-stream":
            return _FakeStream(
                httpx.Response(
                    200,
                    text=_sse(frame),
                    headers={"content-type": "text/event-stream", "mcp-session-id": "s-1"},
                )
            )
        return _FakeStream(httpx.Response(200, json=frame, headers={"mcp-session-id": "s-1"}))


@pytest.mark.asyncio
async def test_the_handshake_sends_the_protocol_version_and_session() -> None:
    server = _FakeClient([{"protocolVersion": "2025-06-18"}, {"tools": [{"name": "search"}]}])
    session = mcp_client._Session(server, TARGET, {})  # type: ignore[arg-type]
    await session.initialize()
    result = await session.request("tools/list")
    assert result == {"tools": [{"name": "search"}]}
    assert [request["method"] for request in server.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert all(headers.get("Mcp-Protocol-Version") == "2025-06-18" for headers in server.headers)
    assert server.headers[-1].get("Mcp-Session-Id") == "s-1"
    assert server.headers[-1].get("Host") == "mcp.example"


@pytest.mark.asyncio
async def test_the_negotiated_protocol_version_is_used_after_initialize() -> None:
    server = _FakeClient([{"protocolVersion": "2025-03-26"}, {"tools": []}])
    session = mcp_client._Session(server, TARGET, {})  # type: ignore[arg-type]
    await session.initialize()
    await session.request("tools/list")

    assert server.headers[1]["Mcp-Protocol-Version"] == "2025-03-26"
    assert server.headers[2]["Mcp-Protocol-Version"] == "2025-03-26"


@pytest.mark.asyncio
async def test_a_session_is_reused_for_later_calls() -> None:
    server = _FakeClient([{"protocolVersion": "2025-06-18"}])
    first = await mcp_client._open_session(server, TARGET, {})  # type: ignore[arg-type]
    second = await mcp_client._open_session(server, TARGET, {})  # type: ignore[arg-type]

    assert first.session_id == second.session_id == "s-1"
    assert [request["method"] for request in server.requests] == [
        "initialize",
        "notifications/initialized",
    ]


@pytest.mark.asyncio
async def test_cached_sessions_are_scoped_to_the_playground_connection() -> None:
    server = _FakeClient([{"protocolVersion": "2025-06-18"}, {"protocolVersion": "2025-06-18"}])
    first = await mcp_client._open_session(server, TARGET, {}, scope="u1:conn-1")  # type: ignore[arg-type]
    second = await mcp_client._open_session(server, TARGET, {}, scope="u2:conn-1")  # type: ignore[arg-type]

    assert first.session_id == second.session_id == "s-1"
    assert [request["method"] for request in server.requests].count("initialize") == 2


@pytest.mark.asyncio
async def test_cached_sessions_use_the_original_host_identity() -> None:
    server = _FakeClient([{"protocolVersion": "2025-06-18"}, {"protocolVersion": "2025-06-18"}])
    first = mcp_client._Target(
        "https://1.2.3.4/rpc", "a.example", "a.example", "https://a.example/rpc"
    )
    second = mcp_client._Target(
        "https://1.2.3.4/rpc", "b.example", "b.example", "https://b.example/rpc"
    )
    await mcp_client._open_session(server, first, {})  # type: ignore[arg-type]
    await mcp_client._open_session(server, second, {})  # type: ignore[arg-type]

    assert [request["method"] for request in server.requests].count("initialize") == 2


@pytest.mark.asyncio
async def test_request_ids_stay_unique_across_a_cached_session() -> None:
    server = _FakeClient([{"protocolVersion": "2025-06-18"}, {"tools": []}, {"tools": []}])
    first = await mcp_client._open_session(server, TARGET, {})  # type: ignore[arg-type]
    await first.request("tools/list")
    second = await mcp_client._open_session(server, TARGET, {})  # type: ignore[arg-type]
    await second.request("tools/list")

    ids = [request["id"] for request in server.requests if "id" in request]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_a_404_keeps_its_status_for_session_recovery() -> None:
    class _NotFound(_FakeClient):
        def stream(self, *_args: Any, **_kwargs: Any) -> _FakeStream:
            return _FakeStream(httpx.Response(404))

    session = mcp_client._Session(_NotFound([]), TARGET, {})  # type: ignore[arg-type]
    with pytest.raises(mcp_client.McpError) as caught:
        await session.request("tools/list")
    assert caught.value.status == 404


@pytest.mark.asyncio
async def test_a_blocked_initialize_does_not_serialize_other_servers() -> None:
    gate = asyncio.Event()

    class _BlockingStream:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        async def __aenter__(self) -> httpx.Response:
            await gate.wait()
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": self._payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"mcp-session-id": "slow"},
            )

        async def __aexit__(self, *_args: Any) -> None:
            pass

    class _BlockingClient(_FakeClient):
        def stream(self, _method: str, _url: str, **kwargs: Any) -> Any:
            payload = kwargs.get("json") or {}
            if payload.get("method") == "initialize":
                self.requests.append(payload)
                self.headers.append(kwargs.get("headers") or {})
                return _BlockingStream(payload)
            return super().stream(_method, _url, **kwargs)

    slow = _BlockingClient([])
    fast = _FakeClient([{"protocolVersion": "2025-06-18"}])
    slow_target = mcp_client._Target(
        "https://slow.example/rpc", "slow.example", "slow.example", "https://slow.example/rpc"
    )
    fast_target = mcp_client._Target(
        "https://fast.example/rpc", "fast.example", "fast.example", "https://fast.example/rpc"
    )

    blocked = asyncio.create_task(
        mcp_client._open_session(slow, slow_target, {}, scope="slow")  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    fast_session = await asyncio.wait_for(
        mcp_client._open_session(fast, fast_target, {}, scope="fast"),  # type: ignore[arg-type]
        timeout=1,
    )
    assert fast_session.session_id == "s-1"

    gate.set()
    await blocked


@pytest.mark.asyncio
async def test_a_racing_initialize_ends_the_losing_session() -> None:
    gate = asyncio.Event()
    both_initializing = asyncio.Event()

    class _GatedStream:
        def __init__(self, payload: dict[str, Any], session_id: str) -> None:
            self._payload = payload
            self._session_id = session_id

        async def __aenter__(self) -> httpx.Response:
            await gate.wait()
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": self._payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"mcp-session-id": self._session_id},
            )

        async def __aexit__(self, *_args: Any) -> None:
            pass

    class _RacingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.initialized = 0
            self.deleted: list[str] = []

        def stream(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            **_kwargs: Any,
        ) -> Any:
            if method == "DELETE":
                self.deleted.append((headers or {}).get("Mcp-Session-Id", ""))
                return _FakeStream(httpx.Response(204))
            assert json is not None
            self.requests.append(json)
            self.headers.append(headers or {})
            if json.get("method") == "initialize":
                self.initialized += 1
                if self.initialized == 2:
                    both_initializing.set()
                return _GatedStream(json, f"s-{self.initialized}")
            return _FakeStream(httpx.Response(202))

    client = _RacingClient()
    first = asyncio.create_task(
        mcp_client._open_session(client, TARGET, {}, scope="race")  # type: ignore[arg-type]
    )
    second = asyncio.create_task(
        mcp_client._open_session(client, TARGET, {}, scope="race")  # type: ignore[arg-type]
    )
    await asyncio.wait_for(both_initializing.wait(), timeout=1)
    gate.set()
    sessions = await asyncio.gather(first, second)

    winner = sessions[0].session_id
    assert winner == sessions[1].session_id
    assert [cached.session_id for cached in mcp_client._session_cache.values()] == [winner]
    loser = "s-2" if winner == "s-1" else "s-1"
    assert client.deleted == [loser]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "hang"])
async def test_a_failing_delete_does_not_fail_the_losing_caller(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_client, "_CLOSE_TIMEOUT_S", 0.05)
    gate = asyncio.Event()
    both_initializing = asyncio.Event()

    class _GatedStream:
        def __init__(self, payload: dict[str, Any], session_id: str) -> None:
            self._payload = payload
            self._session_id = session_id

        async def __aenter__(self) -> httpx.Response:
            await gate.wait()
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": self._payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"mcp-session-id": self._session_id},
            )

        async def __aexit__(self, *_args: Any) -> None:
            pass

    class _HangingStream:
        async def __aenter__(self) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def __aexit__(self, *_args: Any) -> None:
            pass

    class _RacingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.initialized = 0

        def stream(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            **_kwargs: Any,
        ) -> Any:
            if method == "DELETE":
                if failure == "connect":
                    raise httpx.ConnectError("refused")
                return _HangingStream()
            assert json is not None
            self.requests.append(json)
            self.headers.append(headers or {})
            if json.get("method") == "initialize":
                self.initialized += 1
                if self.initialized == 2:
                    both_initializing.set()
                return _GatedStream(json, f"s-{self.initialized}")
            return _FakeStream(httpx.Response(202))

    client = _RacingClient()
    first = asyncio.create_task(
        mcp_client._open_session(client, TARGET, {}, scope="race")  # type: ignore[arg-type]
    )
    second = asyncio.create_task(
        mcp_client._open_session(client, TARGET, {}, scope="race")  # type: ignore[arg-type]
    )
    await asyncio.wait_for(both_initializing.wait(), timeout=1)
    gate.set()
    sessions = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert sessions[0].session_id == sessions[1].session_id
    assert [cached.session_id for cached in mcp_client._session_cache.values()] == [
        sessions[0].session_id
    ]


@pytest.mark.asyncio
async def test_an_sse_answer_is_parsed() -> None:
    server = _FakeClient(
        [{"protocolVersion": "2025-06-18"}, {"tools": [{"name": "search"}]}],
        content_type="text/event-stream",
    )
    session = mcp_client._Session(server, TARGET, {})  # type: ignore[arg-type]
    await session.initialize()
    assert await session.request("tools/list") == {"tools": [{"name": "search"}]}


@pytest.mark.asyncio
async def test_an_error_frame_becomes_an_mcp_error() -> None:
    class _Failing(_FakeClient):
        def stream(self, *_args: Any, **kwargs: Any) -> _FakeStream:
            payload = kwargs.get("json") or {}
            if "id" not in payload:
                return _FakeStream(httpx.Response(202))
            return _FakeStream(
                httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32602, "message": "bad arguments"},
                    },
                )
            )

    session = mcp_client._Session(_Failing([]), TARGET, {})  # type: ignore[arg-type]
    with pytest.raises(mcp_client.McpError, match="bad arguments"):
        await session.initialize()


@pytest.mark.asyncio
async def test_a_public_address_is_rewritten_to_the_checked_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client, "_allowed_internal_hosts", lambda: set())
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (mcp_client.socket.AF_INET, mcp_client.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    target = await mcp_client._resolve_target("https://mcp.example:8443/rpc?x=1")
    assert target.url == "https://93.184.216.34:8443/rpc?x=1"
    assert target.host_header == "mcp.example:8443"
    assert target.sni_hostname == "mcp.example"


@pytest.mark.asyncio
async def test_a_private_address_is_refused_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client, "_allowed_internal_hosts", lambda: set())
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (mcp_client.socket.AF_INET, mcp_client.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))
        ],
    )
    with pytest.raises(mcp_client.McpError, match="not allowed"):
        await mcp_client._resolve_target("http://mcp.internal/rpc")


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["64:ff9b::a00:1", "::ffff:10.0.0.1"])
async def test_an_ipv6_wrapper_around_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(mcp_client, "_allowed_internal_hosts", lambda: set())
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (mcp_client.socket.AF_INET6, mcp_client.socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))
        ],
    )
    with pytest.raises(mcp_client.McpError, match="not allowed"):
        await mcp_client._resolve_target("http://mcp.internal/rpc")


@pytest.mark.asyncio
async def test_an_explicitly_allowed_host_may_reach_a_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client, "_allowed_internal_hosts", lambda: {"mcp.internal"})
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (mcp_client.socket.AF_INET, mcp_client.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))
        ],
    )
    target = await mcp_client._resolve_target("http://mcp.internal:8080/rpc")
    assert target.url == "http://10.0.0.5:8080/rpc"
    assert target.host_header == "mcp.internal:8080"


@pytest.mark.asyncio
async def test_metadata_is_refused_even_when_the_host_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client, "_allowed_internal_hosts", lambda: {"mcp.internal"})
    monkeypatch.setattr(
        mcp_client.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
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
        await mcp_client._resolve_target("http://mcp.internal/rpc")


@pytest.mark.asyncio
async def test_the_clients_ignore_proxy_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class _CaptureClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

        async def __aenter__(self) -> _CaptureClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

    async def _target(_url: str) -> mcp_client._Target:
        return TARGET

    async def _tools(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def _call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"content": "", "isError": False}

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", _CaptureClient)
    monkeypatch.setattr(mcp_client, "_resolve_target", _target)
    monkeypatch.setattr(mcp_client, "_with_session", _tools)
    await mcp_client.list_tools("https://mcp.example/rpc")
    monkeypatch.setattr(mcp_client, "_with_session", _call)
    await mcp_client.call_tool("https://mcp.example/rpc", "search", {})

    assert len(captured) == 2
    assert all(client.get("trust_env") is False for client in captured)


@pytest.mark.asyncio
async def test_a_slow_server_is_bounded_by_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _target(_url: str) -> mcp_client._Target:
        return TARGET

    async def _slow(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(mcp_client, "_CALL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(mcp_client, "_resolve_target", _target)
    monkeypatch.setattr(mcp_client, "_with_session", _slow)
    with pytest.raises(mcp_client.McpError, match="too long"):
        await mcp_client.list_tools("https://mcp.example/rpc")


@pytest.mark.asyncio
async def test_tool_listing_follows_every_page() -> None:
    class _Paged:
        def __init__(self) -> None:
            self.calls: list[dict[str, str] | None] = []

        async def request(
            self, _method: str, params: dict[str, str] | None = None
        ) -> dict[str, Any]:
            self.calls.append(params)
            if params is None:
                return {"tools": [{"name": "a"}], "nextCursor": "c1"}
            return {"tools": [{"name": "b"}]}

    session = _Paged()
    assert await mcp_client._list_all_tools(session) == [{"name": "a"}, {"name": "b"}]  # type: ignore[arg-type]
    assert session.calls == [None, {"cursor": "c1"}]


@pytest.mark.asyncio
async def test_a_response_past_the_limit_is_stopped_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client, "_MAX_BODY_BYTES", 5)

    class _Response:
        async def aiter_bytes(self):
            yield b"123"
            yield b"456789"

    with pytest.raises(mcp_client.McpError, match="too large"):
        await mcp_client._read_body(_Response())  # type: ignore[arg-type]


def test_structured_content_is_used_when_no_text_is_present() -> None:
    assert (
        mcp_client._flatten_result({"content": [], "structuredContent": {"temperature": 23}})
        == '{"temperature": 23}'
    )
    assert (
        mcp_client._flatten_result(
            {
                "content": [{"type": "text", "text": "23 C"}],
                "structuredContent": {"temperature": 23},
            }
        )
        == "23 C"
    )


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
    seen: dict[str, Any] = {}

    async def _tools(_url: str, _headers: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return [{"name": "search", "description": "Find pages", "inputSchema": {"type": "object"}}]

    original = mcp_client.list_tools
    mcp_client.list_tools = _tools  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/tools",
                json={"url": "https://mcp.example/rpc", "connection_id": "conn-1"},
            )
    finally:
        mcp_client.list_tools = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json()["tools"] == [
        {"name": "search", "description": "Find pages", "inputSchema": {"type": "object"}}
    ]
    assert seen["scope"] == "u-internal:conn-1"


@pytest.mark.asyncio
async def test_a_free_user_cannot_use_the_proxy() -> None:
    async with _client(FREE) as client:
        response = await client.post(
            "/internal/playground/mcp/tools",
            json={"url": "https://mcp.example/rpc", "connection_id": "conn-1"},
        )
        listed = await client.get("/internal/playground/mcp/servers")
    assert response.status_code == 403
    assert listed.status_code == 403


@pytest.mark.asyncio
async def test_a_tool_call_returns_the_flattened_result() -> None:
    async def _call(
        _url: str,
        _name: str,
        _arguments: dict[str, Any],
        _headers: Any = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {"content": "42", "isError": False}

    original = mcp_client.call_tool
    mcp_client.call_tool = _call  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/call",
                json={
                    "url": "https://mcp.example/rpc",
                    "connection_id": "conn-1",
                    "name": "search",
                    "arguments": {},
                },
            )
    finally:
        mcp_client.call_tool = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json() == {"content": "42", "isError": False}


@pytest.mark.asyncio
async def test_an_unreachable_server_is_a_bad_gateway() -> None:
    async def _tools(_url: str, _headers: Any = None, **_kwargs: Any) -> list[dict[str, Any]]:
        raise mcp_client.McpError("Could not reach the MCP server")

    original = mcp_client.list_tools
    mcp_client.list_tools = _tools  # type: ignore[assignment]
    try:
        async with _client(INTERNAL) as client:
            response = await client.post(
                "/internal/playground/mcp/tools",
                json={"url": "https://mcp.example/rpc", "connection_id": "conn-1"},
            )
    finally:
        mcp_client.list_tools = original  # type: ignore[assignment]

    assert response.status_code == 502
    assert response.json()["detail"] == "The MCP server request failed."


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


def _store_document():
    from serving.config.mcp_servers import McpServersDocument

    return McpServersDocument.model_validate(
        {
            "servers": [
                {
                    "id": "docs",
                    "name": "Docs search",
                    "description": "Find pages",
                    "url": "https://docs.example/mcp/secret?token=hidden",
                    "headers": {"Authorization": "${MCP_DOCS_TOKEN}"},
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_the_store_lists_servers_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground_mcp, "_catalog", _store_document)
    async with _client(INTERNAL) as client:
        response = await client.get("/internal/playground/mcp/servers")

    assert response.status_code == 200
    assert response.json() == {
        "servers": [
            {
                "id": "docs",
                "name": "Docs search",
                "description": "Find pages",
                "url": "https://docs.example",
                "docsUrl": "",
                "iconUrl": "",
            }
        ]
    }
    assert "MCP_DOCS_TOKEN" not in response.text


@pytest.mark.asyncio
async def test_a_store_server_resolves_its_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _tools(url: str, headers: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(url=url, headers=headers, **kwargs)
        return [{"name": "search"}]

    from serving.servers import registry

    monkeypatch.setattr(playground_mcp, "_catalog", _store_document)
    monkeypatch.setattr(registry, "resolve_reference", lambda name: "secret-value")
    monkeypatch.setattr(mcp_client, "list_tools", _tools)
    async with _client(INTERNAL) as client:
        response = await client.post(
            "/internal/playground/mcp/tools",
            json={"server_id": "docs", "connection_id": "conn-1"},
        )

    assert response.status_code == 200
    assert seen["url"] == "https://docs.example/mcp/secret?token=hidden"
    assert seen["headers"] == {"Authorization": "secret-value"}
    assert seen["scope"] == "u-internal:conn-1"


@pytest.mark.asyncio
async def test_a_store_server_with_an_unset_secret_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serving.servers import registry

    called = False

    async def _tools(_url: str, _headers: Any = None, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(playground_mcp, "_catalog", _store_document)
    monkeypatch.setattr(registry, "resolve_reference", lambda name: None)
    monkeypatch.setattr(mcp_client, "list_tools", _tools)
    async with _client(INTERNAL) as client:
        response = await client.post(
            "/internal/playground/mcp/tools",
            json={"server_id": "docs", "connection_id": "conn-1"},
        )

    assert response.status_code == 503
    assert called is False


@pytest.mark.asyncio
async def test_an_unknown_store_server_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground_mcp, "_catalog", _store_document)
    async with _client(INTERNAL) as client:
        response = await client.post(
            "/internal/playground/mcp/tools",
            json={"server_id": "missing", "connection_id": "conn-1"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_request_names_either_a_url_or_a_store_server() -> None:
    async with _client(INTERNAL) as client:
        response = await client.post(
            "/internal/playground/mcp/tools",
            json={
                "url": "https://mcp.example/rpc",
                "server_id": "docs",
                "connection_id": "conn-1",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_invalid_store_is_empty_and_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from serving.config import documents

    monkeypatch.setattr(documents, "get", lambda _kind: {"servers": [{"id": "Bad"}]})
    async with _client(INTERNAL) as client:
        listed = await client.get("/internal/playground/mcp/servers")
        tools = await client.post(
            "/internal/playground/mcp/tools",
            json={"server_id": "docs", "connection_id": "conn-1"},
        )

    assert listed.status_code == 200
    assert listed.json() == {"servers": []}
    assert tools.status_code == 503


@pytest.mark.asyncio
async def test_a_tool_call_by_server_id_resolves_the_store_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _call(
        url: str,
        name: str,
        arguments: dict[str, Any],
        headers: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen.update(url=url, name=name, headers=headers, **kwargs)
        return {"content": "42", "isError": False}

    from serving.servers import registry

    monkeypatch.setattr(playground_mcp, "_catalog", _store_document)
    monkeypatch.setattr(registry, "resolve_reference", lambda name: "secret-value")
    monkeypatch.setattr(mcp_client, "call_tool", _call)
    async with _client(INTERNAL) as client:
        response = await client.post(
            "/internal/playground/mcp/call",
            json={
                "server_id": "docs",
                "connection_id": "conn-1",
                "name": "search",
                "arguments": {},
            },
        )

    assert response.status_code == 200
    assert seen["url"] == "https://docs.example/mcp/secret?token=hidden"
    assert seen["headers"] == {"Authorization": "secret-value"}
