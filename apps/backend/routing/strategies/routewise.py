"""RouteWise (cost-aware primal-dual) routing strategy.

Self-registers via ``register_strategy("routewise")`` at import time.

``RouteWiseParams`` mirrors ``routing.routewise.config.RouteWiseConfig``
field-for-field with the same defaults.  ``extra="forbid"`` rejects unknown
keys at boot, so a typo in ``models.yaml`` (``router_params: { daily_quotas: 5000 }``)
fails fast with a clear message rather than silently using the default.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any, get_type_hints

from pydantic import BaseModel, create_model

from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from routing.strategies import register_strategy


class _RouteWiseParamsBase(BaseModel):
    """Strict base for generated RouteWise strategy parameters."""

    model_config = {"extra": "forbid"}


def _routewise_param_fields() -> dict[str, tuple[Any, Any]]:
    defaults = RouteWiseConfig()
    type_hints = get_type_hints(RouteWiseConfig)
    return {
        field.name: (type_hints[field.name], getattr(defaults, field.name))
        for field in fields(RouteWiseConfig)
    }


RouteWiseParams = create_model(
    "RouteWiseParams",
    __base__=_RouteWiseParamsBase,
    **_routewise_param_fields(),
)


register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))
