"""Provider label → human-readable display name resolution.

A provider *label* is the slug that lands in ``api_logs.provider`` and
``provider_hourly_stats.provider``; it is what every provider-scoped admin view
groups on. By default the label is the route ``kind`` (``vllm``, ``zai``, …),
but ``config/models.yaml`` can override it per route so two endpoints of the
same kind — e.g. two local GPU boxes both served by vLLM — stay separate
cohorts in the dashboard instead of collapsing into one row. See
``serving.servers.registry`` for the parsing side.

This module is deliberately a leaf: it imports no admin router, so both the
routing layer and the admin endpoints can use it without an import cycle.
"""

from __future__ import annotations

from typing import Any

# Display names for labels that ship with the gateway. A route-level
# ``provider_display_name`` in models.yaml and admin-created custom provider
# definitions both take precedence over this table.
BUILT_IN_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic",
    "chutes": "Chutes",
    "claude": "Claude (Vertex)",
    "cliproxy": "CLI Proxy",
    "deepseek": "DeepSeek",
    "featherless": "Featherless",
    "gemini": "Gemini",
    "kimi": "Kimi",
    "kimi_coding": "Kimi (coding plan)",
    "minimax": "MiniMax",
    "ollama": "Ollama",
    "openai": "OpenAI",
    "openai_compat": "OpenAI-compatible",
    "openrouter": "OpenRouter",
    "sglang": "SGLang",
    "staging": "staging",
    "vllm": "vLLM",
    "zai": "ZAI",
}

# route_metadata key carrying the operator-supplied display name for a
# relabelled route. Written by the registry, read here.
DISPLAY_NAME_METADATA_KEY = "provider_display_name"


def humanize_provider(provider: str) -> str:
    """Return a readable fallback name for an otherwise unknown label."""
    return provider.replace("_", " ").replace("-", " ").title()


def display_names_from_router(router_obj: Any) -> dict[str, str]:
    """Map provider label → display name declared by models.yaml routes.

    Walks the live routing table looking for the display name the registry
    stashed in ``route_metadata``. When several routes claim the same label,
    the first one encountered wins so the result is stable across calls.
    """
    names: dict[str, str] = {}
    for route in getattr(router_obj, "routes", {}).values():
        for adapter, _weight in getattr(route, "adapters", []):
            config = getattr(adapter, "config", None)
            provider = getattr(config, "provider", None)
            if not isinstance(provider, str) or not provider:
                continue
            if provider in names:
                continue
            metadata = getattr(config, "route_metadata", None) or {}
            display_name = metadata.get(DISPLAY_NAME_METADATA_KEY)
            if isinstance(display_name, str) and display_name.strip():
                names[provider] = display_name.strip()
    return names


def _custom_display_names() -> dict[str, str]:
    """Map provider label → display name for admin-created custom providers."""
    from serving.adapters import provider_registry

    return {
        definition.provider: definition.display_name
        for definition in provider_registry.list_provider_definitions()
        if definition.display_name
    }


def resolve_display_names(router_obj: Any) -> dict[str, str]:
    """Return every provider label the gateway can name, mapped to its name.

    Precedence, highest first: a route-level ``provider_display_name`` in
    models.yaml, an admin-created custom provider definition, then the built-in
    table. Labels with no known name are absent from the result — callers fall
    back to showing the raw label rather than a guessed one.
    """
    names = dict(BUILT_IN_DISPLAY_NAMES)
    names.update(_custom_display_names())
    names.update(display_names_from_router(router_obj))
    return names
