"""Playground endpoints that proxy the three MCP calls a tools picker needs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from serving import mcp_client
from serving.servers.deps import require_role
from serving.servers.request_body import json_body_after, json_body_schema
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/playground/mcp", tags=["Playground"])

_require_internal = require_role("internal")


class McpServerBody(BaseModel):
    """Connection details for one user-supplied MCP server."""

    url: str = Field(min_length=1, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)


class McpCallBody(McpServerBody):
    """One tool invocation on that server."""

    name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)


_tools_body = json_body_after(_require_internal, McpServerBody)
_call_body = json_body_after(_require_internal, McpCallBody)


def _refuse(exc: mcp_client.McpError) -> HTTPException:
    """Log the MCP failure and return the fixed message a client may see."""
    logger.warning("Playground MCP request failed", exc_info=exc)
    return HTTPException(502, "The MCP server request failed.")


@router.post(
    "/tools",
    response_model=None,
    openapi_extra=json_body_schema(McpServerBody),
)
async def list_tools(
    _caller: dict[str, Any] = Depends(_require_internal),
    body: McpServerBody = Depends(_tools_body),
) -> dict[str, Any]:
    """Return the tools an existing MCP server publishes."""
    try:
        tools = await mcp_client.list_tools(body.url, body.headers)
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
    _caller: dict[str, Any] = Depends(_require_internal),
    body: McpCallBody = Depends(_call_body),
) -> dict[str, Any]:
    """Invoke one tool on an existing MCP server."""
    try:
        return await mcp_client.call_tool(body.url, body.name, body.arguments, body.headers)
    except mcp_client.McpError as exc:
        raise _refuse(exc) from exc
