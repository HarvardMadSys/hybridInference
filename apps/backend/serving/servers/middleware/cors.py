"""CORS whose allowed origins follow the ``CORS_ALLOWED_ORIGINS`` setting.

Starlette's ``CORSMiddleware`` computes its headers once, from the origins it is
built with, so a list captured at app creation would ignore an administrator's
later edit until a restart. This wrapper consults ``settings.cors_allowed_origins``
on every request and rebuilds the Starlette middleware when the list changes.
Every other option — credentials, methods, headers — is fixed at construction,
so the CORS semantics are exactly Starlette's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.middleware.cors import CORSMiddleware

from serving.config.settings import get_settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class SettingsCORSMiddleware:
    """Apply CORS with the origins currently configured."""

    def __init__(self, app: ASGIApp, **options: Any) -> None:
        """Wrap *app*; *options* are ``CORSMiddleware`` options other than the origins."""
        self.app = app
        self._options = options
        self._origins: list[str] | None = None
        self._cors: CORSMiddleware | None = None

    def _current(self) -> CORSMiddleware:
        origins = get_settings().cors_allowed_origins
        # The overlay replaces the list object when the setting changes, so the
        # identity check is the common, cheap case; equality covers a settings
        # object rebuilt with the same origins.
        if self._cors is None or (origins is not self._origins and origins != self._origins):
            self._cors = CORSMiddleware(self.app, allow_origins=list(origins), **self._options)
        self._origins = origins
        return self._cors

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request through CORS built from the current origin list."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self._current()(scope, receive, send)
