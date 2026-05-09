"""Strategy registry for per-model router dispatch.

Each routing strategy lives in a sibling module (``fixed.py``, ``routewise.py``,
...) and self-registers a ``(Router class, Params Pydantic model)`` pair via
``register_strategy(name)((Router, Params))`` at import time.

``build_router(name, params_dict)`` is the single dispatch point used by
``ModelRouterRegistry`` to translate a YAML ``router: <name>`` declaration
into a concrete ``BaseRouter`` instance.

Import-order contract:
    Strategy submodules import from ``routing.routers`` /
    ``routing.routewise.router`` at module top.  This module imports the
    submodules at the *bottom* of the file to trigger registration without
    creating a cycle.  Direction is one-way: ``strategies -> routers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.strategies.weight import FixedRatioStrategy

if TYPE_CHECKING:
    from routing.routers import BaseRouter


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


def build_router(name: str, params: dict[str, Any] | None) -> BaseRouter:
    """Construct a router by strategy name + raw params dict from YAML.

    Args:
        name: Strategy name (must be registered).
        params: Raw params dict from ``models.yaml`` (``None`` and ``{}``
            both mean "use strategy defaults").

    Returns:
        A configured ``BaseRouter`` instance.

    Raises:
        ValueError: If ``name`` is not registered.  Error message lists all
            known strategies to help operators spot typos.
        pydantic.ValidationError: If ``params`` fails the strategy's Pydantic
            schema (``extra="forbid"`` on every Params model).
    """
    if name not in _STRATEGIES:
        raise ValueError(f"unknown router strategy {name!r}; known: {sorted(_STRATEGIES)}")
    router_cls, params_cls = _STRATEGIES[name]
    validated = params_cls.model_validate(params or {})
    return router_cls(params=validated)


# Trigger registration of built-in strategies via import side effects.
# Imports are at the bottom to avoid circular imports: the strategy modules
# import from routing.routers / routing.routewise at their top.
from routing.strategies import fixed, routewise  # noqa: F401
