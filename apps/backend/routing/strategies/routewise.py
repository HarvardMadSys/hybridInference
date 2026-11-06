"""RouteWise (cost-aware primal-dual) routing strategy.

Self-registers via ``register_strategy("routewise")`` at import time.

``RouteWiseParams`` mirrors ``routing.routewise.config.RouteWiseConfig``
field-for-field with the same defaults.  ``extra="forbid"`` rejects unknown
keys at boot, so a typo in ``models.yaml`` (``router_params: { daily_quotas: 5000 }``)
fails fast with a clear message rather than silently using the default.
Retired tuning keys are the one exception: see ``_RETIRED_PARAMS``.
"""

from __future__ import annotations

import threading
from dataclasses import fields
from typing import Any, get_type_hints

from pydantic import BaseModel, create_model, model_validator

from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from routing.strategies import register_strategy
from serving.utils.logging import get_logger

logger = get_logger(__name__)

# Former field names and where their replacement lives now.
# Kept so a stale models.yaml fails at boot with a pointer instead of a bare
# "extra inputs are not permitted".
_MOVED_TO_ROUTE_LEVEL = {
    "shadow_price_window_hours": "renamed to envelope_window_hours",
    "daily_quota": "route-level quota.limit",
    "reset_timezone": "route-level quota.window.timezone",
    "quota_monthly_fee": "removed (subscription fees are sunk cost)",
    "concurrency_enabled": "route-level provider_type: concurrency",
    "concurrency_limit": "route-level concurrency.limit",
    "concurrency_monthly_fee": "removed (subscription fees are sunk cost)",
}

# Tuning keys that no longer exist, and why. Unlike the keys above, these never
# changed what a deployment provisions, so a models.yaml that still sets one
# keeps booting: the key is dropped and has no effect, whatever its value.
_RETIRED_PARAMS: dict[str, str] = {
    "latency_min_samples": "provider selection never read it",
}
_warned_retired_params: set[str] = set()
_warned_retired_params_lock = threading.Lock()


def _warn_retired_params(keys: list[str]) -> None:
    # Validation also runs on Admin reads, so warn once per key per process.
    with _warned_retired_params_lock:
        fresh = [key for key in keys if key not in _warned_retired_params]
        _warned_retired_params.update(fresh)
    if fresh:
        logger.warning(
            "Ignoring retired RouteWise router_params, which have no effect; remove "
            "them from models.yaml: %s",
            "; ".join(f"{key} ({_RETIRED_PARAMS[key]})" for key in fresh),
        )


class _RouteWiseParamsBase(BaseModel):
    """Strict base for generated RouteWise strategy parameters."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _reject_moved_resource_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            moved = sorted(set(data) & set(_MOVED_TO_ROUTE_LEVEL))
            if moved:
                hints = "; ".join(f"'{key}' -> {_MOVED_TO_ROUTE_LEVEL[key]}" for key in moved)
                raise ValueError(
                    "RouteWise resource limits moved from router_params to the "
                    f"route entries in models.yaml: {hints}"
                )
            retired = sorted(set(data) & _RETIRED_PARAMS.keys())
            if retired:
                _warn_retired_params(retired)
                data = {key: value for key, value in data.items() if key not in _RETIRED_PARAMS}
        return data


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
