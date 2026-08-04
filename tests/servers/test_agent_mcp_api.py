"""API-surface tests for the MCP proxy endpoint.

The upstream is a ``MockTransport``, so these pin what actually crosses each
boundary: which credential reaches the MCP server, which never reaches the
sandbox, and which requests are refused before they leave the gateway at all.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs.mcp_registry import McpRegistry, McpServer
from serving.agent_jobs.tokens import SCOPE_MODEL, mint_worker_token
from serving.servers.deps import get_agent_job_store, get_operational_store
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import agent_mcp as agent_mcp_router

pytestmark = pytest.mark.asyncio

_FENCE = {"job_id": "ajob_abc", "attempt_id": 5, "lease_generation": 2}
_UPSTREAM = "https://mcp.example.com/mcp"
_UPSTREAM_SECRET = "Bearer ghs_deployment_secret"


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret the capability tokens are derived from."""
    monkeypatch.setenv("API_KEY_SECRET", "mcp-proxy-secret")
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeJobStore:
    """Resolves one live fence, mirroring the real SQL predicate."""

    def __init__(self, *, live: bool = True, granted: list[str] | None = None) -> None:
        self.live = live
        self.granted = ["github"] if granted is None else granted

    async def resolve_model_credential(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        matches = (job_id, attempt_id, lease_generation) == (
            _FENCE["job_id"],
            _FENCE["attempt_id"],
            _FENCE["lease_generation"],
        )
        if not (self.live and matches):
            return None
        return {
            "user_id": "owner-1",
            "role": "internal",
            "job_id": job_id,
            "mcp_servers": list(self.granted),
            "budget_usd": 5.0,
            "model": "glm-5.1",
        }


class Upstream:
    """A stand-in MCP server that records what the gateway sent it."""

    def __init__(self, *, body: bytes | None = None, content_type: str = "application/json"):
        self.requests: list[httpx.Request] = []
        self.body = body if body is not None else b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.content_type = content_type

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer with the canned body."""
        self.requests.append(request)
        return httpx.Response(
            200,
            content=self.body,
            headers={"content-type": self.content_type, "mcp-session-id": "sess-1"},
        )


def _registry(tools: frozenset[str] = frozenset({"get_issue"})) -> McpRegistry:
    """A registry with one credentialed, allowlisted server."""
    return McpRegistry(
        servers={
            "github": McpServer(
                name="github",
                url=_UPSTREAM,
                headers={"Authorization": _UPSTREAM_SECRET},
                tools=tools,
            )
        }
    )


@pytest.fixture()
def upstream():
    """The stand-in MCP server."""
    return Upstream()


@pytest.fixture()
def store():
    """A store resolving the test fence."""
    return FakeJobStore()


@pytest_asyncio.fixture()
async def client(monkeypatch, store, upstream):
    """Mount the proxy router with a mocked upstream and a fixed registry."""
    monkeypatch.setattr(agent_mcp_router, "get_registry", _registry)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    # The real app's handlers, so a body asserted here is the body a
    # sandbox receives rather than FastAPI's raw `detail` wrapper.
    install_error_handlers(app)
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    app.dependency_overrides[get_operational_store] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _token() -> str:
    """Mint the model-scoped token the sandbox actually carries."""
    return mint_worker_token(**_FENCE, scope=SCOPE_MODEL)


def _auth() -> dict[str, str]:
    """Authorization header for the sandbox's token."""
    return {"Authorization": f"Bearer {_token()}"}


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    """One JSON-RPC request body."""
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def test_the_upstream_credential_replaces_the_sandbox_token(client, upstream):
    """The MCP server sees our credential; the job token stops at the gateway."""
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 200

    sent = upstream.requests[0]
    assert sent.headers["authorization"] == _UPSTREAM_SECRET
    assert _token() not in str(sent.headers)


async def test_the_upstream_address_never_reaches_the_sandbox(client):
    """The agent talks to the gateway and learns nothing about who answers."""
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert "mcp.example.com" not in json.dumps(dict(response.headers))
    assert "mcp.example.com" not in response.text
    # The session id has to survive or streamable HTTP does not work at all.
    assert response.headers["mcp-session-id"] == "sess-1"


async def test_a_registry_header_replaces_the_sandbox_header_rather_than_joining_it(
    monkeypatch, store, upstream
):
    """Case must not smuggle a second value through under the same name.

    Starlette lowercases inbound header names and a registry writes them the
    way a human does, so a case-sensitive merge keeps both and httpx sends
    both. The dangerous instance of this is Authorization, where the sandbox's
    own token would ride upstream beside the credential meant to replace it.
    """
    registry = McpRegistry(
        servers={
            "github": McpServer(
                name="github",
                url=_UPSTREAM,
                headers={"Mcp-Session-Id": "registry-value"},
                tools=frozenset({"get_issue"}),
            )
        }
    )
    monkeypatch.setattr(agent_mcp_router, "get_registry", lambda: registry)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    # The real app's handlers, so a body asserted here is the body a
    # sandbox receives rather than FastAPI's raw `detail` wrapper.
    install_error_handlers(app)
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    app.dependency_overrides[get_operational_store] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        await api.post(
            "/v1/agent/mcp/github",
            json=_rpc("initialize"),
            headers={**_auth(), "mcp-session-id": "sandbox-value"},
        )

    assert upstream.requests[0].headers.get_list("mcp-session-id") == ["registry-value"]


async def test_a_server_the_job_was_not_granted_is_refused(monkeypatch, store, client, upstream):
    """A token reaches its job's servers, not every server configured here."""
    store.granted = []
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 403
    assert upstream.requests == []


async def test_a_dead_fence_stops_reaching_tools(store, client, upstream):
    """Cancelling a job revokes its tools exactly as it revokes inference."""
    store.live = False
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 401
    assert upstream.requests == []


async def test_an_ordinary_api_key_is_not_a_job_token(client, upstream):
    """This endpoint is for sandboxes; a user key is refused outright."""
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("initialize"),
        headers={"Authorization": "Bearer hyi-not-an-agent-token"},
    )
    assert response.status_code == 401
    assert upstream.requests == []


