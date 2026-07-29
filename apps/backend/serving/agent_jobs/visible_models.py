"""Which models an agent job can actually call — one predicate, used twice.

The composer's model picker and the create endpoint's validation must agree
with what the inference path will do when the sandbox makes its first call,
or the product offers models that fail with 404 after the job has already
claimed, cloned, and burned an attempt. On staging this was 13 of 15 listed
models: the browsing user's role saw the full list while the job's calls ran
at a narrower role.

The predicate here mirrors the completions/anthropic routers' checks —
registered, published, role-gated (with the visibility resolver's override),
not disabled for this user — evaluated against the *owner's* context, which
is the identity the sandbox's calls now run as (see ``model_auth``).
"""

from __future__ import annotations

from typing import Any

from serving.config.settings import has_role
from serving.model_access import is_model_disabled_for_user

# Agents drive chat surfaces; an embedding model would "resolve" and then be
# useless, so it is excluded from both the picker and the validation.
_AGENT_MODEL_TYPE = "chat"


def _effective_role(user_ctx: dict[str, Any] | None) -> str:
    return (user_ctx or {}).get("role") or "free"


async def _route_allows(
    route: Any,
    canonical: str,
    *,
    visibility_resolver: Any | None,
    user_ctx: dict[str, Any] | None,
) -> bool:
    """The inference-path predicate, minus the 404 wrapping."""
    if not route.published:
        return False
    required = route.required_role or ("admin" if route.admin_only else "free")
    if visibility_resolver is not None:
        required = await visibility_resolver.get_effective_required_role(canonical, required)
    if not has_role(_effective_role(user_ctx), required):
        return False
    return not is_model_disabled_for_user(canonical, user_ctx)


async def agent_visible_models(
    router_exec: Any,
    *,
    visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
) -> list[str]:
    """Canonical chat-model ids this user's agent jobs can call, in registry order."""
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


async def agent_model_resolvable(
    model: str,
    router_exec: Any,
    *,
    visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
) -> bool:
    """Whether one requested id (canonical or alias) would resolve for this user.

    This is the create-time fail-fast: rejecting an unknown model here costs a
    400; accepting it costs a claim, a clone, and an attempt before the first
    model call dies.
    """
    route = getattr(router_exec, "routes", {}).get(model)
    if route is None:
        return False
    configs = [adapter.config for adapter, _ in route.adapters]
    if not configs:
        return False
    if getattr(configs[0], "model_type", _AGENT_MODEL_TYPE) != _AGENT_MODEL_TYPE:
        return False
    return await _route_allows(
        route, configs[0].id, visibility_resolver=visibility_resolver, user_ctx=user_ctx
    )
