"""Who may use a model, and the one access check every model surface applies."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any

from serving.config.settings import has_role

DISABLED_MODELS_PREFERENCE_KEY = "disabled_models"


def _pricing_mapping_indicates_paid(pricing: Any) -> bool:
    """Whether a raw ``pricing`` mapping carries any non-zero price.

    A price that cannot be parsed as a number counts as paid: an unprovable-
    free cost defaults closed, like every other gate in this codebase.
    """
    if not isinstance(pricing, Mapping):
        return False
    for value in pricing.values():
        try:
            if float(value) != 0.0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def model_pricing_indicates_paid(model: Mapping[str, Any]) -> bool:
    """Whether a raw models.yaml model entry can cost money to serve.

    Reads the same shapes the registry copies into adapters: model-level
    ``pricing`` / ``pricing_schedule`` / ``pricing_source`` and their
    route-level overrides. A schedule or source means runtime-resolved prices
    the loader cannot prove are zero, so it counts as paid.
    """
    if _pricing_mapping_indicates_paid(model.get("pricing")):
        return True
    if model.get("pricing_schedule") is not None or model.get("pricing_source") is not None:
        return True
    routes = model.get("route")
    if not isinstance(routes, list):
        return False
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        if _pricing_mapping_indicates_paid(route.get("pricing")):
            return True
        if route.get("pricing_schedule") is not None or route.get("pricing_source") is not None:
            return True
    return False


# Hosts on which a route can be locally terminated: the gateway's own
# machine (or an owned LAN alias of it). Location alone decides nothing —
# a compatibility proxy on localhost can relay to a paid remote provider —
# so an unpriced route is exempt from the unknown-cost default only when a
# local host is combined with a kind that names a self-hosted engine API.
_LOCAL_ROUTE_HOSTS = frozenset(("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))

# Adapter kinds whose API dialect belongs to a self-hosted inference engine.
# Everything else — openai_compat above all — is a forwarding dialect: a
# localhost base_url for one of those can be a reverse proxy in front of an
# externally billed upstream, and "localhost" carries no cost information.
# A deployment that wants free-tier for such a route says so explicitly
# with ``required_role: free``; reported zero user pricing does not prove the
# upstream itself is free.
_PROVABLY_LOCAL_INFERENCE_KINDS = frozenset(("sglang", "vllm", "ollama"))


def _route_is_locally_served(
    route: Mapping[str, Any], provider_kind: Any, model_base_url: Any = None
) -> bool:
    """Whether a route is provably local inference: engine-API kind, local host.

    Both halves must hold. The host being local used to be the whole test,
    which made ``openai_compat`` pointed at a localhost reverse proxy look
    free-by-construction; the kind is the half that says this route is an
    engine endpoint rather than a forwarding one. A route that names no kind
    or no base_url inherits the model's ``provider`` / ``base_url`` fields,
    mirroring the registry's own route synthesis. A ``${VAR}`` template
    base_url cannot be classified from the raw config and fails closed.
    """
    kind = route.get("kind") or provider_kind
    if not isinstance(kind, str) or kind not in _PROVABLY_LOCAL_INFERENCE_KINDS:
        return False
    base_url = route.get("base_url") or model_base_url
    try:
        from urllib.parse import urlparse

        host = urlparse(str(base_url or "")).hostname or ""
    except (ValueError, TypeError):
        return False
    return host in _LOCAL_ROUTE_HOSTS


def _route_cost_is_unknown(
    route: Mapping[str, Any], provider_kind: Any = None, model_base_url: Any = None
) -> bool:
    """Whether a route may incur upstream cost and is not provably local.

    The conservative half of the #1605 gap: a model with no pricing block
    at all cannot be proven free, and unless the route is a local host
    serving a self-hosted engine API, the request can cost real money
    upstream even when user-facing pricing is zero or omitted.
    """
    return not _route_is_locally_served(route, provider_kind, model_base_url)


def model_cost_is_unknown_remote(model: Mapping[str, Any]) -> bool:
    """Whether a model has no nonzero price evidence and a route that may cost.

    Distinguishes the three states the access gate cares about: provably free
    (every route is a local host serving a self-hosted engine API), explicitly
    or provably paid
    (non-zero / unparseable / scheduled / sourced price —
    :func:`model_pricing_indicates_paid`), and unknown cost (no nonzero price
    evidence, and at least one route that is not provably local). That last
    state defaults to ``pro`` too: an externally billed upstream must not
    fall through to free just because customer-facing pricing is zero or
    absent, and neither should a localhost reverse proxy in front of one.
    """
    if model_pricing_indicates_paid(model):
        return False
    provider_kind = model.get("provider")
    model_base_url = model.get("base_url")
    routes = model.get("route")
    if not isinstance(routes, list):
        # A single-route model without a route list: the registry synthesizes
        # one route from the top-level base_url and the model's provider
        # field. Built-in remote providers can use their own endpoint and
        # still incur upstream cost even without a configured base_url.
        return _route_cost_is_unknown(model, provider_kind)
    return any(
        isinstance(route, Mapping) and _route_cost_is_unknown(route, provider_kind, model_base_url)
        for route in routes
    )


def default_required_role_for_model(model: Mapping[str, Any]) -> str:
    """The tier a models.yaml model publishes as when it does not name one.

    Issue #1576: an omitted ``required_role`` used to publish every such model
    free-tier — including paid-upstream compatibility routes. Known paid
    models default to ``pro``; unknown remote costs do too unless the route is
    provably local. ``admin_only`` defaults to ``admin`` when no role is
    explicit. An explicit value always wins, even an invalid one (callers fail
    that closed themselves).

    The unknown-cost half of the gap: a model with no pricing metadata whose
    routes are not all provably local defaults to ``pro`` as well. Provably
    local means both a local host and a self-hosted engine API kind
    (sglang, vllm) — a localhost compatibility proxy is a forwarding
    dialect and can relay to an externally billed upstream, so local
    placement and zero user-facing pricing alone prove nothing about cost.
    Explicit ``required_role`` remains authoritative.
    """
    if "required_role" in model:
        return str(model["required_role"])
    if model.get("admin_only"):
        return "admin"
    if model_pricing_indicates_paid(model):
        return "pro"
    if model_cost_is_unknown_remote(model):
        return "pro"
    return "free"


def normalize_disabled_models(value: Any) -> list[str]:
    """Return a sorted, deduplicated list of disabled canonical model ids."""
    if not isinstance(value, list):
        return []
    normalized = {item.strip() for item in value if isinstance(item, str) and item.strip()}
    return sorted(normalized)


def get_disabled_models_from_preferences(preferences: Any) -> list[str]:
    """Extract normalized disabled models from a preferences JSON object."""
    if not isinstance(preferences, dict):
        return []
    return normalize_disabled_models(preferences.get(DISABLED_MODELS_PREFERENCE_KEY))


def is_model_disabled_for_user(canonical_model_id: str, user_ctx: dict[str, Any] | None) -> bool:
    """Return True if the canonical model id is denied for the user context."""
    if not user_ctx:
        return False
    disabled_models = normalize_disabled_models(user_ctx.get(DISABLED_MODELS_PREFERENCE_KEY))
    return canonical_model_id in disabled_models


#: Set by the grant path on a request authenticated with an ``agr`` token; absent
#: on every other kind of caller. Absence and emptiness therefore mean opposite
#: things, and that asymmetry is the whole of the check below.
GRANT_ALLOWED_MODELS_KEY = "agent_allowed_models"


def is_model_outside_grant_scope(canonical_model_id: str, user_ctx: dict[str, Any] | None) -> bool:
    """Return True if a grant-authenticated caller may not call this model.

    **This is the only scope an inference grant carries.** It names one user,
    one attempt, a short lifetime and a model list; everything except that list
    is about *whether* the credential is live rather than *what* it may reach.
    An unenforced list leaves a leaked grant able to call anything the owner's
    role can, which is what the per-task budget used to bound before the design
    dropped it.

    Absent key means "not a grant" and denies nothing — an ordinary API key is
    governed by role and denylist as before. An **empty list** is a real grant
    that resolved to no models, and denies everything: the mint clamps a
    requested list to what the role can reach, and ``None`` there already means
    "all of them", so an empty list can only mean the intersection was empty.
    Reading it as "unrestricted" would invert the one case where the mint
    already decided the answer was nothing.
    """
    if not user_ctx:
        return False
    allowed = user_ctx.get(GRANT_ALLOWED_MODELS_KEY)
    if allowed is None:
        return False
    return canonical_model_id not in set(allowed)


class ModelAccessDenial(enum.Enum):
    """Why a caller may not use a model.

    Every surface answers each reason the same way it answers an unknown model,
    so a caller cannot tell them apart; the reason is for the surface's own
    logging.
    """

    UNPUBLISHED = "unpublished"
    ROLE = "role"
    DISABLED = "disabled"
    OUTSIDE_GRANT = "outside_grant"


def route_canonical_model_id(route: Any, model_id: str) -> str:
    """Return the canonical id of the model ``route`` serves.

    An alias shares its model's route, so the route's first adapter names the
    model whichever spelling the caller used. ``model_id``, the name the route
    was looked up by, stands in only for a route with no adapters.
    """
    adapters = getattr(route, "adapters", None)
    return adapters[0][0].config.id if adapters else model_id


def baseline_required_role(route: Any) -> str:
    """Return the role a route requires before any runtime visibility override."""
    return route.required_role or ("admin" if route.admin_only else "free")


async def model_access_denial(
    route: Any,
    model_id: str,
    user_ctx: dict[str, Any] | None,
    *,
    visibility_resolver: Any | None = None,
    grant_scope: bool = True,
) -> ModelAccessDenial | None:
    """Return why ``user_ctx`` may not use the model behind ``route``, or None.

    The check every surface applies before it serves or lists a model: the
    route is published, the caller's role meets the role the model requires
    (after the runtime visibility override), the caller has not disabled the
    model, and, with ``grant_scope``, an inference grant names it. Overrides,
    denylists and grants are all keyed by the canonical id, so every check reads
    the canonical id: an alias is held to the same rules as its model.

    Only queries; nothing is claimed or recorded.

    Args:
        route: The route registered under ``model_id``.
        model_id: The name the route was looked up by, canonical id or alias.
        user_ctx: The caller as the auth dependency resolved it, or None for
            an anonymous caller.
        visibility_resolver: Source of runtime required-role overrides, if the
            deployment has one.
        grant_scope: Whether to apply an inference grant's model list. The
            inference surfaces do; the model listings and the agent catalog do
            not.

    Returns:
        The first reason that applies, in the order above, or None when the
        caller may use the model.
    """
    if not route.published:
        return ModelAccessDenial.UNPUBLISHED
    canonical = route_canonical_model_id(route, model_id)
    required = baseline_required_role(route)
    if visibility_resolver is not None:
        required = await visibility_resolver.get_effective_required_role(canonical, required)
    if not has_role((user_ctx or {}).get("role") or "free", required):
        return ModelAccessDenial.ROLE
    if is_model_disabled_for_user(canonical, user_ctx):
        return ModelAccessDenial.DISABLED
    if grant_scope and is_model_outside_grant_scope(canonical, user_ctx):
        return ModelAccessDenial.OUTSIDE_GRANT
    return None
