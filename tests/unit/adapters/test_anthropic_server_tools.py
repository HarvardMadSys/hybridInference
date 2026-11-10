"""The native Anthropic passthrough refuses tool work it cannot bill.

``/v1/messages`` on a native Anthropic route forwards the caller's body and
``anthropic-beta`` header. Server tools -- web search, web fetch, code
execution, tool search, the advisor, MCP connectors, Agent Skills -- run on the
provider's servers and are billed per use on top of tokens, and none of it
reached the gateway's billing, which reads only input and output tokens. Such a
request is now a 400 before anything is sent. Tools the client runs itself
(custom tools, bash, the text editor, memory, computer use) cost only tokens and
pass as before.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from serving.adapters.anthropic import AnthropicAdapter, ServerToolRefused, refuse_server_tools
from serving.adapters.base import ModelConfig
from serving.exceptions import scrub_error_for_user
from serving.http import AsyncHTTPClient

pytestmark = pytest.mark.unit


def _cfg() -> ModelConfig:
    return ModelConfig(
        id="claude-x",
        name="Claude X",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        provider_model_id="claude-x",
    )


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "model": "claude-x",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }


@pytest.fixture
def upstream(monkeypatch):
    """Record every upstream call; answer each with a minimal message."""
    sent: list[dict[str, Any]] = []

    async def json_post_with_retry(self, url, json=None, headers=None, **_kw):
        sent.append({"json": json, "headers": headers})
        return {"type": "message", "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

    class _Content:
        async def iter_any(self):
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    class _Resp:
        status = 200
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.append({"json": json, "headers": headers})
            return _Resp()

    async def ensure_session(self):
        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", json_post_with_retry)
    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", ensure_session)
    return sent


async def _call(stream: bool, body: dict[str, Any], headers: dict[str, str] | None = None):
    adapter = AnthropicAdapter(_cfg())
    if stream:
        return [
            chunk
            async for chunk in adapter.stream_messages(
                {**body, "stream": True}, request_id="r", extra_headers=headers
            )
        ]
    return await adapter.messages(body, request_id="r", extra_headers=headers)


SERVER_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
    {"type": "code_execution_20260521", "name": "code_execution"},
    {"type": "code_execution_20250825", "name": "code_execution"},
    {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
    {"type": "advisor_20260301", "name": "advisor", "model": "claude-x"},
    {"type": "mcp_toolset", "mcp_server_name": "docs"},
    # A server tool that does not exist yet is refused too.
    {"type": "browser_20990101", "name": "browser"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["messages", "stream_messages"])
@pytest.mark.parametrize("tool", SERVER_TOOLS, ids=lambda tool: tool["type"])
async def test_a_server_tool_is_refused_before_anything_is_sent(upstream, stream, tool):
    custom = {"name": "lookup", "description": "d", "input_schema": {"type": "object"}}

    with pytest.raises(ServerToolRefused) as refused:
        await _call(stream, _body(tools=[custom, tool]))

    assert refused.value.status == 400
    assert f"tools.1: the server tool {tool['type']!r}" in refused.value.message
    assert upstream == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["messages", "stream_messages"])
@pytest.mark.parametrize(
    "extra",
    [
        {"mcp_servers": [{"type": "url", "url": "https://mcp.example/sse", "name": "docs"}]},
        {"container": {"skills": [{"type": "anthropic", "skill_id": "pptx"}]}},
    ],
    ids=["mcp_servers", "container"],
)
async def test_mcp_servers_and_containers_are_refused(upstream, stream, extra):
    with pytest.raises(ServerToolRefused, match=f"`{next(iter(extra))}`"):
        await _call(stream, _body(**extra))

    assert upstream == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["messages", "stream_messages"])
@pytest.mark.parametrize(
    "beta",
    [
        "mcp-client-2025-11-20",
        "code-execution-2025-08-25",
        "web-fetch-2025-09-10",
        "advisor-tool-2026-03-01",
        "skills-2025-10-02",
    ],
)
async def test_a_server_tool_beta_is_refused(upstream, stream, beta):
    headers = {"anthropic-beta": f"interleaved-thinking-2025-05-14, {beta}"}

    with pytest.raises(ServerToolRefused, match=beta):
        await _call(stream, _body(), headers)

    assert upstream == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["messages", "stream_messages"])
async def test_client_tools_and_other_betas_go_through_unchanged(upstream, stream):
    tools = [
        {"name": "lookup", "description": "d", "input_schema": {"type": "object"}},
        {"type": "custom", "name": "other", "input_schema": {"type": "object"}},
        {"type": "bash_20250124", "name": "bash"},
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
        {"type": "memory_20250818", "name": "memory"},
        {"type": "computer_toolset_20260801"},
        {"type": "computer_20251124", "name": "computer", "display_width_px": 1024},
    ]
    beta = "claude-code-20250219,interleaved-thinking-2025-05-14,context-management-2025-06-27"

    await _call(stream, _body(tools=tools), {"anthropic-beta": beta})

    (sent,) = upstream
    assert sent["json"]["tools"] == tools
    assert sent["headers"]["anthropic-beta"] == beta


def test_the_refusal_reaches_the_caller_as_an_invalid_request():
    """``/v1/messages`` maps the status and keeps the message; the breaker skips a 400."""
    from serving.servers.routers.anthropic_messages import _map_upstream_status

    with pytest.raises(ServerToolRefused) as refused:
        refuse_server_tools(_body(tools=[{"type": "web_search_20260209"}]), None)

    assert isinstance(refused.value, aiohttp.ClientResponseError)
    assert _map_upstream_status(refused.value.status) == (400, "invalid_request_error")
    message = scrub_error_for_user(refused.value, "req_1", 400)
    assert message.startswith("tools.0: the server tool 'web_search_20260209' is not available")
    assert str(refused.value).startswith("400, message=")
