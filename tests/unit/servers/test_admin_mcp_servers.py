"""Admin CRUD for the Playground MCP server store."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.audit import AuditActor
from serving.servers.deps import (
    get_admin_audit_actor,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin import mcp_servers

BASE: dict[str, Any] = {
    "servers": [
        {
            "id": "docs",
            "name": "Docs",
            "description": "Find pages",
            "url": "https://docs.example/mcp",
            "headers": {"Authorization": "${DOCS_TOKEN}"},
            "docs_url": "https://docs.example",
            "icon_url": "",
        }
    ]
}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch):
    async def _update(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(mcp_servers.documents, "get", lambda _kind: BASE)
    monkeypatch.setattr(mcp_servers.app_config, "update", _update)


def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(mcp_servers.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin"
    app.dependency_overrides[get_admin_audit_actor] = lambda: AuditActor("admin", "u-admin")
    app.dependency_overrides[get_operational_store] = lambda: None
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_lists_the_store_with_header_references() -> None:
    async with _client() as client:
        response = await client.get("/admin/mcp-servers")

    assert response.status_code == 200
    assert response.json()["servers"][0]["headers"] == {"Authorization": "${DOCS_TOKEN}"}
    assert response.json()["servers"][0]["id"] == "docs"


@pytest.mark.asyncio
async def test_adds_a_server() -> None:
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={
                "id": "wiki",
                "name": "Wiki",
                "url": "https://wiki.example/mcp",
                "headers": {},
            },
        )

    assert response.status_code == 201
    assert [server["id"] for server in response.json()["servers"]] == ["docs", "wiki"]


@pytest.mark.asyncio
async def test_refuses_a_duplicate_id() -> None:
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={"id": "docs", "name": "Other", "url": "https://other.example/mcp"},
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_replaces_and_removes_a_server() -> None:
    async with _client() as client:
        updated = await client.put(
            "/admin/mcp-servers/docs",
            json={
                "id": "docs",
                "name": "Docs 2",
                "description": "Find pages",
                "url": "https://docs.example/mcp",
                "headers": {"Authorization": "${DOCS_TOKEN}"},
            },
        )
        removed = await client.delete("/admin/mcp-servers/docs")

    assert updated.status_code == 200
    assert updated.json()["servers"][0]["name"] == "Docs 2"
    assert removed.status_code == 200
    assert removed.json()["servers"] == []


@pytest.mark.asyncio
async def test_refuses_an_unknown_server() -> None:
    async with _client() as client:
        response = await client.delete("/admin/mcp-servers/missing")

    assert response.status_code == 404
