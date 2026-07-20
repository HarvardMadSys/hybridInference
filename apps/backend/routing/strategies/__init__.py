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
    "register_strategy",
]


_STRATEGIES: dict[str, tuple[type, type]] = {}


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
    if name not in _STRATEGIES:
        raise ValueError(f"unknown router strategy {name!r}; known: {sorted(_STRATEGIES)}")
    router_cls, params_cls = _STRATEGIES[name]
    validated = params_cls.model_validate(params or {})
    if dependencies is not None:
        if not _accepts_health_registry(router_cls):
            raise TypeError(
                f"router strategy {name!r} must accept health_registry= when "
                "RouterBuildDependencies are supplied"
            )
        return router_cls(
            params=validated,
            health_registry=dependencies.health_registry,
        )
    return router_cls(params=validated)


# Trigger registration of built-in strategies via import side effects.
# Imports are at the bottom to avoid circular imports: the strategy modules
# import from routing.routers / routing.routewise at their top.
from routing.strategies import fixed, routewise  # noqa: F401
