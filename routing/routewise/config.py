"""RouteWise policy configuration.

Defines tunable parameters for the RouteWise cost-aware routing algorithm,
including decision rule selection, predictor settings, quota parameters,
concurrency parameters, and shadow price bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from serving.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RouteWiseConfig:
    """Policy parameters for RouteWise cost-aware routing.

    Attributes:
        decision_rule: Decision algorithm -- "pd" (primal-dual) or "lapd"
            (look-ahead primal-dual).
        predictor: Latency predictor type -- "ema" or "histogram".
        risk_quantile: Quantile for lower confidence bound in lapd mode.

        daily_quota: Maximum requests per day for S_Q (quota) subscriptions.
        quota_monthly_fee: Monthly cost of the quota subscription (USD).
        reset_timezone: Timezone for daily quota reset.

        concurrency_enabled: Whether S_C (concurrency) routing is active.
            Disabled in Stage 1.
        concurrency_limit: Max concurrent requests for S_C subscriptions.
        concurrency_monthly_fee: Monthly cost of the concurrency subscription
            (USD).

        shadow_price_L_seed: Initial lower bound for shadow price search.
        shadow_price_U_seed: Initial upper bound for shadow price search.
        shadow_price_adaptive: Enable adaptive shadow price window.
        shadow_price_window_hours: Lookback window (hours) for adaptive
            shadow price estimation.
        shadow_price_min_ratio: Minimum observations required per window
            before adaptive estimation activates.
    """

    decision_rule: str = "pd"
    predictor: str = "ema"
    risk_quantile: float = 0.10

    # S_Q quota parameters
    daily_quota: int = 5000
    quota_monthly_fee: float = 20.0
    reset_timezone: str = "UTC"

    # S_C concurrency parameters (Stage 2, disabled in Stage 1)
    concurrency_enabled: bool = False
    concurrency_limit: int = 8
    concurrency_monthly_fee: float = 25.0

    # Shadow price bounds
    shadow_price_L_seed: float = 0.001
    shadow_price_U_seed: float = 0.500
    shadow_price_adaptive: bool = True
    shadow_price_window_hours: int = 24
    shadow_price_min_ratio: int = 10


def load_routewise_config(path: Path | None = None) -> RouteWiseConfig:
    """Load RouteWise configuration from a YAML file.

    If the file does not exist or cannot be parsed, returns a
    ``RouteWiseConfig`` with default values.

    Args:
        path: Path to the YAML configuration file.  When *None*, defaults
            to ``config/routewise.yaml`` relative to the project root.

    Returns:
        A populated ``RouteWiseConfig`` instance.
    """
    if path is None:
        path = Path("config/routewise.yaml")

    if not path.exists():
        logger.info("RouteWise config not found at %s; using defaults", path)
        return RouteWiseConfig()

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Failed to load RouteWise config from %s (%s); using defaults",
            path,
            exc,
        )
        return RouteWiseConfig()

    if not isinstance(raw, dict):
        logger.warning(
            "RouteWise config at %s must be a mapping at the root; got %s. " "Using defaults.",
            path,
            type(raw).__name__,
        )
        return RouteWiseConfig()

    section = raw.get("routewise", raw)
    if section is None:
        data: dict[str, Any] = {}
    elif isinstance(section, dict):
        data = section
    else:
        logger.warning(
            "RouteWise config section 'routewise' in %s must be a mapping; "
            "got %s. Using defaults.",
            path,
            type(section).__name__,
        )
        return RouteWiseConfig()

    # Flatten ADR nested sections (quota, concurrency, shadow_price) into
    # the flat dataclass namespace so both flat and nested YAML work.
    _NESTED_MAP: dict[str, dict[str, str]] = {
        "quota": {
            "daily_quota": "daily_quota",
            "monthly_fee": "quota_monthly_fee",
            "reset_timezone": "reset_timezone",
        },
        "concurrency": {
            "enabled": "concurrency_enabled",
            "limit": "concurrency_limit",
            "monthly_fee": "concurrency_monthly_fee",
        },
        "shadow_price": {
            "L_seed": "shadow_price_L_seed",
            "U_seed": "shadow_price_U_seed",
            "adaptive": "shadow_price_adaptive",
            "window_hours": "shadow_price_window_hours",
            "min_ratio": "shadow_price_min_ratio",
        },
    }

    flat: dict[str, Any] = {}
    valid_keys = {f.name for f in RouteWiseConfig.__dataclass_fields__.values()}
    nested_sections = set(_NESTED_MAP.keys())

    for k, v in data.items():
        if k in nested_sections and isinstance(v, dict):
            mapping = _NESTED_MAP[k]
            for sub_key, sub_val in v.items():
                flat_key = mapping.get(sub_key)
                if flat_key is not None:
                    flat[flat_key] = sub_val
                else:
                    logger.warning(
                        "RouteWise config: unrecognized key '%s.%s' in %s",
                        k,
                        sub_key,
                        path,
                    )
        elif k in valid_keys:
            flat[k] = v
        else:
            logger.warning("RouteWise config: unrecognized key '%s' in %s", k, path)

    cfg = RouteWiseConfig(**flat)
    logger.info("Loaded RouteWise config from %s", path)
    return cfg
