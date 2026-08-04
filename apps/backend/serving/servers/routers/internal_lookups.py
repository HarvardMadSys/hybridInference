"""The two questions the control plane can no longer answer by importing.

Before the split, cloud agent code lived in this process and read the model
registry and the users table directly. Afterwards those imports are gone, and
each one needs an endpoint *before* the task that moves its caller — or that
task either fails to import or silently degrades, and a silent degrade here
looks like a feature that stopped working for no reason.

| Endpoint | Replaces | Blocks |
|---|---|---|
| ``GET /internal/model-catalog`` | ``visible_models``' catalog read | E4 |
| ``GET /internal/users/{id}/status`` | the gateway ``users`` row read | E7, E8 |

**There was a third, and it is deliberately absent.** An earlier draft served
``GET /internal/mcp-registry`` so the control plane could populate a picker
from this deployment's MCP servers. The ownership amendment moved the MCP
registry, its credentials and its proxy to the cloud agent — which already
holds the job, its requested servers and the attempt fence — so the control
plane reads its own registry and this gateway answers nothing about MCP. Adding
the endpoint back would put the same list in two places and make "which servers
exist" a question with two answers.

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

from serving.agent_jobs.visible_models import agent_visible_models
from serving.model_access import get_disabled_models_from_preferences
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


def _aliases_for(router_exec: Any, visible: list[str]) -> dict[str, str]:
    """Return ``{alias: canonical}`` for the models this user can see.

    The route table keys canonical ids *and* their aliases at the same level,
    each pointing at the same config, so an entry is an alias exactly when its
    key is not the canonical id it resolves to.

    Two entries are dropped rather than returned, because a consumer resolves
    with ``aliases.get(name, name)`` and either would make that lie:

    * an alias whose canonical this user cannot see — it would resolve to a
      model the catalog does not list, and the caller would be refused a
      moment later with nothing to explain it;
    An alias spelled the same as a model this catalog lists is not dropped but
    **refused**: see the comment at the raise. Ordinary inference is untouched
    either way — it keeps routing exactly as the route table says.

    Case is left exactly as configured. Lowercasing here would accept spellings
    the inference path does not, which is a difference nobody would find until
    a job failed at its first call.
    """
    visible_set = set(visible)
    aliases: dict[str, str] = {}
    for name, route in router_exec.routes.items():
        canonical = getattr(route, "canonical_model_id", None)
        if not canonical or canonical == name:
            continue
        if name in visible_set:
            # **Not expressible, so not answered.** This name is listed as a
            # model *and* routes somewhere else — some other model declared it
            # as an alias and overwrote its entry, while the original stayed
            # reachable through an alias of its own.
            #
            # Dropping the entry (what this used to do) is the worst option:
            # the consumer sees the name in `models`, treats it as canonical,
            # mints a grant for it, and the very next request routes to the
            # other model and is refused by that grant's own scope. The job is
            # created successfully and dies at its first model call.
            #
            # There is no correct answer to give, so the endpoint says so.
            raise _error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ambiguous_model_catalog",
                f"Model name {name!r} is both a model and an alias for "
                f"{canonical!r}; this catalog cannot be resolved unambiguously.",
            )
        if canonical not in visible_set:
            continue
        aliases[name] = canonical
    return aliases


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
        user_ctx={
            "role": user.get("role") or "free",
            "user_id": user["id"],
            # Without this the resolver's denylist check reads an absent
            # key and passes, so a model the owner disabled is offered by
            # the composer — and the grant minted from that choice carries it.
            "disabled_models": get_disabled_models_from_preferences(user.get("preferences")),
        },
    )
    return {
        "user_id": user["id"],
        "role": user.get("role") or "free",
        "models": models,
        # The translation table, so the control plane resolves an alias the way
        # it used to when it could read this registry in-process. It resolves
        # once and works in canonical ids from there; nothing downstream — the
        # grant, the store, the scope check — ever sees an alias.
        "aliases": _aliases_for(router_exec, models),
    }


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
