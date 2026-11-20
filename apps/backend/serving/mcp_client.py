"""Minimal Streamable HTTP client for an MCP server the playground points at.

Only the three calls a tools picker needs are implemented: ``initialize``,
``tools/list`` and ``tools/call``. There is no registry here: the caller
supplies the server URL and any auth headers, because the deployment's own MCP
registry and proxy moved to the cloud agent with the rest of that feature.

The caller-supplied URL is checked before it is used, and the check decides the
address the socket is opened to: the host is resolved here, the URL is rebuilt
around the chosen address, and the original name travels in ``Host`` and, for
TLS, in ``sni_hostname``. A name that resolves differently between the check and
the connection therefore cannot redirect the request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from serving.config.settings import get_settings
from serving.utils.ip_classification import is_public_unicast
from serving.utils.metadata_addresses import METADATA_HOSTNAMES, is_metadata_address

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_PROTOCOL_VERSION = "2025-06-18"
_TIMEOUT_S = 20.0
_DNS_TIMEOUT_S = 5.0
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_TOOL_PAGES = 100
_MAX_TOOLS = 5000
_SESSION_TTL_S = 600.0
_CALL_TIMEOUT_S = 60.0
_CLOSE_TIMEOUT_S = 2.0
_FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "accept",
        "mcp-session-id",
        "mcp-protocol-version",
        "connection",
    }
)


class McpError(Exception):
    """An MCP server could not be reached or answered with an error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class _Target:
    """The address actually connected to, and the identity the server sees."""

    url: str
    host_header: str
    sni_hostname: str | None
    identity: str = ""


def _allowed_internal_hosts() -> set[str]:
    raw = get_settings().playground_mcp_allowed_hosts
    return {host.strip().rstrip(".").lower() for host in raw.split(",") if host.strip()}


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise McpError("The MCP server URL is not valid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpError("The MCP server URL must be an http or https address.")
    if parsed.username or parsed.password:
        raise McpError("The MCP server URL must not include credentials.")


async def _resolve_target(url: str) -> _Target:
    """Pick an allowed address and rebuild the URL around it."""
    _validate_url(url)
    parsed = urlparse(url)
    host = parsed.hostname.rstrip(".").lower()
    if host in METADATA_HOSTNAMES:
        raise McpError("That MCP server address is not allowed.")
    allow_private = host in _allowed_internal_hosts()

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            resolved = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM),
                timeout=_DNS_TIMEOUT_S,
            )
        except (TimeoutError, socket.gaierror) as exc:
            raise McpError("The MCP server host could not be resolved.") from exc
        addresses = []
        for result in resolved:
            try:
                addresses.append(ipaddress.ip_address(result[4][0]))
            except ValueError as exc:
                raise McpError("The MCP server resolved to an invalid address.") from exc

    chosen = next(
        (
            address
            for address in addresses
            if not is_metadata_address(address) and (allow_private or is_public_unicast(address))
        ),
        None,
    )
    if chosen is None:
        raise McpError("That MCP server address is not allowed.")

    literal = f"[{chosen}]" if chosen.version == 6 else str(chosen)
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = f"{host}:{parsed.port}" if parsed.port else host
    return _Target(
        url=f"{parsed.scheme}://{netloc}{path}",
        host_header=host_header,
        sni_hostname=host if parsed.scheme == "https" else None,
        identity=f"{parsed.scheme}://{host}{f':{parsed.port}' if parsed.port else ''}{path}",
    )


def _clean_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _FORBIDDEN_HEADERS
    }


async def _read_body(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise McpError("The MCP server response was too large.")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _parse_frames(body: str, content_type: str) -> list[dict[str, Any]]:
    if "text/event-stream" in content_type:
        return _parse_sse(body)
    try:
        payload = json.loads(body)
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


def _result_for(frames: list[dict[str, Any]], request_id: str) -> Any:
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
    def __init__(
        self,
        client: httpx.AsyncClient,
        target: _Target,
        headers: dict[str, str],
        *,
        session_id: str | None = None,
        protocol_version: str = _PROTOCOL_VERSION,
    ) -> None:
        self._client = client
        self._target = target
        self._headers = headers
        self._session_id = session_id
        self._protocol_version = protocol_version

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": self._protocol_version,
            "Host": self._target.host_header,
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @property
    def _extensions(self) -> dict[str, str]:
        return {"sni_hostname": self._target.sni_hostname} if self._target.sni_hostname else {}

    async def close(self) -> None:
        """End this session on the server, best effort."""
        if self._session_id is None:
            return
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                async with self._client.stream(
                    "DELETE",
                    self._target.url,
                    headers=self._request_headers(),
                    extensions=self._extensions,
                ):
                    pass
        except (httpx.HTTPError, TimeoutError):
            pass

    async def _post(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"Content-Type": "application/json", **self._request_headers()}
        extensions = self._extensions
        try:
            async with self._client.stream(
                "POST",
                self._target.url,
                json=payload,
                headers=headers,
                extensions=extensions,
            ) as response:
                if response.status_code >= 400:
                    raise McpError(
                        f"The MCP server refused the request ({response.status_code}).",
                        status=response.status_code,
                    )
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id
                if "id" not in payload:
                    return []
                body = await _read_body(response)
                return _parse_frames(body, response.headers.get("content-type", ""))
        except httpx.HTTPError as exc:
            raise McpError(f"Could not reach the MCP server: {exc}") from exc

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = uuid4().hex
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        return _result_for(await self._post(payload), request_id)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload)

    async def initialize(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hybridinference-playground", "version": "1.0"},
            },
        )
        if isinstance(result, dict):
            negotiated = result.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated:
                self._protocol_version = negotiated
        await self.notify("notifications/initialized")


