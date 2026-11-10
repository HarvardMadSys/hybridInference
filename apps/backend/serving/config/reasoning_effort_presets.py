"""Verified effort defaults registered by trusted backend extensions."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from serving.adapters.reasoning_effort import validate_effort_map

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class ReasoningEffortPreset:
    """A named default, using the same public-to-upstream map as a route override."""

    label: str
    effort_map: Mapping[str, str]


_PRESETS: dict[tuple[str, str, str, str], ReasoningEffortPreset] = {}


def _preset_key(
    provider: str, adapter_kind: str, provider_model_id: str, base_url: str
) -> tuple[str, str, str, str]:
    # OpenRouter pins can be written as a bare provider slug or bracketed
    # target in admin. The adapter kind already names the exact pin.
    if adapter_kind == "openrouter" or adapter_kind.startswith("openrouter["):
        provider, adapter_kind = "openrouter", adapter_kind.lower()
    return provider, adapter_kind, provider_model_id, base_url.rstrip("/")


def register_reasoning_effort_preset(
    *,
    provider: str,
    adapter_kind: str,
    provider_model_id: str,
    base_url: str,
    label: str,
    effort_map: Mapping[str, str],
) -> None:
    """Register one verified upstream target. Duplicate targets fail at startup."""
    key = _preset_key(provider, adapter_kind, provider_model_id, base_url)
    if not all(key) or not label.strip():
        raise ValueError("An effort preset requires a provider, adapter kind, model, URL and label")
    if key in _PRESETS:
        raise ValueError(
            f"Duplicate reasoning effort preset for {provider!r} / {provider_model_id!r}"
        )
    _PRESETS[key] = ReasoningEffortPreset(label, MappingProxyType(validate_effort_map(effort_map)))


def find_reasoning_effort_preset(
    provider: str, adapter_kind: str, provider_model_id: str, base_url: str
) -> ReasoningEffortPreset | None:
    """Match the actual upstream, including its URL, with no name or family inference."""
    return _PRESETS.get(_preset_key(provider, adapter_kind, provider_model_id, base_url))
