"""An inference grant works on the agent entry and nowhere else.

A Cloud Agent sandbox can reach the public internet, so a grant it holds can
leave it. What keeps a leaked grant from being spent from anywhere is that the
public API refuses every grant, and only the agent entry, the separate listener
the Cloud Agent's relay reaches, hands one to the grant path.

The mark that separates them is written by the listener that accepted the
connection. These tests hold both halves: the public side refuses a grant
through either header and on every path, and does so before the grant store is
consulted; the agent entry passes it on, still inside the inference allowlist;
and the mark comes from the socket, which no header can imitate.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers import agent_entry, app as app_module, auth as auth_module
from serving.servers.auth import verify_api_key
from serving.servers.deps import get_log_store, get_operational_store

# A grant token only by its prefix: the gateway recognises one by `agr.`, and a
# realistic payload would read as a committed credential to the secret scans.
GRANT = "agr.fixture.grant"
GRANT_CONTEXT = {
    "user_id": "owner-1",
    "role": "pro",
    "authenticated": True,
    "is_admin": False,
    "agent_job_id": "ajob_1",
    "agent_grant_id": "gr_1",
    "agent_allowed_models": ["granted-model"],
}


@pytest.fixture
def grant_path(monkeypatch) -> AsyncMock:
    """Stand in for the grant store, and record whether anything reached it."""
    authenticate = AsyncMock(return_value=dict(GRANT_CONTEXT))
    monkeypatch.setattr(auth_module, "authenticate_grant_model_call", authenticate)
    return authenticate


def _gateway() -> FastAPI:
    """Two routes behind the real dependency: one inference path, one not."""
    app = FastAPI()
    app.dependency_overrides[get_operational_store] = lambda: MagicMock()
    app.dependency_overrides[get_log_store] = lambda: MagicMock()

    @app.post("/v1/chat/completions")
    async def chat(user: dict[str, Any] = Depends(verify_api_key)) -> dict[str, Any]:
        return {"user": user["user_id"], "grant": user.get("agent_grant_id")}

    @app.post("/v1/embeddings")
    async def embeddings(user: dict[str, Any] = Depends(verify_api_key)) -> dict[str, Any]:
        return {"user": user["user_id"]}

    return app


async def _post(app: Any, path: str, headers: dict[str, str]) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        return await client.post(path, headers=headers, json={})


HEADERS = (
    pytest.param({"Authorization": f"Bearer {GRANT}"}, id="authorization"),
    pytest.param({"X-API-Key": GRANT}, id="x-api-key"),
)


# ------------------------------------------------------------ the public API


@pytest.mark.parametrize("headers", HEADERS)
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/embeddings"])
async def test_the_public_api_refuses_a_grant_before_looking_it_up(
    grant_path: AsyncMock, headers: dict[str, str], path: str
) -> None:
    response = await _post(_gateway(), path, headers)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == {
        "type": "agent_job_auth",
        "message": "Inference grants are accepted only on the agent entry, not on this API.",
    }
    # Refused on sight: a leaked grant tells its holder nothing about whether
    # it is live, and costs the grant store nothing.
    grant_path.assert_not_awaited()


async def test_a_header_named_like_the_mark_does_not_make_one(grant_path: AsyncMock) -> None:
    headers = {"Authorization": f"Bearer {GRANT}", agent_entry.SCOPE_KEY: "true"}

    response = await _post(_gateway(), "/v1/chat/completions", headers)

    assert response.status_code == 403
    grant_path.assert_not_awaited()


# ---------------------------------------------------------- the agent entry


@pytest.mark.parametrize("headers", HEADERS)
async def test_the_agent_entry_hands_a_grant_to_the_grant_path(
    grant_path: AsyncMock, headers: dict[str, str]
) -> None:
    response = await _post(agent_entry._Marked(_gateway()), "/v1/chat/completions", headers)

    assert response.status_code == 200
    assert response.json() == {"user": "owner-1", "grant": "gr_1"}
    grant_path.assert_awaited_once()
    assert grant_path.await_args.args == (GRANT,)


async def test_on_the_agent_entry_a_grant_still_buys_only_inference(grant_path: AsyncMock) -> None:
    headers = {"Authorization": f"Bearer {GRANT}"}

    response = await _post(agent_entry._Marked(_gateway()), "/v1/embeddings", headers)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"
    grant_path.assert_not_awaited()


# ------------------------------------------------------------ the listener


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_connections_to_the_agent_entry_socket_are_marked(grant_path: AsyncMock) -> None:
    """Over a real socket, beside the same application reached without it."""
    gateway = _gateway()
    entry = await agent_entry.start(gateway, "127.0.0.1", 0)
    try:
        host, port = entry.address
        async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
            accepted = await client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {GRANT}"}, json={}
            )
        refused = await _post(gateway, "/v1/chat/completions", {"Authorization": f"Bearer {GRANT}"})
    finally:
        await entry.stop()

    assert accepted.status_code == 200, accepted.text
    assert refused.status_code == 403
    grant_path.assert_awaited_once()


async def test_a_taken_port_fails_startup_by_name() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(RuntimeError, match=f"agent entry cannot listen on 127.0.0.1:{port}"):
            await agent_entry.start(_gateway(), "127.0.0.1", port)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({agent_entry.ENV_PORT: ""}, None),
        ({agent_entry.ENV_PORT: "8090"}, ("0.0.0.0", 8090)),
        ({agent_entry.ENV_PORT: " 8090 ", agent_entry.ENV_HOST: "127.0.0.1"}, ("127.0.0.1", 8090)),
    ],
)
def test_the_configured_address(env: dict[str, str], expected: tuple[str, int] | None) -> None:
    assert agent_entry.configured_address(env) == expected


@pytest.mark.parametrize("value", ["http", "0", "65536", "-1"])
def test_a_port_that_is_not_one_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="is not a port number"):
        agent_entry.configured_address({agent_entry.ENV_PORT: value})


# ------------------------------------------------------------- the lifespan


@pytest.fixture
def quiet_startup(monkeypatch) -> dict[str, int]:
    """The application's lifespan with its services and secrets stubbed out."""
    calls = {"initialize": 0, "shutdown": 0}

    async def initialize() -> object:
        calls["initialize"] += 1
        return object()

    async def shutdown(_services: object) -> None:
        calls["shutdown"] += 1

    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    monkeypatch.setattr(app_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(app_module, "get_settings", lambda: MagicMock(admin_token="configured"))
    return calls


async def _accepts(port: int) -> bool:
    with contextlib.suppress(OSError):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        return True
    return False


async def test_the_lifespan_opens_the_agent_entry_and_closes_it(
    monkeypatch, quiet_startup: dict[str, int]
) -> None:
    port = _free_port()
    monkeypatch.setenv(agent_entry.ENV_PORT, str(port))
    monkeypatch.setenv(agent_entry.ENV_HOST, "127.0.0.1")

    async with app_module.lifespan(FastAPI()):
        assert await _accepts(port)

    assert not await _accepts(port)
    assert quiet_startup == {"initialize": 1, "shutdown": 1}


async def test_without_a_port_there_is_no_agent_entry(
    monkeypatch, quiet_startup: dict[str, int]
) -> None:
    monkeypatch.delenv(agent_entry.ENV_PORT, raising=False)

    async with app_module.lifespan(FastAPI()) as state:
        assert state is None

    assert quiet_startup == {"initialize": 1, "shutdown": 1}


async def test_a_malformed_port_stops_startup_before_anything_opens(
    monkeypatch, quiet_startup: dict[str, int]
) -> None:
    monkeypatch.setenv(agent_entry.ENV_PORT, "agent")

    with pytest.raises(ValueError, match="is not a port number"):
        async with app_module.lifespan(FastAPI()):
            pass

    assert quiet_startup == {"initialize": 0, "shutdown": 0}