async def test_a_blocked_tool_never_leaves_the_gateway(client, upstream):
    """The allowlist is enforced before the request is made, not after."""
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("tools/call", name="create_issue"),
        headers=_auth(),
    )
    # A JSON-RPC error, not an HTTP one: the hop worked and the call failed, so
    # the CLI reports a failed tool instead of dropping the whole server.
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []


async def test_an_allowed_tool_is_forwarded(client, upstream):
    """The allowlist refuses what it names and nothing else."""
    response = await client.post(
        "/v1/agent/mcp/github", json=_rpc("tools/call", name="get_issue"), headers=_auth()
    )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_the_catalogue_is_filtered_on_the_way_back(monkeypatch, store, upstream):
    """A tool outside the allowlist is never advertised to the model."""
    upstream.body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "get_issue"}, {"name": "create_issue"}]},
        }
    ).encode()
    monkeypatch.setattr(agent_mcp_router, "get_registry", _registry)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    # The real app's handlers, so a body asserted here is the body a
    # sandbox receives rather than FastAPI's raw `detail` wrapper.
    install_error_handlers(app)
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    app.dependency_overrides[get_operational_store] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        response = await api.post("/v1/agent/mcp/github", json=_rpc("tools/list"), headers=_auth())
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["get_issue"]


async def test_a_server_removed_from_the_registry_reports_a_deployment_fault(
    monkeypatch, client, upstream
):
    """Granted at creation, gone since: a 503, not a permission error."""
    monkeypatch.setattr(agent_mcp_router, "get_registry", McpRegistry)
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 503
    assert upstream.requests == []


# ── The MCP ownership boundary ─────────────────────────────────────────
#
# MCP moved to the cloud agent: it owns the registry, the credentials and the
# attempt fence, so it is the only place that can decide a tool call. What is
# left here is a route the *deployed* agent still needs, and the pair below is
# the whole of the gateway's side of the move — one credential refused, the
# other untouched until H4 removes the route after cutover.


async def test_an_inference_grant_is_refused_by_the_mcp_route(client):
    """A grant authorizes models. It must buy no tools.

    Both halves matter. If a leaked grant could reach MCP, the model scope
    would bound what it can *call* while saying nothing about what it can
    *do* — and the deployment's upstream MCP credentials sit behind this
    route.
    """
    from serving import grants

    token = grants.mint_grant_token("agr_whatever")
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "insufficient_scope"


async def test_the_deployed_agents_token_still_works(client, upstream):
    """**The other half, and the reason this route is not simply deleted.**

    Every job running at cutover carries a per-attempt worker token. Removing
    the route now would break all of them for the sake of a boundary the cloud
    agent's relay is not yet serving. It goes at H4, after cutover.
    """
    response = await client.post("/v1/agent/mcp/github", json=_rpc("tools/list"), headers=_auth())

    assert response.status_code == 200
