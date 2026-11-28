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
            "headers": {"Authorization": "${MCP_DOCS_TOKEN}"},
            "docs_url": "https://docs.example",
            "icon_url": "",
        }
    ]
}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch):
    async def _update(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _refresh() -> None:
        return None

    monkeypatch.setattr(mcp_servers.documents, "get", lambda _kind: BASE)
    monkeypatch.setattr(mcp_servers.app_config, "update", _update)
    monkeypatch.setattr(mcp_servers.app_config, "refresh", _refresh)
    monkeypatch.setattr(mcp_servers.app_config, "validation_problem", lambda _key: None)


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
    assert response.json()["servers"][0]["headers"] == {"Authorization": "${MCP_DOCS_TOKEN}"}
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
                "headers": {"Authorization": "${MCP_DOCS_TOKEN}"},
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


@pytest.mark.asyncio
async def test_refuses_a_header_that_is_not_an_mcp_secret() -> None:
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={
                "id": "wiki",
                "name": "Wiki",
                "url": "https://wiki.example/mcp",
                "headers": {"Authorization": "${GITHUB_TOKEN}"},
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_the_audit_row_records_where_an_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _log(*_args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(mcp_servers, "log_admin_action", _log)
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={
                "id": "wiki",
                "name": "Wiki",
                "url": "https://wiki.example/mcp",
                "headers": {"Authorization": "${MCP_WIKI_TOKEN}"},
            },
        )

    assert response.status_code == 201
    assert seen["details"] == {
        "id": "wiki",
        "name": "Wiki",
        "url": "https://wiki.example/mcp",
        "headers": ["Authorization"],
    }


@pytest.mark.asyncio
async def test_edits_are_conditional_on_the_document_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _update(*_args: Any, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(mcp_servers.app_config, "source_version", lambda: "v-before")
    monkeypatch.setattr(mcp_servers.app_config, "update", _update)
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={"id": "wiki", "name": "Wiki", "url": "https://wiki.example/mcp"},
        )

    assert response.status_code == 201
    assert seen["expected_version"] == "v-before"


@pytest.mark.asyncio
async def test_refuses_to_overwrite_an_invalid_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_servers.app_config, "validation_problem", lambda _key: "/headers")
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={"id": "wiki", "name": "Wiki", "url": "https://wiki.example/mcp"},
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_a_full_store_is_a_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    full = {"servers": [{**BASE["servers"][0], "id": f"s{i}"} for i in range(200)]}
    monkeypatch.setattr(mcp_servers.documents, "get", lambda _kind: full)
    async with _client() as client:
        response = await client.post(
            "/admin/mcp-servers",
            json={"id": "wiki", "name": "Wiki", "url": "https://wiki.example/mcp"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_non_admins_cannot_write_the_store() -> None:
    from fastapi import HTTPException

    app = FastAPI()
    app.include_router(mcp_servers.router)

    def _refuse() -> None:
        raise HTTPException(403, "Administrator access required")

    app.dependency_overrides[verify_admin_access] = _refuse
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        add = await client.post(
            "/admin/mcp-servers",
            json={"id": "wiki", "name": "Wiki", "url": "https://wiki.example/mcp"},
        )
        change = await client.put(
            "/admin/mcp-servers/docs",
            json={"id": "docs", "name": "Docs", "url": "https://docs.example/mcp"},
        )
        remove = await client.delete("/admin/mcp-servers/docs")

    assert add.status_code == 403
    assert change.status_code == 403
    assert remove.status_code == 403
