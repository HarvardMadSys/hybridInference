"""Which models an agent job can actually call.

The composer's model picker and the create endpoint's validation must agree
with what the inference path will do when the sandbox makes its first call,
or the product offers models that fail with 404 after the job has already
claimed, cloned, and burned an attempt. On staging this was 13 of 15 listed
models: the browsing user's role saw the full list while the job's calls ran
at a narrower role.

The predicate is the inference path's own access check
(``serving/model_access.py``) — registered, published, role-gated (with the
visibility resolver's override), not disabled for this user — evaluated
against the *owner's* context, which is the identity the sandbox's calls now
run as (see ``model_auth``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.model_access import model_access_denial

if TYPE_CHECKING:
    from collections.abc import Sequence

# Agents drive chat surfaces; an embedding model would "resolve" and then be
# useless, so it is excluded from both the picker and the validation.
_AGENT_MODEL_TYPE = "chat"


async def _route_allows(
    route: Any,
    canonical: str,
    *,
    visibility_resolver: Any | None,
    user_ctx: dict[str, Any] | None,
) -> bool:
    """The inference-path check, minus the 404 wrapping.

    No grant scope: the context here is the owner's, from which a grant is
    about to be minted.
    """
    denial = await model_access_denial(
        route,
        canonical,
        user_ctx,
        visibility_resolver=visibility_resolver,
        grant_scope=False,
    )
    return denial is None


async def agent_visible_models(
    router_exec: Any,
    *,
    visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
) -> list[str]:
    """Canonical chat-model ids this user's agent jobs can call, in registry order.

    Chat routes only: /v1/embeddings is outside the grant surface, so a minted
    scope can never name an embedding model and a grant-authenticated caller
    is refused there as outside its scope.
    """
    visible: list[str] = []
    seen: set[str] = set()
    for _model_id, route in tuple(router_exec.routes.items()):
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        canonical = configs[0].id
        if canonical in seen:
            continue
        if getattr(configs[0], "model_type", _AGENT_MODEL_TYPE) != _AGENT_MODEL_TYPE:
            continue
        if not await _route_allows(
            route, canonical, visibility_resolver=visibility_resolver, user_ctx=user_ctx
        ):
            continue
        seen.add(canonical)
        visible.append(canonical)
    return visible


def agent_model_reasoning_efforts(router_exec: Any, models: Sequence[str]) -> dict[str, list[str]]:
    """The reasoning-effort values each of ``models`` accepts, for the ones that do.

    Models that cannot take a reasoning effort are absent from the mapping
    rather than present with an empty list: a consumer intersecting this
    against its own runtime's vocabulary should reach "no knob here" by finding
    nothing, which is also what it finds when talking to a gateway too old to
    answer this at all. One shape for both, so the picker cannot render an
    empty control in either case.

    Read off the first route's config, as the rest of this module does — the
    field is per model, copied onto every route the registry builds from it.

    Args:
        router_exec: Model registry.
        models: Canonical ids to describe, normally the caller's visible list.

    Returns:
        ``{model_id: [values]}`` for those models declaring a domain.
    """
    efforts: dict[str, list[str]] = {}
    for model_id in models:
        route = getattr(router_exec, "routes", {}).get(model_id)
        if route is None:
            continue
        # Describe what can be described. This is additive metadata on an
        # endpoint whose actual job is the model list, so a route that cannot
        # account for its adapters costs the caller one absent effort domain —
        # not the catalog, and not the job that was about to be created.
        configs = [adapter.config for adapter, _ in getattr(route, "adapters", None) or ()]
        if not configs:
            continue
        cfg = configs[0]
        if "reasoning_effort" not in (getattr(cfg, "supported_params", None) or ()):
            continue
        declared = [str(value) for value in (getattr(cfg, "reasoning_efforts", None) or ())]
        if declared:
            efforts[model_id] = declared
    return efforts
