"""Admin endpoints for the Playground MCP server store."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from serving.audit import AuditActor, AuditTarget
from serving.config import app_config, documents
from serving.config.mcp_servers import McpServerEntry, McpServersDocument
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_admin_audit_actor, get_operational_store, verify_admin_access

if TYPE_CHECKING:
    from serving.config.revisions import SourceVersion

router = APIRouter(prefix="/admin")


def _read() -> tuple[McpServersDocument, SourceVersion | None]:
    """Read the catalog and the version the edit must replace.

    The version is captured before the document, so a concurrent save makes
    this write conflict rather than silently overwriting the other edit.
    """
    version = app_config.source_version()
    try:
        return McpServersDocument.model_validate(documents.get("mcp")), version
    except ValidationError as exc:
        raise HTTPException(400, documents.describe_validation_error(exc)) from None


def _response(document: McpServersDocument) -> dict[str, Any]:
    return {"servers": [entry.model_dump() for entry in document.servers]}


async def _save(
    document: McpServersDocument,
    *,
    admin_id: str,
    actor: AuditActor,
    op_store: Any,
    action: str,
    entry: McpServerEntry,
    expected_version: SourceVersion | None,
) -> dict[str, Any]:
    try:
        await app_config.update(
            {"MCP_SERVERS": json.dumps(document.model_dump(), sort_keys=True)},
            updated_by=admin_id,
            expected_version=expected_version,
        )
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    await log_admin_action(
        op_store,
        action=action,
        details={"id": entry.id, "name": entry.name},
        actor=actor,
        target=AuditTarget("mcp_server", entry.id),
    )
    return _response(document)


@router.get("/mcp-servers", response_model=None)
async def list_mcp_servers(
    _admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Return the store catalog, including header references but never their values."""
    return _response(_read()[0])


@router.post("/mcp-servers", response_model=None, status_code=201)
async def create_mcp_server(
    payload: McpServerEntry,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Add one server to the store."""
    document, version = _read()
    if any(entry.id == payload.id for entry in document.servers):
        raise HTTPException(409, "A server with that id already exists.")
    updated = McpServersDocument(servers=[*document.servers, payload])
    return await _save(
        updated,
        admin_id=admin_id,
        actor=actor,
        op_store=op_store,
        action="mcp_server.add",
        entry=payload,
        expected_version=version,
    )


@router.put("/mcp-servers/{server_id}", response_model=None)
async def update_mcp_server(
    server_id: str,
    payload: McpServerEntry,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Replace one server's entry; the id in the path is the key that stays."""
    if payload.id != server_id:
        raise HTTPException(400, "The server id cannot change.")
    document, version = _read()
    if not any(entry.id == server_id for entry in document.servers):
        raise HTTPException(404, "That server is not in the store.")
    updated = McpServersDocument(
        servers=[payload if entry.id == server_id else entry for entry in document.servers]
    )
    return await _save(
        updated,
        admin_id=admin_id,
        actor=actor,
        op_store=op_store,
        action="mcp_server.update",
        entry=payload,
        expected_version=version,
    )


@router.delete("/mcp-servers/{server_id}", response_model=None)
async def delete_mcp_server(
    server_id: str,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Remove one server from the store."""
    document, version = _read()
    if not any(entry.id == server_id for entry in document.servers):
        raise HTTPException(404, "That server is not in the store.")
    entry = next(entry for entry in document.servers if entry.id == server_id)
    updated = McpServersDocument(
        servers=[entry for entry in document.servers if entry.id != server_id]
    )
    return await _save(
        updated,
        admin_id=admin_id,
        actor=actor,
        op_store=op_store,
        action="mcp_server.remove",
        entry=entry,
        expected_version=version,
    )
