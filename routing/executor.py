"""Backward-compatibility re-exports for routing.executor.

All routing logic now lives in ``routing.routers``. This module re-exports
the public symbols so that existing imports continue to work:

    from routing.executor import RouteExecutor
    from routing.executor import ProviderPinError, RouteConfig
"""

from routing.routers import (
    FixedRouter as RouteExecutor,
    ProviderPinError,
    RouteConfig,
    _has_non_empty_content,
)

__all__ = ["ProviderPinError", "RouteConfig", "RouteExecutor", "_has_non_empty_content"]
