"""The MCP proxy endpoint: how a closed sandbox reaches the MCP ecosystem.

``/v1/agent/mcp/{server}`` is an MCP streamable-HTTP endpoint that the agent's
own CLI speaks to as if it were the upstream server. It is not a convenience
layer — it is what lets the design's ``platform_only`` egress tier survive
contact with MCP at all. The alternative, letting the sandbox dial
``api.githubcopilot.com`` itself, needs an open network *and* a credential
inside the sandbox, and gives both to the phase that runs untrusted repository
content. This gives that phase neither.

What crosses which boundary:

```text
sandbox ──[job token, gateway URL]──▶ gateway ──[deployment credential]──▶ MCP server
```

The sandbox authenticates with the capability token it already holds, on the
network it already has. It never learns the upstream address or its key, and
the fence that governs the token is the one that already governs model calls,
so a cancelled job loses its tools at the same instant it loses inference.

Three checks stand between a request and the upstream, and they are
deliberately independent:

1. the token resolves to a live attempt (:func:`authenticate_agent_tool_call`);
2. the named server is one *this job* was created with — not merely one the
   deployment configured, or any job's token would reach every server by
   naming it in the path;
3. the tool is on the server's allowlist (:mod:`serving.agent_jobs.mcp_proxy`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from serving import grants
from serving.agent_jobs.mcp_proxy import (
    MAX_BUFFERED_RESPONSE_BYTES,
    SseFilter,
    filter_json_body,
    plan_request,
)
from serving.agent_jobs.mcp_registry import McpServer, get_registry
from serving.agent_jobs.model_auth import (
    AgentModelAuthError,
    authenticate_agent_tool_call,
    authenticate_grant_tool_call,
    looks_like_agent_token,
)
from serving.servers.deps import get_agent_job_store, get_operational_store
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from serving.storage.agent_job_store import AgentJobStore

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agent/mcp")

# An MCP call is a tool call: it can be a repository search or an issue fetch,
# and it is not a token stream. Generous, but bounded — a sandbox waiting on a
# hung upstream is a job burning its wall-clock timeout for nothing.
_UPSTREAM_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 10.0

# Forwarded from the agent to the upstream. Everything else — Authorization
# above all, but equally Cookie, X-Forwarded-*, and whatever else the CLI or an
# intermediary attaches — is dropped: the upstream sees a request this gateway
# made, carrying this deployment's credential and nothing of the sandbox's.
_FORWARD_TO_UPSTREAM = (
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
)

# Returned from the upstream to the agent. Session id must survive for
# streamable HTTP to work at all; the rest is this gateway's to decide, and
# Set-Cookie in particular must never reach a sandbox.
_RETURN_TO_AGENT = ("content-type", "mcp-session-id", "mcp-protocol-version")

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def _bearer(authorization: str | None) -> str:
    """Extract the sandbox's capability token, or refuse."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not looks_like_agent_token(token):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "message": "This endpoint is for agent jobs and takes a job token.",
                }
            },
        )
    return token


async def _authorize(
    server_name: str,
    authorization: str | None,
    store: AgentJobStore | None,
    op_store: Any | None = None,
) -> tuple[McpServer, str]:
    """Resolve the token and the named server, or raise the right HTTP error."""
    token = _bearer(authorization)
    try:
        if grants.looks_like_grant_token(token):
            # Same fence, no quota: a tool call invokes no inference provider,
            # so it spends nothing there is a limit on.
            granted = await authenticate_grant_tool_call(token, op_store=op_store)
            identity = {
                "job_id": granted["agent_job_id"],
                "mcp_servers": granted["agent_allowed_mcp"],
            }
        else:
            identity = await authenticate_agent_tool_call(token, job_store=store)
    except AgentModelAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"type": "invalid_request_error", "message": exc.message}},
        ) from exc

    job_id = identity["job_id"]
    if server_name not in identity["mcp_servers"]:
        # The job's own list, not the registry's. A token is scoped to the job
        # it was minted for, and a job reaches exactly the servers it was
        # created with.
        logger.warning(
            "agent_mcp_server_not_granted",
            extra={
                "event": "agent_mcp_server_not_granted",
                "job_id": job_id,
                "server": server_name,
            },
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "permission_error",
                    "message": f"This job was not created with the {server_name!r} MCP server.",
                }
            },
        )

    server = get_registry().get(server_name)
    if server is None:
        # Granted at creation, gone from the registry since. Not the job's
        # fault and not a permission problem: say so plainly rather than
        # reporting it as a missing endpoint.
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "server_error",
                    "message": (
                        f"The {server_name!r} MCP server is no longer configured on this "
                        "deployment."
                    ),
                }
            },
        )
    return server, job_id


