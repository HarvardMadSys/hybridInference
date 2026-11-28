"""The MCP store's behaviour against real ``app_config`` state."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.audit import AuditActor
from serving.config import app_config
from serving.servers.deps import (
    get_admin_audit_actor,
    get_current_user,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers import playground_mcp
from serving.servers.routers.admin import mcp_servers
from tests.fixtures.app_config_store import FakeAppConfigStore, row

INTERNAL = {"user_id": "u-internal", "role": "internal", "is_admin": False}


@pytest.fixture(autouse=True)
def configuration():
    app_config.reset_state()
    yield
    app_config.reset_state()


async def _load(rows=()) -> None:
    store = FakeAppConfigStore(list(rows))
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)


def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(mcp_servers.router)
    app.include_router(playground_mcp.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin"
    app.dependency_overrides[get_admin_audit_actor] = lambda: AuditActor("admin", "u-admin")
    app.dependency_overrides[get_operational_store] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: INTERNAL
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_an_old_form_store_is_not_overwritten_or_served() -> None:
    await _load(
        [
            row(
                "MCP_SERVERS",
                json.dumps(
                    {
                        "servers": [
                            {
                                "id": "docs",
                                "name": "Docs",
                                "url": "https://docs.example/mcp",
                                "headers": {"Authorization": "${OLD_TOKEN}"},
                            }
                        ]
                    }
                ),
            )
        ]
    )
    assert app_config.validation_problem("MCP_SERVERS")
    async with _client() as client:
        admin = await client.get("/admin/mcp-servers")
        listed = await client.get("/internal/playground/mcp/servers")
        tools = await client.post(
            "/internal/playground/mcp/tools",
            json={"server_id": "docs", "connection_id": "conn-1"},
        )

    assert admin.status_code == 409
    assert listed.status_code == 200
    assert listed.json() == {"servers": []}
    assert tools.status_code == 503
