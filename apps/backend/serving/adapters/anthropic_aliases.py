"""Alias map: Anthropic's public display IDs -> our registry IDs.

Why: clients calling the Anthropic Messages API typically pass model names
like ``claude-3-5-sonnet-latest`` or dated aliases
(``claude-3-5-sonnet-20241022``). We register models under our own canonical
IDs (e.g. ``claude-sonnet-4.6``). This table maps the former to the latter so
existing Anthropic SDK code works without reconfiguration.
"""

from __future__ import annotations

ANTHROPIC_MODEL_ALIASES: dict[str, str] = {
    # Sonnet family
    "claude-3-5-sonnet-latest": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.6",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    # Opus family
    "claude-3-opus-latest": "claude-opus-4.7",
    "claude-3-opus-20240229": "claude-opus-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-7": "claude-opus-4.7",
}


def resolve_anthropic_alias(model_id: str) -> str:
    """Return the registry ID for ``model_id``, or ``model_id`` if unknown.

    Unknown IDs pass through; the router then attempts a direct registry
    lookup and returns 404 if that also fails.
    """
    return ANTHROPIC_MODEL_ALIASES.get(model_id, model_id)
