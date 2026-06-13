"""Kimi coding-plan adapter: OpenAICompatAdapter subclass with coding-tool identity.

The Kimi (Moonshot) coding plan gates access to requests that present a
recognized coding-tool identity. Two upstream requirements are encoded here:

1. A ``User-Agent: claude-code/0.1.0`` request header.
2. A leading ``{"role": "system", "content": "You are OpenCode"}`` message,
   prepended unless the request already starts with exactly that message.

Everything else (auth, payload shape, usage parsing, key-pool rotation) is
inherited unchanged from OpenAICompatAdapter.
"""

from __future__ import annotations

from typing import Any

from .openai_compat import OpenAICompatAdapter

# Coding-tool identity expected by the Kimi coding plan.
_USER_AGENT = "claude-code/0.1.0"
_SYSTEM_PROMPT = "You are OpenCode"


class KimiCodingAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter for the Kimi coding plan."""

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        # Only inject our default when no User-Agent is already present. The
        # check is case-insensitive so an explicit ``extra_headers`` override
        # (e.g. ``user-agent``) wins without producing a duplicate header.
        if not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = _USER_AGENT
        return headers

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        if not _starts_with_opencode_system(prepared):
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
