"""Kimi coding-plan adapter: OpenAICompatAdapter subclass with coding-tool identity.

The Kimi (Moonshot) coding plan gates access to requests that present a
recognized coding-tool identity. When enabled, two upstream requirements are
encoded here:

1. A ``User-Agent: claude-code/0.1.0`` request header.
2. A leading ``{"role": "system", "content": "You are OpenCode"}`` message,
   prepended unless the request already starts with exactly that message.

The injection is gated by the ``kimi_coding_identity_enabled`` runtime setting
(admin-dashboard toggle, default on). The setting read is async, but the header
and message hooks are synchronous, so the resolved value is stashed in a
:class:`~contextvars.ContextVar` by the async request entrypoints and read back
by the sync hooks. ContextVars are per-task, so this stays correct when the
shared adapter instance serves concurrent requests.

Everything else (auth, payload shape, usage parsing, key-pool rotation) is
inherited unchanged from OpenAICompatAdapter.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .openai_compat import OpenAICompatAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Coding-tool identity expected by the Kimi coding plan.
_USER_AGENT = "claude-code/0.1.0"
_SYSTEM_PROMPT = "You are OpenCode"
# Admin-dashboard toggle key (see RUNTIME_SETTINGS_REGISTRY).
_SETTING_KEY = "kimi_coding_identity_enabled"

# Per-request flag set by the async entrypoints and read by the sync hooks.
# Defaults to True so direct hook calls (and pre-init paths) preserve behaviour.
_identity_active: ContextVar[bool] = ContextVar("kimi_coding_identity_active", default=True)


class KimiCodingAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter for the Kimi coding plan."""

    async def _identity_enabled(self) -> bool:
        """Resolve the admin toggle, defaulting to enabled if unavailable."""
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            return await get_runtime_settings_instance().get_bool(_SETTING_KEY)
        except (RuntimeError, KeyError):
            # Singleton not initialized or key missing — keep default behaviour.
            return True

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Resolve the identity toggle, then run the standard completion path."""
        token = _identity_active.set(await self._identity_enabled())
        try:
            return await super().chat_completion(messages, **params)
        finally:
            _identity_active.reset(token)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Resolve the identity toggle, then run the standard streaming path."""
        token = _identity_active.set(await self._identity_enabled())
        try:
            async for chunk in super().stream_chat_completion(messages, **params):
                yield chunk
        finally:
            _identity_active.reset(token)

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        # Only inject our default when the toggle is on and no User-Agent is
        # already present. The check is case-insensitive so an explicit
        # ``extra_headers`` override (e.g. ``user-agent``) wins without
        # producing a duplicate header.
        if _identity_active.get() and not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = _USER_AGENT
        return headers

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        if _identity_active.get() and not _starts_with_opencode_system(prepared):
            prepared = [{"role": "system", "content": _SYSTEM_PROMPT}, *prepared]
        return prepared


def _starts_with_opencode_system(messages: list[dict[str, Any]]) -> bool:
    """Return True when the first message is exactly the OpenCode system message.

    Uses full dict equality (not just role/content) so a near-match carrying
    extra keys still triggers a prepend — keeping the leading message an exact
    match in case the upstream gate is strict.
    """
    if not messages:
        return False
    return messages[0] == {"role": "system", "content": _SYSTEM_PROMPT}
