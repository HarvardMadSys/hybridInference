"""Kimi coding-plan adapter: OpenAICompatAdapter subclass with coding-tool identity.

The Kimi (Moonshot) coding plan gates access to requests that present a
recognized coding-tool identity. When enabled, two upstream requirements are
encoded here:

1. A ``User-Agent: claude-code/0.1.0`` request header.
2. A leading ``{"role": "system", "content": "You are OpenCode"}`` message,
   prepended unless the request already starts with exactly that message.

The injection is gated by the ``kimi_coding_identity_enabled`` runtime setting
(admin-dashboard toggle, default on). This is a *global* setting, so the
synchronous header/message hooks read it straight from the runtime-settings TTL
cache via :meth:`RuntimeSettings.get_cached`; the async request entrypoints warm
that cache first (a best-effort DB-backed refresh). Reading the cache is
synchronous and per-instance-safe, so it works correctly under concurrency and
across task hand-offs (e.g. RouteWise hedging advancing a stream in a new task).

Everything else (auth, payload shape, usage parsing, key-pool rotation) is
inherited unchanged from OpenAICompatAdapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

from .openai_compat import OpenAICompatAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

# Coding-tool identity expected by the Kimi coding plan.
_USER_AGENT = "claude-code/0.1.0"
_SYSTEM_PROMPT = "You are OpenCode"
# Admin-dashboard toggle key (see RUNTIME_SETTINGS_REGISTRY).
_SETTING_KEY = "kimi_coding_identity_enabled"


class KimiCodingAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter for the Kimi coding plan."""

    async def _warm_identity_setting(self) -> None:
        """Refresh the toggle into the runtime-settings TTL cache.

        Best-effort: the synchronous hooks read the cached value, so we resolve
        it (DB-backed) here first. Any failure is logged and left to the hooks'
        default-on fallback, so a settings outage never breaks a request.
        """
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            await get_runtime_settings_instance().get_bool(_SETTING_KEY)
        except Exception:
            logger.warning(
                "kimi_identity_toggle_read_failed",
                extra={"event": "kimi_identity_toggle_read_failed"},
            )

    def _identity_enabled(self) -> bool:
        """Read the toggle synchronously from the runtime-settings cache.

        Defaults to enabled when the cache is cold or settings are unavailable,
        matching the registry default and preserving behaviour on cold starts.
        """
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            found, value = get_runtime_settings_instance().get_cached(_SETTING_KEY)
            if found:
                return bool(value)
        except Exception:
            pass
        return True

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Warm the identity toggle, then run the standard completion path."""
        await self._warm_identity_setting()
        return await super().chat_completion(messages, **params)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Warm the identity toggle, then run the standard streaming path."""
        await self._warm_identity_setting()
        async for chunk in super().stream_chat_completion(messages, **params):
            yield chunk

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Warm the identity toggle, then run the standard embeddings path."""
        await self._warm_identity_setting()
        return await super().embeddings(input_data, **params)

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        # Only inject our default when the toggle is on and no User-Agent is
        # already present. The check is case-insensitive so an explicit
        # ``extra_headers`` override (e.g. ``user-agent``) wins without
        # producing a duplicate header.
        if self._identity_enabled() and not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = _USER_AGENT
        return headers

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        if self._identity_enabled() and not _starts_with_opencode_system(prepared):
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
