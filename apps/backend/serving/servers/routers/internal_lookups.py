"""The three questions the control plane can no longer answer by importing.

Before the split, cloud agent code lived in this process and read the model
registry, the MCP registry and the users table directly. Afterwards those
imports are gone, and each one needs an endpoint *before* the task that moves
its caller — or that task either fails to import or silently degrades, and a
silent degrade here looks like a feature that stopped working for no reason.

| Endpoint | Replaces | Blocks |
|---|---|---|
| ``GET /internal/mcp-registry`` | ``mcp_registry.get_registry()`` | E6 |
| ``GET /internal/model-catalog`` | ``visible_models``' catalog read | E4 |
| ``GET /internal/users/{id}/status`` | the gateway ``users`` row read | E7, E8 |

**The registry endpoint returns names and display metadata only.** A server's
``url`` and ``headers`` are exactly what the split keeps on this side: the
headers carry the deployment's upstream credential. The control plane needs to
know *which* servers exist so it can populate a picker and reject an unknown
name; it never needs to reach them, because the sandbox reaches them through
this gateway's proxy.

**The catalog endpoint is not ``GET /v1/models``**, and that is not a
preference. That route authenticates with ``optional_verify_api_key``, which
does not recognise an identity JWT — a control plane calling it would receive
the *anonymous* catalog, and every pro/internal/admin user would silently lose
the models their role can reach. Same authorization as the rest of this module:
the dispatch token, with the user named explicitly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status

from serving.agent_jobs.mcp_registry import get_registry
from serving.agent_jobs.visible_models import agent_visible_models
from serving.servers.deps import (
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers.internal_auth import (
    error as _error,
    require_dispatch_token,
    require_store as _require_store,
)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/mcp-registry")
async def mcp_registry(_: None = Depends(require_dispatch_token)) -> dict[str, Any]:
    """List the MCP servers this deployment offers, without their credentials.

    Args:
        _: Dispatch-token authorization.

    Returns:
        Name, description, default flag, and whether the server's tools are
        unfiltered — enough to populate a picker and validate a request.
    """
    registry = get_registry()
    servers = []
    for name in registry.names:
        server = registry.get(name)
        if server is None:  # pragma: no cover - names() is derived from servers
            continue
        # url and headers are deliberately absent. The headers hold this
        # deployment's upstream credential, and the control plane has no use
        # for the address: the sandbox reaches MCP through our proxy.
        servers.append(
            {
                "name": server.name,
                "description": server.description,
                "default": server.default,
                "tools": sorted(server.tools),
                "unfiltered": server.unfiltered,
            }
        )
    return {"servers": servers}


@router.get("/model-catalog")
async def model_catalog(
    user_id: str = Query(max_length=128),
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """List the chat models one user's agent jobs may call.

    Deliberately not ``GET /v1/models``: that route cannot authenticate an
    identity JWT and would hand back the anonymous catalog, silently narrowing
    every non-free user to the models a stranger sees.

    Args:
        user_id: Whose role to resolve the catalog for.
        _: Dispatch-token authorization.
        store: Operational store, for the user's role.
        router_exec: Model registry.
        visibility_resolver: Runtime visibility overrides, so this answer and
            the inference path's agree.

    Returns:
        The canonical chat-model ids, in registry order.

    Raises:
        HTTPException: 403 if the account is unknown or not active.
    """
    store = _require_store(store)
    user = await store.get_user_by_id(user_id)
    if user is None or user.get("status") != "active":
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "subject_unavailable",
            "That user cannot be resolved.",
        )
    models = await agent_visible_models(
        router_exec,
        visibility_resolver=visibility_resolver,
        user_ctx={"role": user.get("role") or "free", "user_id": user["id"]},
    )
    return {"user_id": user["id"], "role": user.get("role") or "free", "models": models}


@router.get("/users/{user_id}/status")
async def user_status(
    user_id: str,
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Report whether a user exists, may sign in, and with what role.

    The control plane keeps only an external user id; everything else about an
    account stays here. This is how it answers "may this person still use the
    service" without holding a copy of the users table.

    Unlike the other two, an unknown user is a **200 with ``exists: false``**
    rather than a 403. The caller is asking a question about an account, not
    trying to act as one, and it needs to distinguish "gone" from "suspended"
    to decide whether to archive a thread or show a message.

    Args:
        user_id: The account to report on.
        _: Dispatch-token authorization.
        store: Operational store.

    Returns:
        Existence, active flag, status, role and email.
    """
    store = _require_store(store)
    user = await store.get_user_by_id(user_id)
    if user is None:
        return {"user_id": user_id, "exists": False, "active": False}
    return {
        "user_id": user["id"],
        "exists": True,
        "active": user.get("status") == "active",
        "status": user.get("status"),
        "role": user.get("role") or "free",
        "email": user.get("email"),
    }
