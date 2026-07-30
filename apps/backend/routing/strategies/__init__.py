"""Strategy registry for per-model router dispatch.

Each routing strategy lives in a sibling module (``fixed.py``, ``routewise.py``,
...) and self-registers a ``(Router class, Params Pydantic model)`` pair via
``register_strategy(name)((Router, Params))`` at import time.

``build_router(name, params_dict)`` is the single dispatch point used by
``ModelRouterRegistry`` to translate a YAML ``router: <name>`` declaration
into a concrete ``RouterProtocol`` implementation.

Import-order contract:
    Strategy submodules import from ``routing.routers`` /
    ``routing.routewise.router`` at module top.  This module imports the
    submodules at the *bottom* of the file to trigger registration without
    creating a cycle.  Direction is one-way: ``strategies -> routers``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from routing.strategies.weight import FixedRatioStrategy

if TYPE_CHECKING:
    from routing.dependencies import RouterBuildDependencies
    from routing.protocols import RouterProtocol


__all__ = [
    "FixedRatioStrategy",
    "build_router",
    "register_missing_strategy",
    "register_strategy",
    "validate_router_config",
]


_STRATEGIES: dict[str, tuple[type, type]] = {}

# Strategies whose implementation package is not installed (optional extras).
# Selecting one in models.yaml must fail configuration validation with an
# actionable message instead of failing at backend import time.
_MISSING_STRATEGIES: dict[str, str] = {}


def register_missing_strategy(name: str, reason: str) -> None:
    """Record that ``name`` is a known strategy without an installed backend.

    Used by the import guards for optional strategy packages (and by tests).
    A later successful :func:`register_strategy` for the same name wins.
    """
    _MISSING_STRATEGIES[name] = reason


def register_strategy(name: str):
    """Register a routing strategy under ``name``.

    Usage::

        register_strategy("fixed")((FixedRouter, FixedParams))

    The double-call shape (``register_strategy(name)(item)``) keeps the call
    site declarative and matches the spec.

    Registered router constructors must accept ``params=``. Constructors used
    with application-scoped :class:`RouterBuildDependencies` must additionally
    accept ``health_registry=`` (or arbitrary keyword arguments); standalone
    factory calls remain compatible with params-only constructors.

    Args:
        name: Strategy name to register under.

    Returns:
        A decorator that accepts a ``(Router class, Params Pydantic model)``
        tuple and stores it in the registry.
    """

    def deco(item: tuple[type, type]) -> tuple[type, type]:
        cls, params_cls = item
        _STRATEGIES[name] = (cls, params_cls)
        return item

    return deco


def _accepts_health_registry(router_cls: type) -> bool:
    """Return whether a registered router constructor accepts health injection."""
    try:
        parameters = inspect.signature(router_cls).parameters
    except (TypeError, ValueError):
        return False
    health_parameter = parameters.get("health_registry")
    if health_parameter is not None and health_parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _validated_strategy(
    name: str,
    params: dict[str, Any] | None,
    *,
    dependencies: RouterBuildDependencies | None = None,
) -> tuple[type, Any]:
    """Resolve and validate a strategy without constructing its router."""
    if name not in _STRATEGIES:
        if name in _MISSING_STRATEGIES:
            raise ValueError(_MISSING_STRATEGIES[name])
        raise ValueError(f"unknown router strategy {name!r}; known: {sorted(_STRATEGIES)}")
    router_cls, params_cls = _STRATEGIES[name]
    validated = params_cls.model_validate(params or {})
    if dependencies is not None and not _accepts_health_registry(router_cls):
        raise TypeError(
            f"router strategy {name!r} must accept health_registry= when "
            "RouterBuildDependencies are supplied"
        )
    return router_cls, validated


def validate_router_config(
    name: str,
    params: dict[str, Any] | None,
    *,
    dependencies: RouterBuildDependencies | None = None,
) -> Any:
    """Validate strategy configuration without constructing a router.

    The same name, Pydantic schema, and dependency-injection checks used by
    :func:`build_router` run here, but the registered router constructor is
    never called. This keeps boot and admin validation free of router lifecycle
    side effects.
    """
    _router_cls, validated = _validated_strategy(
        name,
        params,
        dependencies=dependencies,
    )
    return validated


def build_router(
    name: str,
    params: dict[str, Any] | None,
    *,
    dependencies: RouterBuildDependencies | None = None,
) -> RouterProtocol:
    """Construct a router by strategy name + raw params dict from YAML.

    Args:
        name: Strategy name (must be registered).
        params: Raw params dict from ``models.yaml`` (``None`` and ``{}``
            both mean "use strategy defaults").
        dependencies: Optional application-scoped dependencies.  When omitted,
            construction retains the standalone factory behavior and each
            router owns its default collaborators.

    Returns:
        A configured ``RouterProtocol`` implementation.

    Raises:
        ValueError: If ``name`` is not registered.  Error message lists all
            known strategies to help operators spot typos.
        TypeError: If application dependencies are supplied but the registered
            router constructor cannot accept ``health_registry=``.
        pydantic.ValidationError: If ``params`` fails the strategy's Pydantic
            schema (``extra="forbid"`` on every Params model).
    """
    router_cls, validated = _validated_strategy(
        name,
        params,
        dependencies=dependencies,
    )
    if dependencies is not None:
        return router_cls(
            params=validated,
            health_registry=dependencies.health_registry,
        )
    return router_cls(params=validated)


# Trigger registration of built-in strategies via import side effects.
# Imports are at the bottom to avoid circular imports: the strategy modules
# import from routing.routers / routing.routewise at their top.
from routing.strategies import fixed  # noqa: F401

try:
    from routing.strategies import routewise  # noqa: F401
except ImportError:  # pragma: no cover - exercised only without the extra
    # RouteWise is heading for an optional install (private package). Keep
    # the neutral registry importable and surface the gap at configuration
    # validation time instead of at backend import time.
    register_missing_strategy(
        "routewise",
        "router strategy 'routewise' is configured but the optional RouteWise "
        "package is not installed; install the 'routewise' extra "
        "(uv sync --extra routewise) or select a different router in models.yaml",
    )
