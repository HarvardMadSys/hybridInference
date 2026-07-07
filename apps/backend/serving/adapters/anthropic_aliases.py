"""Alias map: Anthropic's public display IDs -> our registry IDs.

Why: clients calling the Anthropic Messages API typically pass model names
like ``claude-3-5-sonnet-latest`` or dated aliases
(``claude-3-5-sonnet-20241022``). We register models under our own canonical
IDs (e.g. ``claude-sonnet-4.6``). This table maps the former to the latter so
existing Anthropic SDK code works without reconfiguration.
"""

from __future__ import annotations

import re

# Claude Code suffixes model IDs with a bracketed context-window marker when
# the user opts into a long-context variant (e.g. ``claude-opus-4-8[1m]``).
# No upstream of ours serves distinct context variants, so the marker is
# stripped before alias/registry resolution.
_CONTEXT_MARKER_RE = re.compile(r"\[\d+[km]\]$")

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

    A trailing context-window marker (``[1m]``-style, appended by Claude
    Code) is stripped first. Unknown IDs pass through; the router then
    attempts a direct registry lookup and returns 404 if that also fails.
    """
    base = _CONTEXT_MARKER_RE.sub("", model_id)
    return ANTHROPIC_MODEL_ALIASES.get(base, base)
