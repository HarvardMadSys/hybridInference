"""Coding-plan adapter: OpenAICompatAdapter subclass with coding-tool identity.

Several vendor coding plans (Kimi/Moonshot, the Z.AI GLM coding plan) gate
access to requests that present a recognized coding-tool identity. When
enabled, two upstream requirements are encoded here:

1. A ``User-Agent: claude-code/0.1.0`` request header.
2. A leading ``{"role": "system", "content": "You are OpenCode"}`` message,
   prepended unless the request already starts with exactly that message.

When the toggle is off, the system message is not injected and the caller's own
``User-Agent`` (captured into the request context by the request-id middleware)
is forwarded upstream instead of the coding-tool identity.

The same identity works for every coding-plan provider routed through this
adapter (e.g. the ``kimi_coding`` and ``zai`` adapter kinds), so the behaviour
is provider-neutral. Everything else (auth, payload shape, usage parsing,
key-pool rotation) is inherited unchanged from OpenAICompatAdapter.

The injection is gated by the ``coding_identity_enabled`` runtime setting
(admin-dashboard toggle, default on), which governs every coding-plan provider.
Each async request entrypoint resolves the toggle exactly once and snapshots it
into a :class:`~contextvars.ContextVar`; the synchronous header/message hooks
read that snapshot, so both see one consistent value for the lifetime of the
request even if an admin flips the setting mid-request. The snapshot is ``set``
but never ``reset`` — so it is safe across task hand-offs (e.g. RouteWise hedging
advancing a stream in a new task), and because each request runs in its own task
context the value never leaks between requests.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

from .openai_compat import OpenAICompatAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

# Coding-tool identity expected by the coding plans.
_USER_AGENT = "claude-code/0.1.0"
_SYSTEM_PROMPT = "You are OpenCode"
# Admin-dashboard toggle key (see RUNTIME_SETTINGS_REGISTRY). Governs every
# coding-plan provider routed through this adapter.
_SETTING_KEY = "coding_identity_enabled"

# Per-request snapshot of the toggle. Defaults to True so direct hook calls and
# pre-init paths preserve behaviour. See the module docstring for the rationale
# behind set-without-reset.
_identity_snapshot: ContextVar[bool] = ContextVar("coding_identity", default=True)


class CodingIdentityAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter that presents a coding-tool identity.

    Used by vendor coding plans (Kimi/Moonshot, Z.AI GLM) that gate access on a
    recognized coding-tool ``User-Agent`` plus a leading OpenCode system message.
    """

    async def _resolve_identity(self) -> bool:
        """Resolve the admin toggle, defaulting to enabled if unavailable."""
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            return await get_runtime_settings_instance().get_bool(_SETTING_KEY)
        except Exception:
            # Singleton not initialized, key missing, or DB/store error — never
            # let a settings lookup break inference; keep default behaviour.
            logger.warning(
                "coding_identity_toggle_read_failed",
                exc_info=True,
                extra={"event": "coding_identity_toggle_read_failed"},
            )
            return True

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Snapshot the identity toggle once, then run the completion path."""
        _identity_snapshot.set(await self._resolve_identity())
        return await super().chat_completion(messages, **params)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Snapshot the identity toggle once, then run the streaming path."""
        _identity_snapshot.set(await self._resolve_identity())
        async for chunk in super().stream_chat_completion(messages, **params):
            yield chunk

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Snapshot the identity toggle once, then run the embeddings path."""
        _identity_snapshot.set(await self._resolve_identity())
        return await super().embeddings(input_data, **params)

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        # An explicit ``extra_headers`` User-Agent (any casing) always wins.
        if any(key.lower() == "user-agent" for key in headers):
            return headers
        if _identity_snapshot.get():
            headers["User-Agent"] = _USER_AGENT
        else:
            # Toggle off: forward the caller's own User-Agent when available.
            client_ua = _client_user_agent()
            if client_ua:
                headers["User-Agent"] = client_ua
        return headers

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        if _identity_snapshot.get() and not _starts_with_opencode_system(prepared):
            prepared = [{"role": "system", "content": _SYSTEM_PROMPT}, *prepared]
        return prepared


def _client_user_agent() -> str | None:
    """Return the caller's User-Agent from the request context, if present."""
    from serving.utils import context as req_ctx

    ua = req_ctx.get().get("client_user_agent")
    return ua if isinstance(ua, str) and ua else None


def _starts_with_opencode_system(messages: list[dict[str, Any]]) -> bool:
    """Return True when the first message is exactly the OpenCode system message.

    Uses full dict equality (not just role/content) so a near-match carrying
    extra keys still triggers a prepend — keeping the leading message an exact
    match in case the upstream gate is strict.
    """
    if not messages:
        return False
    return messages[0] == {"role": "system", "content": _SYSTEM_PROMPT}
