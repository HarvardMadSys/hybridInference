"""Backward-compatibility re-exports for routing.executor.

The router implementation now lives in ``routing.routers``. This module
re-exports its public symbols and legacy helper aliases from public leaf
modules so that existing imports continue to work:

    from routing.executor import RouteExecutor
    from routing.executor import AllCircuitsOpenError, ProviderPinError, RouteConfig
"""

from routing.routers import (
    AllCircuitsOpenError,
    FixedRouter as RouteExecutor,
    ProviderPinError,
    RouteConfig,
)
from routing.streaming import has_non_empty_content

_has_non_empty_content = has_non_empty_content

__all__ = [
    "AllCircuitsOpenError",
    "ProviderPinError",
    "RouteConfig",
    "RouteExecutor",
    "_has_non_empty_content",
]
