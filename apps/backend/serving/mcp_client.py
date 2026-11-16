"""Minimal Streamable HTTP client for an MCP server the playground points at.

Only the three calls a tools picker needs are implemented: ``initialize``,
``tools/list`` and ``tools/call``. There is no registry here: the caller
supplies the server URL and any auth headers, because the deployment's own MCP
registry and proxy moved to the cloud agent with the rest of that feature.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from serving.utils.metadata_addresses import METADATA_HOSTNAMES, is_metadata_address

_PROTOCOL_VERSION = "2025-06-18"
_TIMEOUT_S = 20.0
_DNS_TIMEOUT_S = 5.0
_MAX_BODY_BYTES = 2 * 1024 * 1024
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "content-type", "accept", "mcp-session-id", "connection"}
)


class McpError(Exception):
    """An MCP server could not be reached or answered with an error."""


def validate_mcp_url(url: str) -> str:
    """Return *url* if it is a usable http(s) URL, else raise :class:`McpError`."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise McpError("The MCP server URL is not valid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpError("The MCP server URL must be an http or https address.")
    host = parsed.hostname.rstrip(".").lower()
    if host in METADATA_HOSTNAMES:
        raise McpError("That MCP server address is not allowed.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if is_metadata_address(address):
        raise McpError("That MCP server address is not allowed.")
    return url


async def _refuse_metadata_resolution(url: str) -> None:
    host = urlparse(url).hostname
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        resolved = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM),
            timeout=_DNS_TIMEOUT_S,
        )
    except (TimeoutError, socket.gaierror) as exc:
        raise McpError("The MCP server host could not be resolved.") from exc
    for result in resolved:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError:
            raise McpError("The MCP server resolved to an invalid address.") from None
        if is_metadata_address(address):
            raise McpError("That MCP server address is not allowed.")


def _clean_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _FORBIDDEN_HEADERS
    }


def _parse_frames(response: httpx.Response) -> list[dict[str, Any]]:
    if len(response.content) > _MAX_BODY_BYTES:
        raise McpError("The MCP server response was too large.")
    if "text/event-stream" in response.headers.get("content-type", ""):
        return _parse_sse(response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise McpError("The MCP server returned an unreadable response.") from exc
    return [payload] if isinstance(payload, dict) else []


def _parse_sse(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    data: list[str] = []
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line.strip() or not data:
            continue
        try:
            frame = json.loads("\n".join(data))
        except ValueError:
            frame = None
        data = []
        if isinstance(frame, dict):
            frames.append(frame)
    if data:
        try:
            frame = json.loads("\n".join(data))
        except ValueError:
            frame = None
        if isinstance(frame, dict):
            frames.append(frame)
    return frames


def _result_for(frames: list[dict[str, Any]], request_id: int) -> Any:
    for frame in frames:
        if frame.get("id") != request_id:
            continue
        error = frame.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "The MCP server reported an error."
            raise McpError(str(message))
        if "result" in frame:
            return frame["result"]
    raise McpError("The MCP server did not answer the request.")


class _Session:
    def __init__(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
        self._client = client
        self._url = url
        self._headers = headers
        self._session_id: str | None = None
        self._next_id = 0

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = await self._client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"Could not reach the MCP server: {exc}") from exc
        if response.status_code >= 400:
            raise McpError(f"The MCP server refused the request ({response.status_code}).")
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return response

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = await self._post(payload)
        return _result_for(_parse_frames(response), request_id)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload)

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hybridinference-playground", "version": "1.0"},
            },
        )
        await self.notify("notifications/initialized")


async def list_tools(url: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return the tool definitions an MCP server publishes."""
    validate_mcp_url(url)
    await _refuse_metadata_resolution(url)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
        session = _Session(client, url, _clean_headers(headers))
        await session.initialize()
        result = await session.request("tools/list")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]


async def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke one MCP tool and flatten its result into text for the transcript."""
    validate_mcp_url(url)
    await _refuse_metadata_resolution(url)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as client:
        session = _Session(client, url, _clean_headers(headers))
        await session.initialize()
        result = await session.request("tools/call", {"name": name, "arguments": arguments})
    if not isinstance(result, dict):
        return {"content": "", "isError": False}
    return {
        "content": _flatten_content(result.get("content")),
        "isError": bool(result.get("isError")),
    }


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        else:
            parts.append(json.dumps(part))
    return "\n".join(parts)
