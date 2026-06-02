"""Effective-cost helpers for the current RouteWise body router."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from routewise.core import quota_effective_cost

ProviderTier = Literal["api", "quota", "concurrency"]


@dataclass(frozen=True)
class EffectiveCost:
    """Effective cost for one feasible provider candidate."""

    endpoint_id: str
    tier: ProviderTier
    cost_usd: float
    reason: str


def api_request_cost_usd(
    *,
    prompt_tokens: int | float,
    predicted_output_tokens: int | float,
    input_price_per_m: float,
    output_price_per_m: float,
) -> float:
    """Return cold-cache route-time API request cost in USD.

    The first FreeInference integration intentionally uses
    ``estimated_cached_tokens = 0`` for routing and envelope calibration.
    Actual post-completion billing remains cache-aware elsewhere.
    """
    prompt = max(float(prompt_tokens or 0), 0.0)
    output = max(float(predicted_output_tokens or 0), 0.0)
    return (input_price_per_m * prompt + output_price_per_m * output) / 1_000_000.0


def quota_shadow_price_usd(
    *,
    used_fraction: float,
    lower: float,
    upper: float,
) -> float:
    """Compute the Routewise quota shadow price ``L * (U/L)^z``."""
    lower = max(float(lower), 1e-12)
    upper = max(float(upper), lower)
    if math.isclose(upper, lower):
        return lower
    return quota_effective_cost(float(used_fraction), L=lower, U=upper)
