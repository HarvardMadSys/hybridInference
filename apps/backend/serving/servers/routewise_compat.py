"""Import-tolerant access to the RouteWise integration.

RouteWise is a required dependency: the public, MIT-licensed
``llm-routewise`` distribution on PyPI, published by the same lab as this
gateway. A correctly installed backend therefore always takes the real
import path below.

This module covers the case where it is nevertheless absent — a partial
install, or a build that deliberately drops the strategy — because no single
routing algorithm should decide whether HybridInference can start. Serving
code that merely *checks* for RouteWise routers — the boot path and the
generic provider admin — imports these names from here so the backend stays
importable either way. With RouteWise installed every re-export is the real
object and behavior is byte-identical.

Without the package the stand-ins are inert by design:

- ``RouteWiseRouter`` is a sentinel type nothing is an instance of, so every
  ``isinstance`` gate is simply False;
- ``EnvelopeNotCalibratedError`` is never raised, so ``except`` arms never
  match;
- ``model_routewise_setting_keys`` returns no keys (there are no RouteWise
  settings to invalidate);
- constructing ``RouteWiseSettingsResolver`` or calling
  ``apply_routewise_settings_to_router`` raises — the boot path only reaches
  them behind availability gates, and reaching them without the package is a
  real bug worth surfacing.

The dedicated RouteWise admin router and the strategy registration carry
their own guards (``serving.servers.routers.admin`` and
``routing.strategies``).
"""

from __future__ import annotations

from typing import Any

_MISSING = (
    "RouteWise is not installed; reinstall the gateway dependencies "
    "(uv sync) to restore the required 'llm-routewise' distribution"
)

try:
    from routing.routewise.envelope import EnvelopeNotCalibratedError
    from routing.routewise.router import RouteWiseRouter
    from serving.config.routewise_model_settings import (
        RouteWiseSettingsResolver,
        apply_routewise_settings_to_router,
        model_routewise_setting_keys,
    )

    ROUTEWISE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the neutral-import test
    ROUTEWISE_AVAILABLE = False

    class EnvelopeNotCalibratedError(Exception):  # type: ignore[no-redef]
        """Stand-in that no code path raises when RouteWise is absent."""

    class RouteWiseRouter:  # type: ignore[no-redef]
        """Sentinel type: nothing is ever an instance of it."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Refuse construction without the RouteWise package."""
            raise RuntimeError(_MISSING)

    class RouteWiseSettingsResolver:  # type: ignore[no-redef]
        """Stand-in that refuses construction without the package."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Refuse construction without the RouteWise package."""
            raise RuntimeError(_MISSING)

    async def apply_routewise_settings_to_router(*args: Any, **kwargs: Any) -> None:
        """Refuse to apply RouteWise settings without the package."""
        raise RuntimeError(_MISSING)

    def model_routewise_setting_keys(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        """Without RouteWise there are no per-model setting keys."""
        return ()


__all__ = [
    "ROUTEWISE_AVAILABLE",
    "EnvelopeNotCalibratedError",
    "RouteWiseRouter",
    "RouteWiseSettingsResolver",
    "apply_routewise_settings_to_router",
    "model_routewise_setting_keys",
]