@dataclass
class _CachedSession:
    session_id: str
    protocol_version: str
    last_used: float


_SessionKey = tuple[str, tuple[tuple[str, str], ...]]
_session_cache: dict[_SessionKey, _CachedSession] = {}
_init_lock = asyncio.Lock()


def _session_key(scope: str, target: _Target, headers: dict[str, str]) -> _SessionKey:
    return (scope, target.identity or target.url, tuple(sorted(headers.items())))


def _cached_session(key: _SessionKey) -> _CachedSession | None:
    """The live cache entry for *key*, pruning expired ones on the way."""
    now = time.monotonic()
    for stale in [
        cached_key
        for cached_key, cached in _session_cache.items()
        if now - cached.last_used > _SESSION_TTL_S
    ]:
        _session_cache.pop(stale, None)
    cached = _session_cache.get(key)
    if cached is not None:
        cached.last_used = now
    return cached


async def _open_session(
    client: httpx.AsyncClient,
    target: _Target,
    headers: dict[str, str],
    *,
    scope: str = "",
) -> _Session:
    """Reuse a server's session, or initialize one and remember it."""
    key = _session_key(scope, target, headers)
    async with _init_lock:
        cached = _cached_session(key)
    if cached is not None:
        return _Session(
            client,
            target,
            headers,
            session_id=cached.session_id,
            protocol_version=cached.protocol_version,
        )

    session = _Session(client, target, headers)
    await session.initialize()
    async with _init_lock:
        raced = _cached_session(key)
        if raced is None and session.session_id:
            _session_cache[key] = _CachedSession(
                session.session_id, session.protocol_version, time.monotonic()
            )
    if raced is not None:
        await session.close()
        return _Session(
            client,
            target,
            headers,
            session_id=raced.session_id,
            protocol_version=raced.protocol_version,
        )
    return session


async def _with_session(
    client: httpx.AsyncClient,
    target: _Target,
    headers: dict[str, str],
    action: Callable[[_Session], Awaitable[Any]],
    *,
    scope: str = "",
) -> Any:
    """Run *action* on a cached session, retrying once if the server forgot it."""
    key = _session_key(scope, target, headers)
    session = await _open_session(client, target, headers, scope=scope)
    try:
        return await action(session)
    except McpError as exc:
        if exc.status != 404 or session.session_id is None:
            raise
        cached = _session_cache.get(key)
        if cached is not None and cached.session_id == session.session_id:
            _session_cache.pop(key, None)
        session = await _open_session(client, target, headers, scope=scope)
        return await action(session)


async def list_tools(
    url: str, headers: dict[str, str] | None = None, *, scope: str = ""
) -> list[dict[str, Any]]:
    """Return every tool definition a paginated MCP server publishes."""
    target = await _resolve_target(url)
    clean = _clean_headers(headers)
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S, follow_redirects=False, trust_env=False
    ) as client:
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                return await _with_session(client, target, clean, _list_all_tools, scope=scope)
        except TimeoutError as exc:
            raise McpError("The MCP server took too long to answer.") from exc


async def _list_all_tools(session: _Session) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(_MAX_TOOL_PAGES):
        result = await session.request("tools/list", {"cursor": cursor} if cursor else None)
        if not isinstance(result, dict):
            break
        batch = result.get("tools")
        if isinstance(batch, list):
            tools.extend(
                tool
                for tool in batch
                if isinstance(tool, dict) and isinstance(tool.get("name"), str)
            )
        if len(tools) >= _MAX_TOOLS:
            break
        next_cursor = result.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    return tools[:_MAX_TOOLS]


async def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
    *,
    scope: str = "",
) -> dict[str, Any]:
    """Invoke one MCP tool and flatten its result into text for the transcript."""
    target = await _resolve_target(url)
    clean = _clean_headers(headers)

    async def _call(session: _Session) -> Any:
        return await session.request("tools/call", {"name": name, "arguments": arguments})

    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S, follow_redirects=False, trust_env=False
    ) as client:
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                result = await _with_session(client, target, clean, _call, scope=scope)
        except TimeoutError as exc:
            raise McpError("The MCP server took too long to answer.") from exc
    return {
        "content": _flatten_result(result),
        "isError": bool(result.get("isError")) if isinstance(result, dict) else False,
    }


def _flatten_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    text = _flatten_content(result.get("content"))
    if text:
        return text
    structured = result.get("structuredContent")
    return "" if structured is None else json.dumps(structured)


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
