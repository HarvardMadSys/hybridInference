"""Playground endpoints that proxy the three MCP calls a tools picker needs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from serving import mcp_client
from serving.config.mcp_servers import McpServerEntry, McpServersDocument
from serving.servers.deps import require_role
from serving.servers.request_body import json_body_after, json_body_schema
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/playground/mcp", tags=["Playground"])

_require_internal = require_role("internal")


class McpServerBody(BaseModel):
    """Connection details for one MCP server, by URL or by store entry."""

    url: str = Field(default="", max_length=2048)
    server_id: str = Field(default="", max_length=64)
    headers: dict[str, str] = Field(default_factory=dict)
    connection_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _one_target(self) -> McpServerBody:
        if bool(self.url) == bool(self.server_id):
            raise ValueError("provide exactly one of url or server_id")
        return self


class McpCallBody(McpServerBody):
    """One tool invocation on that server."""

    name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)


_tools_body = json_body_after(_require_internal, McpServerBody)
_call_body = json_body_after(_require_internal, McpCallBody)


def _catalog() -> McpServersDocument | None:
    from serving.config import documents

    try:
        return McpServersDocument.model_validate(documents.get("mcp"))
    except ValidationError as exc:
        logger.warning("MCP server store is invalid", exc_info=exc)
        return None


def _store_entry(server_id: str) -> McpServerEntry:
    catalog = _catalog()
    if catalog is None:
        raise HTTPException(503, "The MCP server store is unavailable.")
    for entry in catalog.servers:
        if entry.id == server_id:
            return entry
    raise HTTPException(404, "That MCP server is not in the store.")


def _resolved_headers(entry: McpServerEntry) -> dict[str, str]:
    from serving.servers.registry import resolve_reference

    resolved: dict[str, str] = {}
    for name, value in entry.headers.items():
        if value.startswith("${") and value.endswith("}"):
            value = resolve_reference(value[2:-1]) or ""
        if not value:
            raise HTTPException(503, "That MCP server is not configured.")
        resolved[name] = value
    return resolved


def _target(body: McpServerBody) -> tuple[str, dict[str, str]]:
    if not body.server_id:
        return body.url, body.headers
    entry = _store_entry(body.server_id)
    return entry.url, _resolved_headers(entry)


def _refuse(exc: mcp_client.McpError) -> HTTPException:
    """Log the MCP failure and return the fixed message a client may see."""
    logger.warning("Playground MCP request failed", exc_info=exc)
    return HTTPException(502, "The MCP server request failed.")


@router.get("/servers", response_model=None)
async def list_servers(
    _caller: dict[str, Any] = Depends(_require_internal),
) -> dict[str, Any]:
    """Return the store's servers without their credentials. An invalid document is empty."""
    catalog = _catalog()
    if catalog is None:
        return {"servers": []}
    return {
        "servers": [
            {
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "url": entry.url,
                "docsUrl": entry.docs_url,
                "iconUrl": entry.icon_url,
            }
            for entry in catalog.servers
        ]
    }


@router.post(
    "/tools",
    response_model=None,
    openapi_extra=json_body_schema(McpServerBody),
)
async def list_tools(
    caller: dict[str, Any] = Depends(_require_internal),
    body: McpServerBody = Depends(_tools_body),
) -> dict[str, Any]:
    """Return the tools an existing MCP server publishes."""
    url, headers = _target(body)
    scope = f"{caller.get('user_id', '')}:{body.connection_id}"
    try:
        tools = await mcp_client.list_tools(url, headers, scope=scope)
    except mcp_client.McpError as exc:
        raise _refuse(exc) from exc
    return {
        "tools": [
            {
                "name": tool["name"],
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]
    }


@router.post(
    "/call",
    response_model=None,
    openapi_extra=json_body_schema(McpCallBody),
)
async def call_tool(
    caller: dict[str, Any] = Depends(_require_internal),
    body: McpCallBody = Depends(_call_body),
) -> dict[str, Any]:
    """Invoke one tool on an existing MCP server."""
    url, headers = _target(body)
    scope = f"{caller.get('user_id', '')}:{body.connection_id}"
    try:
        return await mcp_client.call_tool(url, body.name, body.arguments, headers, scope=scope)
    except mcp_client.McpError as exc:
        raise _refuse(exc) from exc