def _upstream_headers(request: Request, server: McpServer) -> dict[str, str]:
    """Build the outbound header set: the agent's protocol, our credential.

    Merged case-insensitively, which is not a nicety. HTTP header names are
    case-insensitive but ``dict`` keys are not, and the forwarded set arrives
    lowercased from Starlette while a registry writes ``Authorization`` the way
    a human does. A naive ``update`` therefore keeps *both*, httpx sends both,
    and the sandbox's own token rides along beside the credential meant to
    replace it.
    """
    headers = {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() in _FORWARD_TO_UPSTREAM
    }
    # The registry always wins over anything the sandbox sent under the same
    # name — now that the same name is actually recognized as the same name.
    headers.update({key.lower(): value for key, value in server.headers.items()})
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Pick what may travel back to the sandbox."""
    return {
        key: value for key, value in upstream.headers.items() if key.lower() in _RETURN_TO_AGENT
    }


def build_upstream_client() -> httpx.AsyncClient:
    """Build the client the proxy dials upstream with.

    Redirects are not followed: a redirect would re-send this deployment's
    credential to whatever host the response named.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_UPSTREAM_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
        follow_redirects=False,
    )


async def _proxy(
    request: Request,
    *,
    server: McpServer,
    job_id: str,
    method: str,
    body: bytes = b"",
) -> Any:
    """Forward one request upstream and stream or filter the reply back."""
    client = build_upstream_client()
    upstream_request = client.build_request(
        method,
        server.url,
        content=body or None,
        headers=_upstream_headers(request, server),
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(
            "agent_mcp_upstream_error",
            extra={"event": "agent_mcp_upstream_error", "job_id": job_id, "server": server.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "server_error",
                    "message": f"The {server.name!r} MCP server could not be reached.",
                }
            },
        ) from exc

    content_type = upstream.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        return StreamingResponse(
            _stream_sse(upstream, client, server),
            status_code=upstream.status_code,
            headers={**_response_headers(upstream), **_SSE_HEADERS},
            media_type=content_type,
        )

    try:
        chunks: list[bytes] = []
        total = 0
        async for chunk in upstream.aiter_bytes():
            total += len(chunk)
            if total > MAX_BUFFERED_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {
                            "type": "server_error",
                            "message": (
                                f"The {server.name!r} MCP server returned more than "
                                "this gateway will buffer for one reply."
                            ),
                        }
                    },
                )
            chunks.append(chunk)
    finally:
        await upstream.aclose()
        await client.aclose()

    payload = b"".join(chunks)
    if content_type.startswith("application/json"):
        payload = filter_json_body(payload, server)
    return Response(
        content=payload,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
    )


async def _stream_sse(
    upstream: httpx.Response, client: httpx.AsyncClient, server: McpServer
) -> AsyncIterator[bytes]:
    """Relay an SSE reply, rewriting tool catalogues as they pass."""
    sse = SseFilter(server)
    try:
        async for chunk in upstream.aiter_bytes():
            if out := sse.feed(chunk):
                yield out
        if tail := sse.flush():
            yield tail
    finally:
        await upstream.aclose()
        await client.aclose()


@router.post("/{server_name}")
async def mcp_post(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    op_store: Any = Depends(get_operational_store),
) -> Any:
    """Relay one MCP request, refusing tools the allowlist does not cover."""
    server, job_id = await _authorize(server_name, authorization, store, op_store)
    body = await request.body()

    plan = plan_request(body, server)
    if plan.rejected:
        logger.warning(
            "agent_mcp_tool_blocked",
            extra={
                "event": "agent_mcp_tool_blocked",
                "job_id": job_id,
                "server": server.name,
                "tools": list(plan.blocked_tools),
            },
        )
        # 200 with a JSON-RPC error, which is what the protocol calls for: the
        # HTTP hop succeeded and the *call* failed. Answering 403 here would
        # look like a broken connection to the CLI and take the whole server
        # down for the rest of the job.
        return JSONResponse(plan.rejection, status_code=200)

    return await _proxy(request, server=server, job_id=job_id, method="POST", body=body)


@router.get("/{server_name}")
async def mcp_get(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    op_store: Any = Depends(get_operational_store),
) -> Any:
    """Open the server-to-client stream for an established session."""
    server, job_id = await _authorize(server_name, authorization, store, op_store)
    return await _proxy(request, server=server, job_id=job_id, method="GET")


@router.delete("/{server_name}")
async def mcp_delete(
    server_name: str,
    request: Request,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    op_store: Any = Depends(get_operational_store),
) -> Any:
    """Terminate an MCP session."""
    server, job_id = await _authorize(server_name, authorization, store, op_store)
    return await _proxy(request, server=server, job_id=job_id, method="DELETE")


__all__ = ["router"]
