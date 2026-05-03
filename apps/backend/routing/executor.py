"""Backward-compatibility re-exports for routing.executor.

All routing logic now lives in ``routing.routers``. This module re-exports
the public symbols so that existing imports continue to work:

    from routing.executor import RouteExecutor
    from routing.executor import AllCircuitsOpenError, ProviderPinError, RouteConfig
"""

from routing.routers import (
    AllCircuitsOpenError,
    FixedRouter as RouteExecutor,
    ProviderPinError,
    RouteConfig,
    _has_non_empty_content,
)

__all__ = [
    "AllCircuitsOpenError",
    "ProviderPinError",
    "RouteConfig",
    "RouteExecutor",
    "_has_non_empty_content",
]
